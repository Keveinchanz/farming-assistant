"""任务 3.3 —— 多模态信息融合。

把几路输入汇到一次大模型调用：
    图像识别（ONNX 模型）  → 候选病害 + 置信度
    虫害检测（YOLOv8n ONNX）→ 图上的虫体框（与病害是**两套独立标签空间**）
    语音转写（百度 ASR）   → 文本
    文字描述（用户填写）   → 文本
    知识库检索（RAG 4.4）  → 参考资料

融合策略（顺序很重要）：
    1. 图像识别给出 top-k 候选，作为**闭集**提供给大模型 —— 大模型只能从中选，防幻觉。
    2. 文字/语音描述再与知识库的作物名、病害名做匹配，把命中的病害补进候选集。
       这样即使用户没传图，纯文字描述也能得到有效诊断。
    3. RAG 检索用（文字描述 + 候选病害名）拼查询串，召回防治资料。
    4. 交给大模型生成报告；模型不可用时由规则模板降级生成，结构完全一致。

三处降级状态（llm / rag / voice）都在响应的 degraded 字段里如实上报，前端据此显示角标。

## 虫害为什么**不进** candidates

candidates 是闭环的**闭集**（见 _text_candidates 与 MAX_CANDIDATES），它存在的唯一理由
就是让大模型只能从有限选项里挑，从根上堵死幻觉。而虫害是 102 类昆虫，
既无法枚举进提示词、也无法用候选列表卡住 —— 塞进 candidates 会同时做三件坏事：
污染这个防幻觉设计、挤掉本来就更重要的病害候选、让 mock 的规则模板取到昆虫名后无从下手。

所以虫害走**完全独立的一路**：顶层新增 `pest` 字段（结构化检测结果，给前端画框），
以及 context 里的 `pest_detections`（给大模型当参考上下文）。
LLM 侧另有一条白名单防线：见 DeepSeekProvider._normalize_pest。

## 「不进闭集」与「是不是主结论」是两件事

虫害不进 candidates（上一条，防的是大模型编造虫名），但**检出虫害时它是主结论** ——
见 `_apply_pest_primary`。原因是病害那一路**总会**给出一个 Top-1：照片上真正的麻烦是虫时，
分类器仍会报出一个病名和某个置信度，若不降级，界面上就会出现
「一条 0.96 的虫害证据被一个 0.18 的病害结论压在下面」。
（注意：病害分类器**并不是**没有「健康」类，`label_map.json` 里有 `is_healthy` 标记；
问题不在这里，而在于缺一条「虫害更强时把病害降级」的规则。）
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

from src.common.io_utils import read_json
from src.common.logging_utils import setup_logger
from src.common.paths import path_of
from src.content.library_build import (
    flatten_plan,
    medication_source,
    medication_text,
    pest_plan_sections,
)
from src.dialect.config import get_dialect
from src.llm.base import BaseLLM
from src.voice.factory import get_voice

logger = setup_logger("agri.fusion")

MAX_CANDIDATES = 8
TEXT_MATCH_LIMIT = 5

# 敢说「未见明显病斑」所需的图像置信度。低于此值一律说「没把握，请补拍」：
# 把一片病叶说成健康，比说「看不清」严重得多 —— 前者会让农户放弃打药。
HEALTHY_CONF = 0.60

# 低到这个程度的图像结论不该被当成「诊断」：实测一张健康玉米叶，Top-1 只有 8.7%，
# 模型却照样给出一个具体病名。农户只看标题，那个标题会让他白打一遍药。
# 取 0.25 而不是分类器的 0.45 阈值，是刻意保守：0.45 会把大量「低置信但对」的
# 正常图也压成「看不清」，覆盖率掉得比收益快。
LOW_CONF_GATE = 0.25


class DiagnosisFusion:
    """多模态诊断编排器。"""

    def __init__(self, predictor, llm: BaseLLM, rag=None, pest_detector=None) -> None:
        self.predictor = predictor
        self.llm = llm
        self.rag = rag
        # 可选：没装虫害模型时整个平台照常工作，只是没有这一路证据
        self.pest_detector = pest_detector
        self.label_map = read_json(path_of("label_map"))
        self._index: dict[str, dict] = self.label_map["index_to_label"]

    # ---------------------------------------------------------------- 文字匹配

    def _text_candidates(self, text: str) -> list[dict]:
        """从用户描述里匹配出病害候选。

        匹配规则：作物名 + 病害描述同时出现才算命中（只出现"叶子发黄"这类泛化描述不算），
        命中的优先度高于只命中作物名的。
        """
        text = (text or "").strip()
        if not text:
            return []

        strong, weak = [], []
        for entry in self._index.values():
            if entry["is_healthy"]:
                continue
            crop_hit = entry["crop"] and entry["crop"] in text
            cond_hit = entry["condition"] and entry["condition"] in text
            if crop_hit and cond_hit:
                strong.append(entry)
            elif cond_hit and len(entry["condition"]) >= 3:
                weak.append(entry)

        strong.sort(key=lambda e: -e["num_images"])
        weak.sort(key=lambda e: -e["num_images"])

        out, seen = [], set()
        for e in strong + weak:
            if e["label_cn"] in seen:
                continue
            seen.add(e["label_cn"])
            out.append({
                "label_cn": e["label_cn"], "crop": e["crop"], "condition": e["condition"],
                "pathogen_type": e["pathogen_type"], "index": e["index"],
                "confidence": 0.5 if e in strong else 0.3,
                "origin": "文字描述匹配",
            })
            if len(out) >= TEXT_MATCH_LIMIT:
                break
        return out

    # ---------------------------------------------------------------- 健康 / 不确定

    @staticmethod
    def _health_state(recognition: dict | None, pest_result: dict | None) -> str | None:
        """图像 Top-1 落在「健康」类时，给出该走哪个出口；不是健康类就返回 None。

        为什么必须单独一条路：`label_map.json` 里有 21 个 `is_healthy` 类。它们进了
        candidates 就会被当成病名，于是界面上出现「玉米健康」配一套「尽早防治」的
        方案 —— 这是农户一眼能看穿的错，代价是整条信任。

        三个条件缺一不可：
          · Top-1 是健康类；
          · 置信度 ≥ HEALTHY_CONF（不够高就说「没把握、补拍」，绝不硬说健康）；
          · 没有检出虫体（叶子上有虫就不是健康）。
        """
        top1 = (recognition or {}).get("top1") or {}
        if not top1.get("is_healthy"):
            return None
        if (pest_result or {}).get("count"):
            return None
        conf = float(top1.get("confidence") or 0.0)
        return "healthy" if conf >= HEALTHY_CONF else "unsure"

    def _rule_payload(self, *, kind: str, recognition: dict, asr: dict | None,
                      pest_result: dict | None, degraded: dict, dialect_key: str,
                      dialect_name: str, input_summary: dict) -> dict:
        """健康 / 没把握这两种结论一律走规则，不交给大模型。

        kind:
          · healthy          —— 图像置信度够，判健康；
          · unsure_healthy   —— 最像健康，但没到 HEALTHY_CONF；
          · unsure_weak      —— 图像 Top-1 连 LOW_CONF_GATE 都不到，等于没识别出来。

        为什么不让模型写：实测模型会一边在 summary 里写「未见明显病斑」，一边把
        disease_name 填成「玉米日灼病」，再配一套「农业防治/化学防治」——
        农户只看标题和方案，那套方案会被真的照做。
        """
        top1 = (recognition or {}).get("top1") or {}
        crop = str(top1.get("crop") or "")
        conf = float(top1.get("confidence") or 0.0)
        crop_txt = f"（{crop}）" if crop else ""

        if kind == "healthy":
            report = {
                "disease_name": "",
                "healthy": True,
                "confidence": round(conf, 4),
                "summary": f"这张照片未见明显病斑，暂时按健康处理{crop_txt}。",
                "symptom_match": (
                    f"图像识别结果落在健康类（{top1.get('label_cn', '健康')}，置信度 {conf:.0%}），"
                    "本次也未检出虫体。需要注意：一张照片只代表这一片叶，不能代表整块田，"
                    "更不等于往后不会发病。"
                ),
                "treatment_sections": [
                    {"title": "复查建议", "content":
                        "3 天后在同一片叶、同一角度再拍一张对比。若出现新斑点、卷曲、发黄或虫体，"
                        "再拍近景复诊 —— 早发现三天的病，用药量通常能少一半。"},
                    {"title": "现在不建议打药", "content":
                        "没看到症状就打预防药，既多花钱，也会让病原菌和害虫更快产生抗药性。"
                        "要防的话优先做农业措施：通风降湿、清除病残体、平衡氮肥。"},
                ],
                "medication_reminder": "",
                "safety_note": "本结论仅基于这一张照片，不构成全田诊断。",
                "source": "rule",
                "notice": "由健康类识别规则直接给出，未经大模型改写。",
            }
        elif kind == "unsure_healthy":
            report = {
                "disease_name": "",
                "confidence": round(conf, 4),
                "summary": "这张照片看着不像有病斑，但我没把握，先别急着打药。",
                "symptom_match": (
                    "图像最像健康类，但置信度不到 60%，达不到「判健康」的门槛。"
                    "常见原因是逆光、叶片抖动、焦点落在背景上 —— 这三种都会把病斑拍没。"
                ),
                "treatment_sections": [
                    {"title": "请补拍一张", "content":
                        "把叶子举到逆光下拍叶背：很多虫和霉层藏在叶背，正面看不出来。"
                        "手指按住叶柄防抖，手机离叶子 15~20 厘米，对焦点落在叶片上。"},
                    {"title": "如果是整株出问题", "content":
                        "先看是新叶还是老叶、是局部还是整片，再拍一张整株照，"
                        "方便判断是缺素还是病害。"},
                ],
                "medication_reminder": "",
                "safety_note": "本次没有形成诊断结论，未给出用药建议。",
                "source": "rule",
                "notice": "图像置信度不足，按「没把握」处理，未经大模型改写。",
            }
        else:   # unsure_weak
            report = {
                "disease_name": "",
                "confidence": round(conf, 4),
                "summary": "这张照片我没看清，先别急着打药，补拍一张再来。",
                "symptom_match": (
                    f"图像识别给不出可信结论（Top-1 仅 {conf:.1%}），文字描述里也没有可对应的症状。"
                    "这种情况下报一个病名等于拿猜测当诊断，所以宁可让你重拍一次。"
                ),
                "treatment_sections": [
                    {"title": "怎么补拍", "content":
                        "① 逆光拍叶背（虫和霉层最先在叶背露出来）；② 手机离叶 15~20 厘米，"
                        "手指按住叶柄防抖；③ 对焦点落在叶片上，别对着天空测光。"},
                    {"title": "顺手补两句话", "content":
                        "打字或按住说话，说清「什么作物、哪个部位、几天了、有没有蔓延」——"
                        "这两句有时比照片更有用。"},
                ],
                "medication_reminder": "",
                "safety_note": "本次没有形成诊断结论，未给出用药建议。",
                "source": "rule",
                "notice": "图像置信度过低，按「没看清」处理，未经大模型改写。",
            }

        report["treatment_plan"] = ""
        report["differential"] = []
        if pest_result is not None:
            report["pest_findings"] = [
                {"name": d.get("label_cn") or d.get("name") or "",
                 "confidence": d.get("confidence", 0.0),
                 "advice": "叶背重点检查，必要时按农药登记标签选药，注意安全间隔期"}
                for d in (pest_result.get("detections") or [])[:3]
            ]
        return {
            "recognition": recognition,
            "asr": asr,
            "pest": pest_result,
            "fused_summary": report["summary"],
            "report": report,
            "candidates": [],
            "sources": [],
            "degraded": degraded,
            "dialect": {"key": dialect_key, "name": dialect_name},
            "llm_provider": getattr(self.llm, "provider", "unknown"),
            "input_summary": input_summary,
        }

    # ---------------------------------------------------------------- 主流程

    def diagnose(
        self,
        *,
        image_bytes: bytes | None = None,
        audio_bytes: bytes | None = None,
        audio_format: str = "auto",
        user_text: str = "",
        dialect: str | None = None,
        topk: int = 5,
    ) -> dict:
        d = get_dialect(dialect)
        degraded = {"llm": False, "rag": False, "voice": False, "pest": False}

        # ---- 1. 图像识别 ----
        recognition = None
        if image_bytes:
            try:
                recognition = self.predictor.predict_bytes(image_bytes)
                logger.info("图像识别：%s（%.2f%%）",
                            recognition["top1"]["label_cn"] if recognition.get("top1") else "无结果",
                            (recognition.get("top1") or {}).get("confidence", 0) * 100)
            except Exception as e:  # noqa: BLE001
                logger.error("图像识别失败：%s", e)
                degraded["image_error"] = str(e)

        # 推理器自己有一份 topk 配置；接口与技能传入的 topk 必须在这里生效，
        # 否则调用方看到的参数是假的。top1 不受影响。
        if recognition and isinstance(recognition.get("topk"), list):
            k = max(1, min(10, int(topk or 5)))
            recognition["topk"] = recognition["topk"][:k]

        # ---- 1b. 虫害检测（同一张图，独立的第二路证据）----
        #
        # pest_result 为 None 表示「本次没有这一路证据」（没装检测器、没传图、或检测失败），
        # 前端据此不显示「疑似虫害」区块；有值但 count=0 表示「跑过了，确实没虫子」。
        # 两种状态必须区分开——前者说「没做这项检查」，后者说「检查了，没发现」，
        # 混为一谈会让农户以为已经排查过虫害。
        pest_result = None
        if image_bytes and self.pest_detector is not None:
            try:
                pest_result = self.pest_detector.detect_bytes(image_bytes)
                logger.info("虫害检测：%d 个框（%s）", pest_result.get("count", 0),
                            "、".join(x["label_cn"] for x in pest_result.get("detections", [])[:3])
                            or "无")
            except Exception as e:  # noqa: BLE001
                # 与既有三路一致：任何一路挂掉都不许让整个诊断接口失败，
                # 只把降级状态如实上报。
                logger.error("虫害检测失败：%s", e)
                degraded["pest"] = True
                pest_result = None

        # ---- 2. 语音转写 ----
        asr = None
        if audio_bytes:
            voice = get_voice()
            try:
                asr = voice.transcribe(audio_bytes, dialect=d["key"], audio_format=audio_format)
                degraded["voice"] = bool(asr.get("mock"))
            except Exception as e:  # noqa: BLE001
                logger.error("语音转写失败：%s", e)
                degraded["voice"] = True
                asr = {"text": "", "provider": "error", "mock": True,
                       "message": f"语音识别失败：{e}", "dialect": d["key"],
                       "dialect_name": d["name"], "fallback": False, "fallback_note": ""}

        asr_text = (asr or {}).get("text", "") or ""
        # 语音转写里抽取到的农业术语（作物名/病害名）一并作为文字匹配的输入，
        # 让"只说了一句话"的纯语音输入也能匹配到病害候选
        asr_terms = " ".join((asr or {}).get("matched_terms") or [])

        # ---- 3. 健康 / 没把握 两个出口，先于候选组装 ----
        health = self._health_state(recognition, pest_result)
        text_candidates = self._text_candidates(f"{user_text} {asr_text} {asr_terms}")
        rule_kind = ""
        if health == "healthy":
            rule_kind = "healthy"
        elif health == "unsure":
            rule_kind = "unsure_healthy"
        elif recognition and not text_candidates and not (pest_result or {}).get("count"):
            # 图像完全没把握、用户也没描述症状、**且没检出虫** ⇒ 这一轮没有证据可以下结论。
            # 此时还硬填一个病名，等于用 8% 的置信度冒充「诊断结果」。
            # 检出虫时不走这里：那是有证据的结论，虫害会由 _apply_pest_primary 升为主结论。
            img_conf = float((recognition.get("top1") or {}).get("confidence") or 0.0)
            if img_conf < LOW_CONF_GATE:
                rule_kind = "unsure_weak"
                logger.info("图像置信度 %.1f%% < %.0f%% 且无文字补充，按「没看清」处理",
                            img_conf * 100, LOW_CONF_GATE * 100)

        if rule_kind:
            # 这三种结论都不交给大模型：让模型「改写」健康，它多半会顺手补一段防治方案；
            # 让模型改写「没把握」，它多半会挑一个候选填上去 —— 两种都等于把猜测写成结论。
            return self._rule_payload(
                kind=rule_kind, recognition=recognition, asr=asr, pest_result=pest_result,
                degraded=degraded, dialect_key=d["key"], dialect_name=d["name"],
                input_summary={
                    "has_image": bool(image_bytes),
                    "has_audio": bool(audio_bytes),
                    "has_text": bool(user_text.strip()),
                },
            )

        # ---- 3b. 组装候选病害（图像 top-k + 文字匹配，去重后按置信度排序）----
        candidates: list[dict] = []
        seen: set[str] = set()
        if recognition and recognition.get("topk"):
            for item in recognition["topk"]:
                # 健康类不是病名，进候选只会被当成病来开药
                if item.get("is_healthy"):
                    continue
                candidates.append({**item, "origin": "图像识别"})
                seen.add(item["label_cn"])
        for item in text_candidates:
            if item["label_cn"] not in seen:
                candidates.append(item)
                seen.add(item["label_cn"])
        candidates = candidates[:MAX_CANDIDATES]

        # ---- 4. RAG 检索 ----
        retrieved: list[dict] = []
        rag_used = False
        if self.rag is not None:
            query = " ".join(filter(None, [
                user_text, asr_text,
                " ".join(c["label_cn"] for c in candidates[:3]),
            ])).strip()
            if query:
                try:
                    retrieved = self.rag.retrieve(query)
                    rag_used = bool(retrieved)
                except Exception as e:  # noqa: BLE001
                    logger.warning("RAG 检索失败，跳过：%s", e)
        degraded["rag"] = not rag_used

        # ---- 5. 大模型生成报告 ----
        context: dict[str, Any] = {
            "image_result": recognition,
            "asr_text": asr_text,
            "user_text": user_text,
            "candidates": candidates,
            "retrieved": retrieved,
            "dialect_name": d["name"],
        }
        # 「最像健康但不够确定」与「图像完全没把握」已经在 3 步里各自短路返回，
        # 走到这里的一定是「有候选、可以交给大模型」的情况。

        # 只有**确实跑过检测**才放这个键（空列表也算跑过）。
        # 两个 provider 都靠"键在不在"来区分「没做检测」与「检测了没检出」，
        # 所以这里不能图省事写成 context["pest_detections"] = (pest_result or {}).get("detections", [])
        # —— 那会把"没做检测"也写成空列表，前端就会永远显示"未发现虫害"。
        if pest_result is not None:
            context["pest_detections"] = pest_result.get("detections") or []

        report = self.llm.generate_report(context)
        degraded["llm"] = bool(report.get("source") == "mock")
        # 来源标注由服务端判定，不信任模型返回的字段（模型可能凭空生成来源）。
        # 只有确实用了农药登记数据的地方，_apply_pest_primary 才会重新写入。
        report["source_notes"] = []
        # 检出虫害就把主结论换成虫害（含防治方案）。放在这里而不是 provider 里，
        # 因为这是唯一同时看得到成品报告与结构化 pest_result 的地方。
        self._apply_pest_primary(report, pest_result)

        return {
            "recognition": recognition,
            "asr": asr,
            "pest": pest_result,
            "fused_summary": self._summary(recognition, asr_text, user_text, report, pest_result),
            "report": report,
            "candidates": candidates,
            "sources": [
                {"title": r.get("title", ""), "source": r.get("source", ""),
                 "score": r.get("score")}
                for r in retrieved
            ],
            "degraded": degraded,
            "dialect": {"key": d["key"], "name": d["name"]},
            "llm_provider": getattr(self.llm, "provider", "unknown"),
            "input_summary": {
                "has_image": bool(image_bytes),
                "has_audio": bool(audio_bytes),
                "has_text": bool(user_text.strip()),
            },
        }

    @staticmethod
    def _apply_pest_primary(report: dict, pest_result: dict | None) -> None:
        """检出虫害时，把主结论从病害换成虫害，防治方案一并换过去。

        触发条件只看 `count > 0`，**绝不看 `degraded["pest"]`** —— 检测抛异常时
        `pest_result` 是 None（两者互斥），看 degraded 会把「检测失败」也判成虫害。

        已知取舍：**只要检出就切**，不做「病害置信度也得低」的二次判断。所以
        「病害很确定、只是恰好框到一只低置信度的虫」也会切成虫害。这是刻意的规则
        （农户看到虫就得先处置虫），不是判错。

        换完的结果：`report["primary"]` 说明谁是主结论；病害那一路**不删**，
        降级成 `primary.note` 里的「另疑似病害」，`disease_name` 原样保留，
        `candidates` 也一个字不动（它仍是纯病害闭集，防幻觉那条不变量不受影响）。
        """
        has_pest = bool(pest_result and pest_result.get("count"))
        if not has_pest:
            report["primary"] = {
                "kind": "disease" if report.get("disease_name") else "unknown",
                "name": report.get("disease_name") or "",
                "confidence": round(float(report.get("confidence") or 0.0), 4),
                "crop": "", "detail": "", "count": 0, "note": "",
            }
            return

        # 名字取自 pest_result["detections"][0]（检测器已按置信度降序排好）。
        # **不能**取 report["pest_findings"]：DeepSeek 的白名单过滤可能把它清成空数组，
        # 即使确实检出了虫。
        det = pest_result["detections"][0]
        name = det.get("label_cn") or ""
        crop = det.get("crop") or ""
        ptype = det.get("pest_type") or ""

        # 换防治方案。走「信息不足」兜底时，旧段落里那句「请补拍病叶正反面照片」
        # 对农户是有用的引导，先把它记下来，别在替换时弄丢。
        old_text = " ".join(
            f"{s.get('title', '')}：{s.get('content', '')}"
            for s in (report.get("treatment_sections") or [])
        )
        sections = pest_plan_sections(ptype)
        report["treatment_sections"] = sections
        # 换了段落就必须同步重算 treatment_plan（它是 sections 的纯文本副本），
        # 否则两者对不上 —— 而 treatment_plan 是被契约检查的。
        report["treatment_plan"] = flatten_plan(sections)
        report["medication_reminder"] = medication_text(name, crop)
        source = medication_source(name)
        report["source_notes"] = [source] if source else []

        disease = report.get("disease_name") or ""
        note = (f"另疑似病害：{disease}（{float(report.get('confidence') or 0):.0%}）"
                if disease else "")
        total = int(pest_result.get("count") or 0)
        report["primary"] = {
            "kind": "pest", "name": name,
            "confidence": round(float(det.get("confidence") or 0.0), 4),
            "crop": crop, "detail": ptype, "count": total, "note": note,
        }

        parts = [f"本次检出虫害：{name}" + (f"（共 {total} 处）" if total > 1 else "")
                 + "，建议按虫害方案处置。"]
        if note:
            parts.append(note + "，可一并核对。")
        if "照片" in old_text or "补充" in old_text:
            parts.append("建议同时补充拍摄病叶正反面照片，以便一并判断病害。")
        report["summary"] = "".join(parts)

    @staticmethod
    def _summary(recognition, asr_text: str, user_text: str, report: dict,
                 pest_result: dict | None = None) -> str:
        bits = []
        if recognition and recognition.get("top1"):
            t = recognition["top1"]
            bits.append(f"图像识别为{t['label_cn']}（{t['confidence']:.0%}）")
        else:
            bits.append("未提供图片")
        desc = " / ".join(x for x in (asr_text.strip(), user_text.strip()) if x)
        bits.append(f"描述：{desc}" if desc else "无文字或语音描述")
        # 结论顺序跟着 primary 走，而不是各自再判一遍有没有虫 —— 这样摘要和界面上
        # 那个大字标题永远说的是同一件事。
        primary = report.get("primary") or {}
        if primary.get("kind") == "pest":
            names = "、".join(x["label_cn"] for x in (pest_result or {}).get("detections", [])[:3])
            bits.append(f"综合结论：虫害{names}")
            if report.get("disease_name"):
                bits.append(f"另疑似病害：{report['disease_name']}")
        else:
            if report.get("disease_name"):
                bits.append(f"综合结论：{report['disease_name']}")
            # 虫害只在真检出时才提，避免每句话后面都挂个"未检出虫害"的尾巴
            if pest_result and pest_result.get("count"):
                names = "、".join(x["label_cn"] for x in pest_result["detections"][:3])
                bits.append(f"另检出虫害：{names}")
        return "；".join(bits)


def build_fusion(predictor=None, llm: BaseLLM | None = None, rag=None,
                 pest_detector="auto") -> DiagnosisFusion:
    """默认装配：图像用 registry/ONNX 的模型，大模型与 RAG 走各自的工厂。

    `pest_detector` 默认 "auto"：**有就装，没有就算了**。
    虫害模型是后加的、可选的第二路证据，如果它缺席就让整个诊断链路起不来，
    那是把可选能力做成了必需依赖 —— 所以这里只记一条 warning。
    """
    from src.llm.factory import get_llm
    from src.rag.factory import get_rag

    if predictor is None:
        from src.infer.predictor import get_predictor

        predictor = get_predictor()

    if pest_detector == "auto":
        try:
            from src.infer.pest_detector import get_pest_detector

            pest_detector = get_pest_detector()
        except Exception as e:  # noqa: BLE001
            logger.warning("虫害检测模型不可用，本次诊断不含虫害证据：%s", e)
            pest_detector = None

    return DiagnosisFusion(
        predictor=predictor,
        llm=llm or get_llm(),
        rag=rag if rag is not None else get_rag(),
        pest_detector=pest_detector,
    )


@lru_cache(maxsize=1)
def get_fusion() -> DiagnosisFusion:
    """进程内共享的融合诊断器。

    HTTP 路由与 skills 技能都通过这里拿同一个实例：分类器、检测器、LLM、RAG
    各只初始化一次，避免同一份 ONNX 在进程里被加载两遍。
    `backend.app.deps.get_fusion()` 只是本函数的一层错误映射包装。
    """
    from src.infer.pest_detector import get_shared_pest_detector
    from src.infer.predictor import get_shared_predictor

    return build_fusion(
        predictor=get_shared_predictor(),
        pest_detector=get_shared_pest_detector(),
    )


def dumps(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, default=str)
