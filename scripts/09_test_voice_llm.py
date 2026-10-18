#!/usr/bin/env python
"""任务 3.1 / 3.2 / 3.3 / 3.4 自检 —— 语音、大模型、方言三处能力在没有 API Key 时能否正常降级。

做四件事：
    1. 导出农业专业词库（knowledge/agri_lexicon.txt）并演示术语纠错
    2. 列出八大方言及其 ASR/TTS 参数，验证运行时切换与「无独立模型时回退」的标注
    3. 调用语音能力（Mock 或真实）做一次转写与合成，确认返回结构
    4. 用一段模拟的多模态上下文跑一次报告生成，确认 report 字段齐全

用法：
    python scripts/09_test_voice_llm.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.common.logging_utils import setup_logger          # noqa: E402
from src.common.paths import path_of                       # noqa: E402
from src.dialect.config import asr_params, list_dialects, tts_params  # noqa: E402

logger = setup_logger("agri.selftest")


def check_lexicon() -> None:
    from src.voice.lexicon import agri_terms, export_lexicon, find_terms

    logger.info("=" * 62)
    logger.info("① 农业专业词库（任务 3.2）")
    n, path = export_lexicon()
    logger.info("词库规模：%d 个词；示例：%s", n, "、".join(agri_terms()[:10]))

    samples = [
        "我发现玉米叶子上有褐色的斑点，像是大斑病",
        "黄瓜叶子背面有白色的粉末，可能是白粉病",
        "用点杀菌剂和叶面肥试试",
    ]
    for s in samples:
        hits = find_terms(s)
        logger.info("  术语抽取：%s", s)
        logger.info("            -> 命中 %d 个：%s", len(hits), "、".join(hits) or "（无）")
    logger.info("说明：这里只做精确术语抽取，不做模糊改写（模糊改写会把正常语句改坏，"
                "识别侧的优化交由百度控制台的自定义词库完成）。")


def check_dialects() -> None:
    logger.info("=" * 62)
    logger.info("② 八大方言配置（任务 3.4）")
    dialects = list_dialects()
    logger.info("共 %d 种方言：", len(dialects))
    for d in dialects:
        a = asr_params(d["key"])
        t = tts_params(d["key"])
        flag = "ASR独立模型" if a["asr_supported"] else "ASR回退普通话"
        logger.info("   %-10s %-6s | ASR dev_pid=%-6s %-14s | TTS per=%-6s %s",
                    d["key"], d["name"], a["dev_pid"], flag, t["per"], t["voice_name"])

    fallback = [d for d in dialects if not d["asr_supported"]]
    logger.info("其中 %d 种方言在百度短语音识别 REST 接口没有独立模型，"
                "已按回退普通话处理（不假装支持）：%s",
                len(fallback), "、".join(d["name"] for d in fallback))


def check_voice() -> None:
    from src.voice.factory import current_mode, get_voice

    logger.info("=" * 62)
    logger.info("③ 语音能力（任务 3.1）—— 当前模式：%s", current_mode())
    voice = get_voice()
    logger.info("实现类：%s，%s", type(voice).__name__, json.dumps(voice.describe(), ensure_ascii=False))

    asr = voice.transcribe(b"\x00" * 1024, dialect="sichuan")
    logger.info("转写返回字段：%s", sorted(asr.keys()))
    logger.info("   text=%r provider=%s mock=%s fallback=%s",
                asr.get("text"), asr.get("provider"), asr.get("mock"), asr.get("fallback"))
    if asr.get("message"):
        logger.info("   说明：%s", asr["message"])

    tts = voice.synthesize("玉米大斑病的防治方法：选用抗病品种，合理轮作。", dialect="cantonese")
    logger.info("合成返回字段：%s", sorted(tts.keys()))
    logger.info("   provider=%s mock=%s 音频字节=%s",
                tts.get("provider"), tts.get("mock"),
                len(tts["audio_bytes"]) if tts.get("audio_bytes") else 0)
    if tts.get("message"):
        logger.info("   说明：%s", tts["message"])


def check_llm() -> None:
    from src.llm.factory import get_llm
    from src.llm.mock_provider import MockLLM  # noqa: F401

    logger.info("=" * 62)
    logger.info("④ 大模型诊断报告（任务 3.3）")
    llm = get_llm()
    logger.info("实现类：%s，%s", type(llm).__name__, json.dumps(llm.describe(), ensure_ascii=False))

    label_map = json.loads(path_of("label_map").read_text(encoding="utf-8"))
    entry = next(e for e in label_map["index_to_label"].values()
                 if e["label_cn"] == "玉米大斑病")

    context = {
        "image_result": {
            "top1": {"label_cn": entry["label_cn"], "crop": entry["crop"],
                     "condition": entry["condition"], "pathogen_type": entry["pathogen_type"],
                     "confidence": 0.87},
            "topk": [{"label_cn": entry["label_cn"], "confidence": 0.87}],
        },
        "asr_text": "我家玉米叶子上有长条形的褐色斑块",
        "user_text": "叶片上出现梭形大斑，边缘褐色中间灰白",
        "candidates": [{"label_cn": entry["label_cn"], "crop": entry["crop"],
                        "condition": entry["condition"], "pathogen_type": entry["pathogen_type"],
                        "confidence": 0.87}],
        "retrieved": [],
        "dialect_name": "四川话",
    }

    report = llm.generate_report(context)
    required = ["disease_name", "confidence", "symptom_match", "treatment_sections",
                "treatment_plan", "medication_reminder", "safety_note", "summary", "source"]
    missing = [k for k in required if k not in report]
    logger.info("报告字段完整性：%s", "全部齐全 ✓" if not missing else f"缺少 {missing}")
    logger.info("   disease_name = %s", report.get("disease_name"))
    logger.info("   confidence   = %s", report.get("confidence"))
    logger.info("   summary      = %s", report.get("summary"))
    logger.info("   symptom_match= %s", str(report.get("symptom_match"))[:120])
    logger.info("   防治小节 %d 个：%s", len(report.get("treatment_sections", [])),
                "、".join(s["title"] for s in report.get("treatment_sections", [])))
    logger.info("   medication   = %s", str(report.get("medication_reminder"))[:100])
    logger.info("   source       = %s", report.get("source"))

    if report.get("source") == "mock":
        logger.info("提示：当前为降级 Mock 报告；在 .env 填入 DEEPSEEK_API_KEY 后重启即走真实大模型。")


def main() -> int:
    check_lexicon()
    check_dialects()
    check_voice()
    check_llm()
    logger.info("=" * 62)
    logger.info("阶段 3 自检完成")
    logger.info("=" * 62)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
