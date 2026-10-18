"""大模型的规则化降级实现 —— 没配 DeepSeek Key 时使用。

原则与语音 Mock 一致：**不伪造模型的自由生成，只用可复核的规则拼装**。
报告内容全部来自两个可追溯的来源：
    · data/label_map.json          病害的作物/病害/病原类型等事实字段
    · src/content/library_build.py 按病原类型整理的防治要点模板（与知识图书库同一份，
                                   不重复维护两套文案）
返回结构与 DeepSeekProvider 完全一致，因此后端和前端无需区分。
响应里 source="mock" 且各接口的 degraded.llm=true，前端据此提示「大模型未配置」。
"""

from __future__ import annotations

from typing import Any

from src.common.logging_utils import setup_logger
from src.content.library_build import (
    PATHOGEN_TEXT,
    PEST_TEXT,
    SAFETY_NOTE,
    flatten_plan,
    split_control,
)
from src.llm.base import BaseLLM

logger = setup_logger("agri.llm.mock")

NOTICE = "未配置 DeepSeek 服务（.env 缺少 DEEPSEEK_API_KEY），以下为本地规则给出的报告。"

# 一份报告里最多列几条虫害。检测器可能在一张图上出十几个框，
# 全列出来会把报告淹掉；按置信度降序取前几条，与病害候选的 MAX_CANDIDATES 思路一致。
MAX_PEST_FINDINGS = 3


def _pest_findings(detections: list[dict]) -> list[dict]:
    """把检测框列表压成报告里的 pest_findings。

    同一张图上同一类虫子出现多个框是常态（例如 5 只蚜虫），这里按中文名去重，
    只保留**首次出现**那条 —— 而检测结果本身已按置信度降序排过，
    所以保留下来的就是该类里最可信的那个框。
    """
    out: list[dict] = []
    seen: set[str] = set()
    for d in detections:
        name = str(d.get("label_cn") or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        _, control = PEST_TEXT.get(d.get("pest_type", ""), PEST_TEXT["其他"])
        out.append({
            "name": name,
            "confidence": round(float(d.get("confidence") or 0.0), 4),
            "crop": d.get("crop", ""),
            "pest_type": d.get("pest_type", ""),
            "advice": control,
        })
        if len(out) >= MAX_PEST_FINDINGS:
            break
    return out


class MockLLM(BaseLLM):
    provider = "mock"

    def generate_report(self, context: dict[str, Any]) -> dict:
        candidates: list[dict] = context.get("candidates") or []
        image_result = context.get("image_result") or {}
        asr_text = (context.get("asr_text") or "").strip()
        user_text = (context.get("user_text") or "").strip()

        if candidates:
            top = candidates[0]
        elif image_result.get("top1"):
            top = image_result["top1"]
        else:
            return self._insufficient(context)

        name = top["label_cn"]
        crop = top["crop"]
        condition = top["condition"]
        pathogen = top.get("pathogen_type", "其他")
        confidence = float(top.get("confidence") or image_result.get("confidence") or 0.0)

        pattern, control = PATHOGEN_TEXT.get(pathogen, PATHOGEN_TEXT["其他"])

        evidence = []
        if image_result.get("top1"):
            evidence.append(
                f"图像识别结果指向「{image_result['top1']['label_cn']}」"
                f"（置信度 {image_result['top1']['confidence']:.2%}）"
            )
        else:
            evidence.append("本次未提供图片，判断仅基于文字/语音描述")
        if asr_text:
            evidence.append(f"农户语音描述为「{asr_text}」")
        if user_text:
            evidence.append(f"农户文字描述为「{user_text}」")
        evidence.append(f"结合上述信息，与{crop}{condition}（{pathogen}性）的典型表现相符")

        differential = [c["label_cn"] for c in candidates[1:4]]

        report = {
            "disease_name": name,
            "confidence": round(confidence, 4),
            "summary": f"综合判断为{crop}{condition}，建议按下列方案尽早防治。",
            "symptom_match": "；".join(evidence) + "。",
            "treatment_sections": [
                {"title": "发病规律与判断依据", "content": f"{crop}{condition}：{pattern}"},
                *split_control(control),
            ],
            "treatment_plan": "",
            "medication_reminder": SAFETY_NOTE,
            "safety_note": SAFETY_NOTE,
            "differential": differential,
            "source": self.provider,
            "notice": NOTICE,
        }

        # 虫害字段只在**检测器确实跑过**时出现（见 _pest_findings 与 fusion.py 的约定）：
        # 键不存在 = 本次没有虫害检测能力，前端不显示这个区块；
        # 空列表 = 检测器跑了但没检出，前端显示"未发现虫害"。
        pest = context.get("pest_detections")
        if pest is not None:
            report["pest_findings"] = _pest_findings(pest)
            # 这里**不再**改写 summary。虫害优先的摘要由 `fusion._apply_pest_primary`
            # 统一写（它是唯一同时看得到成品报告与结构化 pest_result 的地方）。
            # 两处都改会把同一句话改写两遍，而且 DeepSeek 路径没有这一段。

        report["treatment_plan"] = flatten_plan(report["treatment_sections"])
        logger.info("Mock 报告已生成：%s（候选 %d 个，虫害 %s）", name, len(candidates),
                    len(report.get("pest_findings") or []) if pest is not None else "未检测")
        return report

    def _insufficient(self, context: dict) -> dict:
        """连候选都没有（既没图也没能匹配到病害）时的兜底。

        注意这里**仍然要带虫害结果**：一张只有虫子的照片不会匹配到任何病害候选，
        但检测器是实打实检出了虫害的。如果在这里把虫害丢掉，就会出现
        「明明检出了稻飞虱，报告却说信息不足」的荒唐结果。
        """
        text = (context.get("user_text") or context.get("asr_text") or "").strip()
        pest = context.get("pest_detections")
        findings = _pest_findings(pest) if pest is not None else []

        if findings:
            summary = (f"本次未匹配到明确的病害，但检出"
                       f"{'、'.join(f['name'] for f in findings)}，"
                       "虫害处置建议见下方「疑似虫害」。建议同时补充拍摄病叶正反面照片以便判断病害。")
        else:
            summary = "信息不足，暂时无法给出疾病判断。"

        report = {
            "disease_name": "",
            "confidence": 0.0,
            "summary": summary,
            "symptom_match": (
                "本次没有可用的图像识别结果，文字/语音描述也未能匹配到知识库中的病害。"
                + (f"已收到的描述：{text}" if text else "")
                + " 建议补充：① 拍摄清晰的病叶正反面照片；② 说明作物名称与发病部位；"
                  "③ 描述发病时间、天气与近期用药情况。"
            ),
            "treatment_sections": [
                {"title": "建议补充的信息", "content":
                    "① 病叶正反面清晰照片（含整株与特写各一张）；② 作物名称与品种；"
                    "③ 发病部位（叶片/茎秆/根部/果实）；④ 发病持续时间与蔓延速度；"
                    "⑤ 近期天气、灌溉与施肥施药情况。"},
                {"title": "临时处置建议", "content":
                    "在确诊前先隔离发病植株，清除并销毁病叶病株，避免大水漫灌造成扩散；"
                    "暂不盲目施药，以免用错药剂延误防治时机。"},
            ],
            "treatment_plan": "",
            "medication_reminder": SAFETY_NOTE,
            "safety_note": SAFETY_NOTE,
            "differential": [],
            "source": self.provider,
            "notice": NOTICE,
        }
        if pest is not None:
            report["pest_findings"] = findings
        return report

    def answer(self, question: str, *, context: str = "") -> str:
        return (
            f"未配置 DeepSeek 服务，暂时无法对「{question}」生成智能回答。\n"
            "你可以在 .env 中填入 DEEPSEEK_API_KEY 后重启服务，即可启用大模型问答。\n"
            "在此期间，可先查阅「知识图书库」页面中与作物/病害对应的防治知识。"
        )

    def describe(self) -> dict:
        return {
            "provider": self.provider,
            "reason": "未配置 DEEPSEEK_API_KEY",
            "mode": "本地规则",
        }
