"""任务 3.2 —— 农业专业词库，用于优化方言/口音语音识别准确率。

两部分：

1. **导出可上传的词库文件**（主要交付物）：knowledge/agri_lexicon.txt（一行一个词）。
   百度短语音识别的自定义词库/自训练模型必须在控制台配置，接口侧不能临时传参，
   因此这里的产物就是"拿去控制台配置"的清单；
   若申请了自训练模型，把模型 id 填到 .env 的 BAIDU_LM_ID 即可在调用时带上。
   —— 这才是真正在解码阶段生效、能提升识别准确率的机制。

2. **运行时术语抽取**：从识别文本中精确匹配出农业专业术语（作物名、病害名等），
   交给 src/llm/fusion.py 作为额外的候选病害，提高纯语音输入时的诊断命中率。

   注意：这里刻意只做精确匹配，不做模糊改写。曾尝试用编辑距离把「大班病」改成
   「大斑病」，但在没有分词器和 ASR n-best 的情况下，滑动窗口会把正常语句切坏 ——
   实测「现玉米」被改成「玉米」、「斑点，」被改成「斑点病」、「点杀菌剂」被改成
   「杀菌剂」，改错的代价远大于收益，因此放弃该方案。

词表来源：data/label_map.json（271 个类别名 → 47 个作物 + 250 个病害描述）
         + 一份人工整理的农业常用语（施药、田间管理、症状描述等）。
"""

from __future__ import annotations

import json
from functools import lru_cache

from src.common.io_utils import write_text
from src.common.logging_utils import setup_logger
from src.common.paths import path_of

logger = setup_logger("agri.lexicon")

# 农业场景常用语（人工整理，用于补足类别名覆盖不到的日常说法）
GENERAL_TERMS: list[str] = [
    # 田间管理与操作
    "打药", "喷药", "施药", "追肥", "底肥", "基肥", "叶面肥", "复合肥", "有机肥", "尿素",
    "灌水", "滴灌", "漫灌", "排水", "整地", "深耕", "轮作", "间作", "套种", "育苗",
    "移栽", "定植", "整枝", "打杈", "摘心", "疏花", "疏果", "套袋", "覆膜", "闷棚",
    # 症状描述
    "叶片发黄", "叶子发黄", "叶尖干枯", "叶片卷曲", "卷叶", "皱缩", "萎蔫", "枯萎",
    "烂根", "烂果", "根腐", "茎秆发黑", "叶背霉层", "白色粉末", "粉状物", "锈色孢子",
    "水渍状病斑", "褐色斑点", "黑色斑点", "同心轮纹", "枯斑", "穿孔", "落花落果",
    "植株矮化", "长势弱", "徒长", "僵苗", "裂果", "日灼", "冻害", "药害", "肥害",
    # 防治手段
    "抗病品种", "种子处理", "拌种", "包衣", "轮换用药", "安全间隔期", "生物防治",
    "天敌", "黄板", "蓝板", "诱虫灯", "性诱剂", "防虫网", "遮阳网", "绿色防控",
    "统防统治", "测土配方", "水肥一体化", "秸秆还田", "石灰消毒",
    # 常见药剂类别（只到类别，不给具体品种与用量）
    "杀菌剂", "杀虫剂", "除草剂", "杀螨剂", "杀线虫剂", "生物菌剂", "抗病毒制剂",
]

_MIN_TERM_LEN = 2


@lru_cache(maxsize=1)
def agri_terms() -> list[str]:
    """全部农业专业术语（去重、按长度降序 —— 抽取时优先匹配长词）。"""
    terms: set[str] = set(GENERAL_TERMS)

    try:
        label_map = json.loads(path_of("label_map").read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.warning("未找到 label_map.json，词库仅包含通用农业用语；"
                       "请先运行 python scripts/03_build_label_map.py")
        label_map = {}

    for entry in label_map.get("index_to_label", {}).values():
        for key in ("label_cn", "crop", "condition", "pathogen_type"):
            value = (entry.get(key) or "").strip()
            if len(value) >= _MIN_TERM_LEN:
                terms.add(value)

    return sorted((t for t in terms if len(t) >= _MIN_TERM_LEN), key=len, reverse=True)


def find_terms(text: str, *, limit: int = 12) -> list[str]:
    """从识别文本中抽取出命中的农业专业术语（精确子串匹配，长词优先）。

    用途：把口语描述里提到的作物/病害名提取出来，交给
    src/llm/fusion.py 作为额外的候选病害，提高纯语音输入时的诊断命中率。

    这里刻意**只做精确匹配，不做模糊改写**。原因：
        曾尝试用编辑距离/相似度把「大班病」改成「大斑病」，但在没有分词器与
        ASR n-best 的情况下，滑动窗口会把正常语句切坏 —— 实测「现玉米」被改成
        「玉米」、「斑点，」被改成「斑点病」、「点杀菌剂」被改成「杀菌剂」，
        改错的代价远大于收益。真正的识别侧优化应通过百度控制台的自定义词库
        或自训练平台完成（见 export_lexicon），那是官方支持的、在解码阶段生效的机制。
    """
    if not text:
        return []

    found: list[str] = []
    for term in agri_terms():          # 已按长度降序
        if term in text and not any(term in f for f in found):
            found.append(term)
        if len(found) >= limit:
            break
    return found


def export_lexicon() -> tuple[int, str]:
    """导出可上传到百度控制台的词库文件（一行一词）。返回 (词数, 文件路径)。

    这是任务 3.2「农业专业词库」的主要交付物：百度短语音识别的自定义词库必须
    在控制台配置（接口侧不能临时传参），本文件就是拿去上传的清单。
    若申请了自训练模型，把模型 id 填到 .env 的 BAIDU_LM_ID，调用时会自动带上。
    """
    terms = agri_terms()
    path = path_of("knowledge_dir") / "agri_lexicon.txt"
    write_text(path, "\n".join(terms) + "\n")
    logger.info("已导出农业专业词库：%d 个词 -> %s", len(terms), path)
    logger.info("　在百度智能云控制台「语音识别 - 自定义词库/自训练平台」上传该文件；"
                "若拿到模型 id，填入 .env 的 BAIDU_LM_ID 即可在调用时生效。")
    return len(terms), str(path)
