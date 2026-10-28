"""后端接口冒烟测试：依次调用全部接口并打印关键字段。

用法：
    python scripts/21_smoke_test_api.py
    python scripts/21_smoke_test_api.py --base http://127.0.0.1:8000
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.common.logging_utils import setup_logger  # noqa: E402

logger = setup_logger("agri.smoke")

PASS = "  [OK]  "
FAIL = "  [FAIL]"


class Client:
    """极简 HTTP 客户端，**自带一个 Cookie 罐**。

    阶段 7 起接口有了会话：注册/登录会下发 HttpOnly 的 session_token，
    后续请求必须带回去，否则拿到的永远是 401。urllib 不像 requests 那样
    自动管理 Cookie，所以这里手工维护一个 dict。
    """

    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")
        self.failures: list[str] = []
        self.cookies: dict[str, str] = {}

    # ---- Cookie ----

    def _with_cookies(self, headers: dict) -> dict:
        if self.cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        return headers

    def _capture_cookies(self, response) -> None:
        for raw in response.headers.get_all("Set-Cookie") or []:
            head = raw.split(";", 1)[0]
            name, _, value = head.partition("=")
            name, value = name.strip(), value.strip()
            if not name:
                continue
            if value:
                self.cookies[name] = value
            else:
                self.cookies.pop(name, None)      # 登出会下发一个空值来清 Cookie

    def logout(self) -> None:
        self.cookies.clear()

    # ---- 请求 ----

    def _send(self, req: urllib.request.Request, timeout: int):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                self._capture_cookies(r)
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            self._capture_cookies(e)
            raw = e.read().decode("utf-8", "ignore")
            try:
                return e.code, json.loads(raw)
            except json.JSONDecodeError:
                return e.code, {"raw": raw[:400]}
        except Exception as e:  # noqa: BLE001
            return 0, {"error": str(e)}

    def get(self, path: str) -> tuple[int, dict | None]:
        # 中文查询参数必须百分号编码，否则 urllib 会直接抛 UnicodeEncodeError
        url = self.base + urllib.parse.quote(path, safe="/?=&:")
        req = urllib.request.Request(url, headers=self._with_cookies({}), method="GET")
        return self._send(req, timeout=60)

    def post_json(self, path: str, payload: dict) -> tuple[int, dict | None]:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.base + path, data=data, method="POST",
            headers=self._with_cookies({"Content-Type": "application/json"}),
        )
        return self._send(req, timeout=120)

    def post_multipart(self, path: str, fields: dict, files: dict) -> tuple[int, dict | None]:
        boundary = "----agrismoke1234567890"
        body = bytearray()
        for k, v in fields.items():
            body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
        for k, (filename, content, ctype) in files.items():
            body += (
                f"--{boundary}\r\n"
                f"Content-Disposition: form-data; name=\"{k}\"; filename=\"{filename}\"\r\n"
                f"Content-Type: {ctype}\r\n\r\n"
            ).encode()
            body += content + b"\r\n"
        body += f"--{boundary}--\r\n".encode()

        req = urllib.request.Request(
            self.base + path, data=bytes(body), method="POST",
            headers=self._with_cookies({"Content-Type": f"multipart/form-data; boundary={boundary}"}),
        )
        return self._send(req, timeout=300)

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        if ok:
            logger.info("%s %s%s", PASS, name, f"  {detail}" if detail else "")
        else:
            logger.error("%s %s  %s", FAIL, name, detail)
            self.failures.append(name)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    c = Client(args.base)

    logger.info("=" * 68)
    logger.info("后端接口冒烟测试：%s", c.base)
    logger.info("=" * 68)

    # 1 健康检查
    code, body = c.get("/api/health")
    d = (body or {}).get("data") or {}
    model = d.get("model") or {}
    c.check("GET /api/health", code == 200 and (body or {}).get("code") == 0,
            f"status={d.get('status')} model={model.get('backend')} "
            f"classes={model.get('num_classes')}")

    # 1b 虫害检测模型的状态（任务 2.7）
    # 它**刻意**是 health 里的一个独立子对象，不并进上面的 model：model 的
    # backend / num_classes / input_size 说的是 271 类分类模型，把 102 类、640 输入的
    # 检测模型混进去，这三个字段立刻就变成自相矛盾的值。
    health_pest = d.get("pest_detector") or {}
    pest_ready = bool(health_pest.get("available"))
    c.check("GET /api/health 报出虫害检测模型状态", "available" in health_pest,
            f"pest_detector.available={health_pest.get('available')} "
            f"classes={health_pest.get('num_classes')} input={health_pest.get('input_size')} "
            f"backend={health_pest.get('backend')} "
            + (f"hint={health_pest.get('hint')}" if not health_pest.get("available") else ""))
    # 「模型文件在不在」与「health 说它能不能用」必须一致 —— 这条是**真踩过的坑**：
    # main.py 里曾经把 detector.conf_threshold 误写成 confidence_threshold，
    # 那一整块抛 AttributeError 落进 except，于是 health 对一个装好且跑得动的检测
    # 模型报 available=false（而同一时刻多模态接口正拿它出框）。上面那条只查
    # "available" 这个键在不在，压根拦不住这种自相矛盾。
    pest_onnx_exists = bool(((d.get("data") or {}).get("pest_onnx") or {}).get("exists"))
    c.check("health 的虫害模型状态与文件实际存在与否一致",
            pest_ready == pest_onnx_exists,
            f"ONNX 在盘上={pest_onnx_exists}，health 说可用={pest_ready}"
            + (f"；error={health_pest.get('error')}" if health_pest.get("error") else ""))
    # 两套模型各自的数字不能串：分类必须还是 271/224，检测（若已就位）必须是 102/640。
    # 检测未就位时只锁分类那一半，免得没导出 ONNX 就把冒烟测试搞红
    c.check("分类与检测两套模型的信息各归各位",
            model.get("num_classes") == 271 and model.get("input_size") == 224
            and (not pest_ready or (health_pest.get("num_classes") == 102
                                    and health_pest.get("input_size") == 640)),
            f"分类 {model.get('num_classes')} 类 / {model.get('input_size')}px；"
            f"检测 {health_pest.get('num_classes')} 类 / {health_pest.get('input_size')}px"
            f"（就位={pest_ready}）")

    # 2 方言列表
    code, body = c.get("/api/dialect/list")
    items = (body or {}).get("data") or []
    c.check("GET /api/dialect/list", code == 200 and len(items) == 8,
            f"{len(items)} 种方言；"
            + "、".join(f"{i['name']}{'(识别支持)' if i['asr_supported'] else '(识别回退)'}" for i in items[:4])
            + " …")

    # 3 地图数据
    code, body = c.get("/api/map/pest_top3?season=spring")
    d = (body or {}).get("data") or {}
    provs = d.get("provinces") or []
    sample = next((p for p in provs if p["name"] == "河南省"), provs[0] if provs else {})
    c.check("GET /api/map/pest_top3", code == 200 and len(provs) == 34,
            f"{len(provs)} 个省份，demo_data={d.get('demo_data')}；"
            f"河南 threat_scope={sample.get('threat_scope')} "
            f"TOP1={((sample.get('top3') or [{}])[0]).get('pest')}")

    code2, body2 = c.get("/api/map/pest_top3?season=winter")
    d2 = (body2 or {}).get("data") or {}
    w = next((p for p in (d2.get("provinces") or []) if p["name"] == "河南省"), {})
    s = next((p for p in (d.get("provinces") or []) if p["name"] == "河南省"), {})
    c.check("季节切换生效", w.get("top3") != s.get("top3"),
            f"河南 春={((s.get('top3') or [{}])[0]).get('pest')} vs 冬={((w.get('top3') or [{}])[0]).get('pest')}")

    # 4 政策
    code, body = c.get("/api/policy/list")
    d = (body or {}).get("data") or {}
    its = d.get("items") or []
    c.check("GET /api/policy/list",
            code == 200 and d.get("total") == 10 and all(i.get("summary") for i in its),
            f"{d.get('total')} 条；"
            + " | ".join(f"{i['title'][:14]}…/{i['publish_unit']}/{i['item_count']}项" for i in its[:2]))

    code, body = c.get("/api/policy/list?type=subsidy")
    d = (body or {}).get("data") or {}
    c.check("政策按类型筛选", code == 200 and d.get("total", 0) >= 1, f"补贴补助类 {d.get('total')} 条")

    # 5 知识图书库
    code, body = c.get("/api/library?page=1&size=5")
    d = (body or {}).get("data") or {}
    c.check("GET /api/library", code == 200 and d.get("total") == 399,
            f"共 {d.get('total')} 张卡片；返回 {len(d.get('items') or [])} 条；"
            f"分类={[(x['name'], x['count']) for x in (d.get('categories') or [])]}")

    kinds = {x["key"]: x["count"] for x in (d.get("categories") or [])}
    c.check("图书库三个分类计数正确",
            kinds == {"pest": 250, "insect": 102, "care": 47},
            f"病害 {kinds.get('pest')} / 虫害 {kinds.get('insect')} / 养护 {kinds.get('care')}"
            "（pest 这个键装的**全是病害**，虫害在 insect 里）")

    code, body = c.get("/api/library?category=insect&size=3")
    di = (body or {}).get("data") or {}
    c.check("按虫害分类筛选", code == 200 and di.get("total") == 102,
            f"虫害分类 {di.get('total')} 张")

    first = (d.get("items") or [{}])[0]
    code, body = c.get(f"/api/library/{first.get('id','')}")
    d2 = (body or {}).get("data") or {}
    c.check(f"GET /api/library/{first.get('id')}", code == 200 and len(d2.get("sections") or []) >= 2,
            f"{d2.get('title')}，{len(d2.get('sections') or [])} 个小节，read_text {len(d2.get('read_text') or '')} 字")
    # 防止 response_model 字段不全导致数据被静默过滤掉（曾漏掉 condition/pathogen_type）
    missing_fields = [k for k in ("condition", "pathogen_type", "crop_category", "content_source")
                      if k not in d2]
    c.check("图书详情字段完整（未被 response_model 过滤）", not missing_fields,
            f"condition={d2.get('condition')} pathogen_type={d2.get('pathogen_type')} "
            f"crop_category={d2.get('crop_category')}"
            if not missing_fields else f"缺少字段 {missing_fields}")

    # 虫害卡片专测：pest_type / latin 是这一轮新加的字段，走的正是上面那个坑
    # （pydantic 的 response_model 不报错、只静默丢字段），所以必须单独钉一条。
    # 锁的是「字段还在响应里」而不是「字段非空」—— 102 类里有 2 类没有可靠的
    # 拉丁学名（灰象甲、橘瘿蚊），卡片如实留空，那是数据事实不是 bug。
    insect_first = (di.get("items") or [{}])[0]
    code, body = c.get(f"/api/library/{insect_first.get('id','')}")
    dp = (body or {}).get("data") or {}
    c.check("虫害详情：pest_type / latin 活到了响应里（未被 response_model 过滤）",
            code == 200 and bool(dp.get("pest_type"))
            and "latin" in dp and dp.get("pest_type") != dp.get("pathogen_type"),
            f"{dp.get('title')}；虫害={dp.get('condition')} 类群={dp.get('pest_type')} "
            f"学名={dp.get('latin') or '（词表未收录）'} 小节={len(dp.get('sections') or [])} 个")
    c.check("虫害详情不含病原类型", dp.get("pathogen_type") == "",
            f"pathogen_type={dp.get('pathogen_type')!r}（虫害没有这个概念，应留空）")

    code, body = c.get("/api/library?q=霜霉")
    d3 = (body or {}).get("data") or {}
    c.check("图书库关键词检索", code == 200 and (d3.get("total") or 0) > 0, f"「霜霉」命中 {d3.get('total')} 条")

    # 6 TTS（Mock 应返回 mock=true 而不是报错）
    code, body = c.post_json("/api/voice/tts", {"text": "玉米大斑病防治要点", "dialect": "sichuan"})
    d = (body or {}).get("data") or {}
    c.check("POST /api/voice/tts", code == 200 and (body or {}).get("code") == 0,
            f"provider={d.get('provider')} mock={d.get('mock')} audio_url={d.get('audio_url')}")

    # 6b 农技问答（任务 3.3 的问答交互）
    code, body = c.post_json("/api/diagnose/ask",
                             {"question": "什么时候打药最合适？", "context": "当前诊断结论：玉米大斑病"})
    d = (body or {}).get("data") or {}
    c.check("POST /api/diagnose/ask", code == 200 and bool(d.get("answer")),
            f"provider={d.get('provider')} 回答 {len(d.get('answer') or '')} 字")

    code, body = c.post_json("/api/diagnose/ask", {"question": "   "})
    c.check("空问题被拒绝", code == 400, f"HTTP {code} msg={(body or {}).get('msg')}")

    # 7 图像识别（用数据集里的真图，结果应可解释）
    img = Path("Sample/297/297")
    img_files = sorted(img.glob("*.JPG")) if img.exists() else []
    if img_files:
        content = img_files[0].read_bytes()
        code, body = c.post_multipart("/api/diagnose/image", {}, {"file": (img_files[0].name, content, "image/jpeg")})
        d = (body or {}).get("data") or {}
        top1 = d.get("top1") or {}
        c.check("POST /api/diagnose/image",
                code == 200 and top1.get("label_cn") == "萝卜黑斑病",
                f"真实类别=萝卜黑斑病 -> 预测={top1.get('label_cn')} "
                f"置信度={top1.get('confidence')} 耗时={d.get('elapsed_ms')}ms backend={d.get('backend')}")
        topk = d.get("topk") or []
        c.check("图像识别返回 Top-5", len(topk) == 5,
                "、".join(f"{t['label_cn']}({t['confidence']:.2f})" for t in topk[:3]) + " …")

        # 8 多模态（图片 + 文字，无 Key 时应降级但结构完整）
        code, body = c.post_multipart(
            "/api/diagnose/multimodal",
            {"text": "叶片上出现黑色斑点，边缘发黄，最近雨水多", "dialect": "sichuan"},
            {"image": (img_files[0].name, content, "image/jpeg")},
        )
        d = (body or {}).get("data") or {}
        rep = d.get("report") or {}
        required = ["disease_name", "confidence", "symptom_match", "treatment_sections",
                    "treatment_plan", "medication_reminder", "safety_note", "summary", "source"]
        missing = [k for k in required if k not in rep]
        c.check("POST /api/diagnose/multimodal",
                code == 200 and not missing and len(rep.get("treatment_sections") or []) >= 2,
                f"结论={rep.get('disease_name')} 来源={rep.get('source')} "
                f"小节={len(rep.get('treatment_sections') or [])} degraded={d.get('degraded')}")
        c.check("多模态候选病害", len(d.get("candidates") or []) >= 1,
                "、".join(cb["label_cn"] for cb in (d.get("candidates") or [])[:4]))
        c.check("返回输入摘要", bool((d.get("input_summary") or {}).get("has_image")),
                json.dumps(d.get("input_summary"), ensure_ascii=False))

        # 8b 虫害检测结果（任务 2.7.8）
        # 三种情况的契约都得成立，缺一个老前端就会在某个分支上崩：
        #   没启用检测器 / 检测失败 -> pest 为 null
        #   跑过但没检出          -> pest 是结构完整的字典，detections 为 []
        degraded_pest = bool((d.get("degraded") or {}).get("pest"))
        pest_res = d.get("pest")
        if pest_res is None:
            c.check("多模态虫害字段（未启用或降级时的契约）",
                    degraded_pest or not pest_ready,
                    f"pest=null，degraded.pest={degraded_pest}，"
                    f"health 里 available={pest_ready}（两者必居其一，否则是丢了结果）")
        else:
            dets = pest_res.get("detections") or []
            missing_pest = [k for k in ("detections", "count", "image_size", "backend", "elapsed_ms")
                            if k not in pest_res]
            # count 必须与列表长度自洽、image_size 必须是 [w, h] —— 前端就是靠
            # image_size 把框换算成百分比定位的，少一个数覆盖层就会整体错位
            shape_ok = (isinstance(pest_res.get("detections"), list)
                        and isinstance(pest_res.get("count"), int)
                        and pest_res["count"] == len(dets)
                        and len(pest_res.get("image_size") or []) == 2
                        and all(isinstance(x, (int, float)) for x in pest_res["image_size"]))
            box_ok = all(isinstance(x.get("bbox"), list) and len(x["bbox"]) == 4
                         and all(isinstance(v, (int, float)) for v in x["bbox"])
                         and x.get("label_cn") for x in dets)
            c.check("多模态虫害字段（结构完整性）",
                    not missing_pest and shape_ok and box_ok,
                    f"检出 {len(dets)} 个框，backend={pest_res.get('backend')}，"
                    f"image_size={pest_res.get('image_size')}，"
                    f"耗时={pest_res.get('elapsed_ms')}ms，"
                    + ("、".join(f"{x['label_cn']}({x['confidence']:.2f})" for x in dets[:3]) or "阈值以上无检出")
                    + (f"；缺字段 {missing_pest}" if missing_pest else ""))
            # 虫害结果**绝不能**进 candidates：那是防大模型幻觉的闭集（MAX_CANDIDATES=8），
            # 塞进 102 类昆虫会同时挤掉病害候选、污染拒答语义
            pest_names = {x.get("label_cn") for x in dets}
            cand_names = {cb.get("label_cn") for cb in (d.get("candidates") or [])}
            c.check("虫害结果未混入 candidates（闭集防幻觉）",
                    not (pest_names & cand_names),
                    f"虫害 {sorted(pest_names) or '—'} vs 候选 {sorted(cand_names) or '—'}")
            # report.pest_findings 必须与 pest 同进同退：有检测结果才可能有这个字段
            c.check("report.pest_findings 与 pest 字段同进同退",
                    isinstance(rep.get("pest_findings"), list),
                    f"pest_findings = {rep.get('pest_findings')}")

        # 8c 虫害检测**真的出框**的那条路（上面用的是病灶图，通常 0 检出）
        #
        # 为什么值得单独跑一次：用病灶图那一次只覆盖了「跑了、没检出」这一态，
        # 而 response_model 静默丢字段这类 bug 恰恰只在**有内容**时才现形 ——
        # 空列表丢了也还是空列表，什么都看不出来。
        # 这里换一张 IP102 验证集原图。它是 600×333 的**非正方形**图，在这套权重下
        # 稳定检出 2 个框：正方形图上宽高写反了看不出来，非正方形才钉得住。
        # 严格断言 count >= 1 是可以的 —— 同一份 ONNX + 同一组阈值，CPU 推理是确定性的，
        # scripts/19 也已把这份 ONNX 与 torch 前向对齐过。真掉到 0，就是模型换了，该报出来。
        root = Path(__file__).resolve().parents[1]
        pest_img = root / "data/pest/images/val/IP016000663.jpg"
        if not pest_img.exists():
            logger.warning("跳过虫害出框测试：未找到 %s", pest_img)
        else:
            from PIL import Image

            with Image.open(pest_img) as im:
                expect_size = [im.width, im.height]   # [宽, 高]，与服务端同一口径
            code, body = c.post_multipart(
                "/api/diagnose/multimodal",
                {"text": "叶子上有虫，还有虫粪", "dialect": "mandarin"},
                {"image": (pest_img.name, pest_img.read_bytes(), "image/jpeg")},
            )
            d = (body or {}).get("data") or {}
            pests = d.get("pest") or {}
            dets = pests.get("detections") or []
            c.check("POST /api/diagnose/multimodal（虫害出框）",
                    code == 200 and len(dets) >= 1,
                    f"检出 {len(dets)} 个框：" + "、".join(
                        f"{x.get('label_cn')}({x.get('confidence')})" for x in dets[:3])
                    + (f"；degraded.pest={(d.get('degraded') or {}).get('pest')}" if not dets else ""))

            # 框的坐标必须落在**原图**范围内：服务端报的 image_size 若与实际图不符，
            # 前端按百分比画的覆盖层会整体错位 —— 而这件事没有浏览器就看不出来。
            iw, ih = (pests.get("image_size") or [0, 0])[:2]
            in_bounds = all(
                len(x.get("bbox") or []) == 4
                and 0 <= x["bbox"][0] < x["bbox"][2] <= iw
                and 0 <= x["bbox"][1] < x["bbox"][3] <= ih
                for x in dets
            )
            c.check("虫害框落在原图范围内（覆盖层对齐的前提）",
                    pests.get("image_size") == expect_size and in_bounds,
                    f"服务端 image_size={pests.get('image_size')}，实际图 {expect_size}；"
                    + "框 " + str([[round(v) for v in x["bbox"]] for x in dets]))

            # 阈值字段必须活下来：schema 漏声明时它是第一个被静默吃掉的
            # （丢了前端会显示「本次图片在检测阈值 0% 以上未发现虫害」这种像 bug 的话）
            c.check("虫害响应带着本次实际使用的阈值",
                    isinstance(pests.get("confidence_threshold"), (int, float))
                    and 0 < pests["confidence_threshold"] < 1,
                    f"conf={pests.get('confidence_threshold')} iou={pests.get('iou_threshold')}")

            # 检出虫害 ⇒ 主结论必须是虫害。这条用例用的正是当初复现缺陷的那张图
            # （IP016000663.jpg：检出金针虫，报告却写「小麦斑枯病」）——
            # 所以这条断言等于把那次修复钉死在冒烟测试里。
            rep = d.get("report") or {}
            prim = rep.get("primary") or {}
            c.check("检出虫害时主结论切到虫害（不再硬报病害）",
                    prim.get("kind") == "pest"
                    and prim.get("name") == dets[0].get("label_cn"),
                    f"primary={prim}；检出首项={dets[0].get('label_cn')}")
            sec_titles = [s.get("title") for s in (rep.get("treatment_sections") or [])]
            c.check("虫害当主结论时防治方案跟着换成虫害的",
                    bool(sec_titles) and "发病规律与判断依据" not in sec_titles
                    and any("防治" in (t or "") for t in sec_titles),
                    f"方案小节={sec_titles}")
            applied = {cb.get("label_cn") for cb in (d.get("candidates") or [])}
            c.check("切换主结论未污染 candidates 闭集",
                    dets[0].get("label_cn") not in applied,
                    f"candidates={applied}")
    else:
        logger.warning("跳过图像相关测试：未找到 Sample/297/297/*.JPG")

    # 9 参数校验（应当被拒绝）
    code, body = c.post_multipart("/api/diagnose/multimodal", {"text": ""}, {})
    c.check("空输入被拒绝（参数校验）", code == 400 and (body or {}).get("code") == 400,
            f"HTTP {code} msg={(body or {}).get('msg')}")

    code, body = c.get("/api/map/pest_top3?season=badseason")
    c.check("非法季节被拒绝", code == 400, f"HTTP {code} msg={(body or {}).get('msg')}")

    code, body = c.get("/api/library/not-exist-id")
    c.check("不存在的图书 id 返回 404", code == 404, f"HTTP {code} msg={(body or {}).get('msg')}")

    # ---------------- 阶段 7：账号与我的农田（7.12 要求的四条全链路）----------------
    #
    # 用**独立的 Client**：主客户端 c 一路是匿名的（前面那些公开接口就是靠它测的），
    # 这里注册登录会往 Cookie 罐里写会话，混用会让「未登录 401」那几条失去意义。
    import time as _time

    stamp = str(int(_time.time()))[-6:]
    a, b = Client(args.base), Client(args.base)

    code, body = a.post_json("/api/auth/register", {
        "username": f"smoke{stamp}a", "email": f"smoke{stamp}a@example.com",
        "password": "abc12345", "nickname": "冒烟甲"})
    registered = code == 200 and (body or {}).get("code") == 0
    c.check("注册 A（自动登录 + 预置示例田）", registered, (body or {}).get("msg", ""))

    if registered:
        code, body = a.get("/api/farm/fields")
        fields = ((body or {}).get("data") or {}).get("fields") or []
        c.check("A 有 3 条示例田", len(fields) == 3,
                "、".join(f"{f['crop']}({f['name']})" for f in fields))

        rice = next((f for f in fields if f["crop"] == "水稻"), None)
        if rice:
            # 建田（再建一块，覆盖 POST /fields）
            code, body = a.post_json("/api/farm/fields", {
                "name": "冒烟测试田", "crop": "玉米", "area_mu": 1.5,
                "province": "湖北省", "city": "武汉市", "lng": 114.3, "lat": 30.58,
                "sow_date": "2026-07-31"})
            new_id = ((body or {}).get("data") or {}).get("id")
            c.check("新建田块", code == 200 and bool(new_id), f"id={new_id}")

            # 记一笔
            code, body = a.post_json("/api/farm/logs", {
                "field_id": rice["id"], "type": "fertilize", "date": "2026-10-08",
                "item": "尿素", "amount": "20斤", "source": "manual"})
            c.check("记一笔农事", code == 200 and (body or {}).get("code") == 0,
                    (body or {}).get("msg", ""))

            # 今天该干啥：一次拿齐，结构必须完整
            code, body = a.get(f"/api/farm/today?field_id={rice['id']}")
            today = (body or {}).get("data") or {}
            growth = today.get("growth") or {}
            weather = today.get("weather") or {}
            harvest = today.get("harvest") or {}
            need = ("field", "weather", "growth", "harvest", "reminders", "reminders_notes",
                    "logs", "narrative", "narrative_why", "narrative_source")
            c.check("今天该干啥（面板结构完整）", code == 200 and all(k in today for k in need),
                    "缺：" + "、".join(k for k in need if k not in today))
            c.check("积温与阶段算出来了",
                    bool(growth.get("stage")) and growth.get("gdd_cum") is not None,
                    f"{growth.get('stage')} 积温{growth.get('gdd_cum')} 进度{growth.get('progress')}")
            c.check("收获预测给出方法与依据",
                    harvest.get("method") in ("xgboost", "gdd_fallback") and bool(harvest.get("basis")),
                    f"method={harvest.get('method')} 样本{harvest.get('sample_count')}/" +
                    f"{harvest.get('sample_needed')}")
            # 天气结构一致（可用与降级两种情况下字段都一样，只是 source 不同）
            weather_keys = ("days", "source", "source_note", "degraded", "degraded_reason")
            c.check("天气结构完整（可用/降级同构）",
                    all(k in weather for k in weather_keys) and
                    weather.get("source") in ("forecast", "normal", "none"),
                    f"source={weather.get('source')} 降级={weather.get('degraded')}")
            if weather.get("source") == "normal":
                c.check("气候平均值不冒充预报", "不是预报" in (weather.get("source_note") or ""),
                        weather.get("source_note", ""))

            # 3D 参数：12 个一个都不能少
            code, body = a.get(f"/api/farm/model/{rice['id']}")
            model = (body or {}).get("data") or {}
            model_keys = ("height", "leafCount", "leafColor", "leafDroop", "stemColor",
                          "earColor", "earDroop", "dryLeafRatio", "hasEar", "earSize",
                          "hasBlackLayer", "milkLineGone")
            c.check("3D 参数 12 项齐全",
                    code == 200 and all(k in model for k in model_keys),
                    f"高{model.get('height')}cm {model.get('leafCount')}叶 " +
                    f"{model.get('leafColorName')} 有穗={model.get('hasEar')}")

            # 语音解析（不落库）
            code, body = a.post_json("/api/farm/voice_parse",
                                     {"text": "昨天给冒烟测试田施了20斤尿素"})
            parsed = (body or {}).get("data") or {}
            c.check("语音解析出结构化字段", code == 200 and bool((parsed.get("fields") or {}).get("type")),
                    f"type={(parsed.get('fields') or {}).get('type')} " +
                    f"田={(parsed.get('fields') or {}).get('field_name')} " +
                    f"数量={(parsed.get('fields') or {}).get('amount')}")

            # 挂诊断（带一张极小的 PNG）
            png = bytes.fromhex(
                "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
                "1f15c4890000000a49444154789c63000100000500010d0a2db4000000"
                "0049454e44ae426082")
            import base64 as _b64
            code, body = a.post_json("/api/farm/attach_diagnosis", {
                "field_id": rice["id"], "date": "2026-10-05", "label": "白粉病",
                "confidence": 0.9, "image_base64": _b64.b64encode(png).decode()})
            c.check("挂诊断入档", code == 200 and (body or {}).get("code") == 0,
                    f"path={((body or {}).get('data') or {}).get('image_path', '')}")

            # 我收了
            code, body = a.post_json("/api/farm/harvest_mark",
                                     {"field_id": rice["id"], "harvest_date": ""})
            done = (body or {}).get("data") or {}
            c.check("我收了（写收获记录 + 算特征）",
                    code == 200 and bool(done.get("harvest_id")),
                    f"生长期{done.get('actual_days')}天 积温{done.get('gdd_at_harvest')} " +
                    f"样本{done.get('sample_count')}/30")

    # 越权：B 碰不到 A 的田
    code, body = b.post_json("/api/auth/register", {
        "username": f"smoke{stamp}b", "email": f"smoke{stamp}b@example.com",
        "password": "abc12345"})
    c.check("注册 B", code == 200 and (body or {}).get("code") == 0, (body or {}).get("msg", ""))

    if registered and rice:
        code, body = b.get(f"/api/farm/today?field_id={rice['id']}")
        c.check("B 读 A 的田 → 404", code == 404 and (body or {}).get("code") == 404,
                f"HTTP {code}")
        code, body = b.get(f"/api/farm/model/{rice['id']}")
        c.check("B 看 A 的 3D → 404", code == 404, f"HTTP {code}")

    # 未登录 / 登出
    anon = Client(args.base)
    code, body = anon.get("/api/farm/fields")
    c.check("未登录访问 farm → 401", code == 401, f"HTTP {code} msg={(body or {}).get('msg')}")
    code, body = anon.get("/api/library?size=1")
    c.check("未登录访问知识库 → 200", code == 200 and (body or {}).get("code") == 0, f"HTTP {code}")

    code, body = a.post_json("/api/auth/logout", {})
    c.check("A 登出", code == 200 and (body or {}).get("code") == 0, (body or {}).get("msg", ""))
    code, body = a.get("/api/farm/fields")
    c.check("登出后 farm → 401", code == 401, f"HTTP {code}")

    # 9b 技能接口
    code, body = c.get("/api/skills")
    d = (body or {}).get("data") or {}
    names = [s.get("name") for s in (d.get("skills") or [])]
    c.check("GET /api/skills", code == 200 and set(names) == {"crop-diagnosis", "farm-growth"},
            f"{d.get('count')} 个技能：{names}")

    code, body = c.get("/api/skills/crop-diagnosis")
    d = (body or {}).get("data") or {}
    c.check("GET /api/skills/crop-diagnosis",
            code == 200 and "input_schema" in d and "topk" in (d.get("input_schema") or {}).get("properties", {}),
            f"version={d.get('version')} available={d.get('available')}")

    code, body = c.post_json("/api/skills/farm-growth", {
        "field": {"id": "fld_smoke", "crop": "水稻", "sow_date": "2026-05-01"},
        "today": "2026-05-25",
        "include": ["growth", "model", "harvest"],
        "history": [
            {"date": f"2026-05-{day:02d}", "tmax": 30, "tmin": 20}
            for day in range(1, 26)
        ],
    })
    d = (body or {}).get("data") or {}
    r = d.get("result") or {}
    c.check("POST /api/skills/farm-growth",
            code == 200 and (r.get("growth") or {}).get("gdd_cum", 0) > 0
            and (r.get("model") or {}).get("height", 0) > 0,
            f"stage={(r.get('growth') or {}).get('stage')} "
            f"height={(r.get('model') or {}).get('height')} "
            f"method={(r.get('harvest') or {}).get('method')}")

    code, body = c.post_json("/api/skills/not-exist", {})
    c.check("未知技能 → 404", code == 404, f"HTTP {code}")
    code, body = c.post_json("/api/skills/crop-diagnosis", {"image_path": "leaf.jpg"})
    c.check("技能 HTTP 拒绝本地路径 → 400", code == 400, f"HTTP {code}")

    # 9c 来源标注
    code, body = c.get("/api/library/lib_pest_0")
    d = (body or {}).get("data") or {}
    notes = d.get("source_notes") or []
    c.check("知识卡片详情的登记来源",
            code == 200 and notes and notes[0].get("organization") == "农业农村部农药管理司",
            f"source_notes={notes} image_source_notes={d.get('image_source_notes')}")
    code, body = c.get("/api/library/lib_care_萝卜")
    d = (body or {}).get("data") or {}
    c.check("养护卡不误标登记来源", code == 200 and not (d.get("source_notes") or []),
            f"source_notes={d.get('source_notes')}")

    # 10 前端页面
    for page in ("/", "/diagnose.html", "/library.html", "/policy.html",
                 "/farm.html", "/skills.html", "/login.html", "/register.html",
                 "/profile.html", "/admin.html"):
        try:
            with urllib.request.urlopen(c.base + page, timeout=15) as r:
                html = r.read().decode("utf-8", "ignore")
            c.check(f"GET {page}", r.status == 200 and "<html" in html.lower(), f"{len(html)} 字节")
        except Exception as e:  # noqa: BLE001
            c.check(f"GET {page}", False, str(e))

    logger.info("=" * 68)
    if c.failures:
        logger.error("冒烟测试未全部通过，失败项 %d 个：%s", len(c.failures), "、".join(c.failures))
        return 1
    logger.info("冒烟测试全部通过")
    logger.info("=" * 68)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
