"""任务 1.6 —— 采集 2026 年官方惠农政策条目（标题、核心内容、发布单位、发布日期）。

数据源为农业农村部官网已核验的 10 个链接（实测均返回 200）：
    1. 《2026年中央财政强农惠农富农政策清单》（16项）  农业农村部计划财务司  2026-03-25
    2. 《2026年中央财政强农惠农富农政策清单2》（23项） 农业农村部计划财务司  2026-07-03
    3. 《新型农业经营主体提质增效带动小农户增收行动方案》 农村合作经济指导司  2026-05-06
    4. 《关于加快农业保险高质量发展的实施方案》        财政部等四部门        2026-09-08
    5. 《坚持农业农村优先发展完善乡村振兴投入机制实施方案》 农业农村部等六部门  2026-09-07
    6. 《…锚定农业农村现代化扎实推进乡村全面振兴的意见》的实施意见 农业农村部  2026-02-06
    7. 《关于加强农业科普工作的意见》                  三部委办公厅          2026-05-07
    8. 《关于集中整治禁限用农药非法生产经营使用的通知》 农业农村部办公厅      2026-03-19
    9. 《中央财政统筹安排12.5亿元积极支持冬小麦促弱转壮》 计划财务司          2026-03-17
   10. 《当前大豆应对高温干旱技术意见》                种植业管理司          2026-09-11

摘要怎么来的：全部从抓到的原文里提取，不做凭空撰写 ——
    · 导语：正文中页面控件之后、第一个条目编号之前的收尾句
    · 条目：按页面实际的编号层级提取（见 _extract_structure）
    · 核心内容摘要 = 导语 +「主要内容包括：」+ 条目/章节标题（截断）
    · 要点（points）：条目名 + 该条正文的第一句原话，逐字照抄不改写
      （见 extract_points；超过 110 字在逗号处收尾并加「……」，只标截断不改字）
因此摘要与要点都可逐字回溯到官方原文，source_url 留档以便核对，前端标题即可点开原文。

页面结构实测（这决定了下面的解析方式）：站内是**两套模板**，正文起点不同，
认错起点会把整块元数据当成正文开头（见 extract_body）：
    · 滚动新闻页（/gdxw/、/zcjd/）：有「打印本页」，正文紧跟在它之后。
    · 政务公开页（/govpublic/）：**没有**「打印本页」，正文前有一大段
      「索引号…信息所属单位…信息名称…文号…生效日期…发布日期…内容概述…」元数据块，
      正文起点取「字体：[大中小]」之后。

    · 正文被大量行内标签切碎（如「2026」「年」被分成两个节点），
      因此必须先把标签去掉、把所有空白与全角缩进去掉，拼成一整段文本再定位，
      而不是按行解析 —— 按行解析会把「二、提升」切成半截标题。
    · A16 用「一、耕地地力保护补贴。」一级编号，共 16 条（与清单标注一致）
    · A23 用「一、粮油生产技术推广应用」+「1.粮油等重点作物绿色高产高效行动。」两级，
      叶子条目共 23 条（与清单标注一致）
    · XD  用「一、总体要求」+「（一）完善家庭农场分类培育机制」两级
    · 无编号的新闻稿/技术意见（如 9、10）条目数为 0，摘要即为正文开头两句

抓取失败时不抛异常中断流程：写出带 demo_data=true 的回退条目，接口照常返回。

产出：data/policy_list.json
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

from src.common.io_utils import write_json
from src.common.logging_utils import setup_logger
from src.common.paths import path_of

logger = setup_logger("agri.policy")

POLICY_TYPES = [
    {"key": "subsidy", "name": "补贴补助"},
    {"key": "subject", "name": "经营主体"},
    {"key": "service", "name": "技术服务"},
    {"key": "other", "name": "其他"},
]

_TYPE_KEYWORDS = {
    "subsidy": ["补贴", "补助", "贴息", "保险", "资金", "奖励", "转移支付"],
    "subject": ["经营主体", "合作社", "家庭农场", "社会化服务", "龙头企业", "小农户"],
    "service": ["技术", "推广", "培训", "指导", "示范"],
}

# 编号层级（中文序号的字符集，最多到「二十」足够覆盖此类公文）
_CN = "一二三四五六七八九十"
_RE_L1 = re.compile(rf"([{_CN}]{{1,3}})、([^。．.、；]{{2,40}}?)(?=[{_CN}]{{1,3}}、|\d{{1,2}}[.．]|[（(]|。|．|\.|$)")
# 末尾的 (?!\d) 是必需的：正文里「12.5亿元」「1.5万亩」这类小数否则会被当成
# 「第 12 条」，凭空多出条目、还会把导语从数字中间截断。
_RE_L2 = re.compile(r"(?<![\d.])(\d{1,2})[.．](?!\d)([^。．.、；]{2,50}?)(?=[（(]|。|．|\.|$)")
_RE_L3 = re.compile(rf"[（(]([{_CN}]{{1,3}})[）)]([^。．.、；]{{2,40}}?)(?=[（(]|。|．|\.|$)")

# 仅用于定位「第一个编号出现在哪里」——不带标题长度约束，因此不会漏掉
# 「一、总体要求以习近平新时代…为指导」这类长句开头的一级标题。
_RE_ANY_MARKER = re.compile(rf"[{_CN}]{{1,3}}、|[（(][{_CN}]{{1,3}}[）)]|(?<![\d.])\d{{1,2}}[.．](?!\d)")

# 政务公开页（/govpublic/）的正文起点标志——「字体：[大中小]」这类字号切换控件，
# 它出现在正文最前面、且位于「索引号…内容概述」元数据块之后，是最好的切点。
_RE_FONT_SWITCH = re.compile(r"字体[:：]\[[^\]]{0,12}\]")


@dataclass
class PolicySource:
    pid: str
    url: str
    title: str
    publish_unit: str
    publish_date: str          # 与任务清单一致的写法，如 "2026年3月"
    publish_date_iso: str      # 页面实际日期，如 "2026-03-25"
    expected_items: int        # 预期条目数（0 表示该页本就无编号条目）；对不上时告警，用于发现页面改版
    items: list[dict] = field(default_factory=list)
    sections: list[dict] = field(default_factory=list)


SOURCES: list[PolicySource] = [
    PolicySource(
        pid="pol_001",
        url="https://jcs.moa.gov.cn/gdxw/202603/t20260325_6482637.htm",
        title="2026年中央财政强农惠农富农政策清单（16项）",
        publish_unit="农业农村部计划财务司",
        publish_date="2026年3月",
        publish_date_iso="2026-03-25",
        expected_items=16,
    ),
    PolicySource(
        pid="pol_002",
        url="https://jcs.moa.gov.cn/zcjd/202608/t20260804_6486423.htm",
        title="2026年中央财政强农惠农富农政策清单2（23项）",
        publish_unit="农业农村部计划财务司",
        publish_date="2026年7月",
        publish_date_iso="2026-07-03",
        expected_items=23,
    ),
    PolicySource(
        pid="pol_003",
        url="https://hzjjs.moa.gov.cn/gzdt/202605/t20260506_6483937.htm",
        title="新型农业经营主体提质增效带动小农户增收行动方案",
        publish_unit="农业农村部农村合作经济指导司",
        publish_date="2026年5月",
        publish_date_iso="2026-05-06",
        expected_items=0,
    ),
    # ---- 以下 7 条为「政策再多一点」的扩容来源，全部实测 200 且正文可解析 ----
    PolicySource(
        pid="pol_004",
        url="https://www.moa.gov.cn/govpublic/CWS/202609/t20260915_6487680.htm",
        title="关于印发《关于加快农业保险高质量发展的实施方案》的通知（财金〔2026〕88号）",
        publish_unit="财政部、农业农村部、金融监管总局、国家林草局",
        publish_date="2026年9月",
        publish_date_iso="2026-09-08",
        expected_items=22,
    ),
    PolicySource(
        pid="pol_005",
        url="https://www.moa.gov.cn/govpublic/CWS/202609/t20260907_6487329.htm",
        title="关于印发《坚持农业农村优先发展完善乡村振兴投入机制实施方案》的通知（农计财发〔2026〕9号）",
        publish_unit="农业农村部、中央农办、国家发展改革委、财政部、中国人民银行、金融监管总局",
        publish_date="2026年9月",
        publish_date_iso="2026-09-07",
        expected_items=10,
    ),
    PolicySource(
        pid="pol_006",
        url="https://www.moa.gov.cn/govpublic/FZJHS/202602/t20260206_6481380.htm",
        title="农业农村部关于落实《中共中央国务院关于锚定农业农村现代化扎实推进乡村全面振兴的意见》的实施意见（农发〔2026〕1号）",
        publish_unit="农业农村部",
        publish_date="2026年2月",
        publish_date_iso="2026-02-06",
        expected_items=40,
    ),
    PolicySource(
        pid="pol_007",
        url="https://www.moa.gov.cn/govpublic/KJJYS/202604/t20260429_6483775.htm",
        title="农业农村部办公厅科技部办公厅中国科协办公厅关于加强农业科普工作的意见（农办科〔2026〕3号）",
        publish_unit="农业农村部办公厅、科技部办公厅、中国科协办公厅",
        publish_date="2026年5月",
        publish_date_iso="2026-05-07",
        expected_items=10,
    ),
    PolicySource(
        pid="pol_008",
        url="https://www.moa.gov.cn/govpublic/ZZYGLS/202603/t20260323_6482528.htm",
        title="农业农村部办公厅关于集中整治禁限用农药非法生产经营使用的通知（农办农〔2026〕8号）",
        publish_unit="农业农村部办公厅",
        publish_date="2026年3月",
        publish_date_iso="2026-03-19",
        expected_items=10,
    ),
    PolicySource(
        pid="pol_009",
        url="https://jcs.moa.gov.cn/gdxw/202603/t20260317_6482324.htm",
        title="中央财政统筹安排12.5亿元积极支持冬小麦促弱转壮",
        publish_unit="农业农村部计划财务司",
        publish_date="2026年3月",
        publish_date_iso="2026-03-17",
        expected_items=0,
    ),
    PolicySource(
        pid="pol_010",
        url="https://jcs.moa.gov.cn/gdxw/202609/t20260911_6487596.htm",
        title="当前大豆应对高温干旱技术意见",
        publish_unit="农业农村部种植业管理司",
        publish_date="2026年9月",
        publish_date_iso="2026-09-11",
        expected_items=0,
    ),
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )
}


# ------------------------------------------------------------------ 抓取与清洗

def fetch_html(url: str, *, retries: int = 3, timeout: int = 30) -> str:
    import requests

    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, headers=HEADERS, timeout=timeout)
            r.raise_for_status()
            r.encoding = r.apparent_encoding or "utf-8"
            return r.text
        except Exception as e:  # noqa: BLE001
            last_err = e
            logger.warning("第 %d/%d 次抓取失败：%s（%s）", attempt, retries, url, e)
            time.sleep(1.5 * attempt)
    raise RuntimeError(f"抓取失败：{url}") from last_err


def html_to_text(html: str) -> str:
    """HTML -> 无空白的一整段纯文本。

    刻意不插入换行：正文被行内标签切得很碎，按行解析会得到「二、提升」这样的半截标题。
    中文正文里空白不承载语义，全部去掉反而最稳。
    """
    html = re.sub(r"(?s)<script.*?</script>", "", html)
    html = re.sub(r"(?s)<style.*?</style>", "", html)
    html = re.sub(r"(?s)<noscript.*?</noscript>", "", html)
    text = re.sub(r"<[^>]+>", "", html)
    for a, b in (("&nbsp;", ""), ("&#160;", ""), ("&amp;", "&"), ("&quot;", '"'), ("&#39;", "'")):
        text = text.replace(a, b)
    return re.sub(r"[\s　]+", "", text)


def extract_body(text: str) -> str:
    """截取正文：从页面控件之后、到页面尾部导航之前。

    站内是两套模板，必须都认：
      · 滚动新闻页（/gdxw/）：正文紧跟在「打印本页」之后
      · 政务公开页（/govpublic/）：**没有**「打印本页」，正文在「字体：[大中小]」之后，
        前面是一大段「索引号…信息所属单位…发布日期…内容概述…」的元数据块；
        起点认错的话，导语会把整块元数据当成正文开头。
    """
    start = text.find("打印本页")
    if start >= 0:
        body = text[start + len("打印本页"):]
    else:
        m = _RE_FONT_SWITCH.search(text)
        body = text[m.end():] if m else text

    stop_markers = ("扫一扫在手机打开当前页", "【字体：", "字体：[", "上一篇", "下一篇",
                    "网站地图", "关于我们", "主办单位", "承办单位", "网站标识码",
                    # 页脚导航：新闻稿页在正文之后直接跟一串站点链接，中间没有分隔，
                    # 不切掉的话会被当成正文句子摘成「要点」（实测 pol_009）。
                    "机关子站", "直属单位网站", "国务院各部门网站", "地方农业管理部门网站",
                    "京公网安备", "网站声明", "关于本网", "版权声明")
    for mk in stop_markers:
        pos = body.find(mk)
        if pos > 100:
            body = body[:pos]
    return body


def extract_meta(text: str, src: PolicySource) -> tuple[str, str]:
    """取发布日期与来源；取不到就用任务清单里已核验的值。

    两套模板的日期写法不同：
      · 滚动新闻页：`日期：2026-03-17`
      · 政务公开页：元数据块里的 `发布日期2026年09月08日`（无冒号）
    """
    date_iso, unit = src.publish_date_iso, src.publish_unit

    m = re.search(r"日期[:：]\s*(\d{4}-\d{2}-\d{2})", text)
    if m:
        date_iso = m.group(1)
    else:
        # 政务公开页优先认「发布日期」（元数据块里另有「生效日期」，别抓错）
        for label in ("发布日期", "发布时间", "生效日期"):
            m = re.search(rf"{label}[：:]?\s*(\d{{4}})年(\d{{1,2}})月(\d{{1,2}})日", text)
            if m:
                date_iso = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
                break

    m = re.search(r"来源[:：]([^【]{2,30}?)(?=【|$)", text)
    if m:
        raw = m.group(1).strip()
        if raw:
            unit = raw if raw.startswith("农业农村部") else f"农业农村部{raw}"
    return date_iso, unit


# ------------------------------------------------------------------ 条目解析

def _extract_structure(body: str) -> tuple[list[dict], list[dict], int]:
    """提取两级编号结构。

    返回 (sections, items, first_marker_pos)
        sections：一级编号（一、二、…）
        items   ：叶子条目 —— 有「1.」子项就用子项，否则用「（一）」，再否则用一级编号自身
        first_marker_pos：第一个编号在正文中的位置，用于切出导语
    """
    l1 = [{"index": i + 1, "title": t.strip()} for i, (_, t) in enumerate(_RE_L1.findall(body))]
    l2 = [{"index": int(n), "title": t.strip()} for n, t in _RE_L2.findall(body)]
    l3 = [{"index": i + 1, "title": t.strip()} for i, (_, t) in enumerate(_RE_L3.findall(body))]

    # 叶子层级的选择：子项够多就用子项（A23 的「1.」共 23 条），否则用「（一）」，否则用一级编号
    if len(l2) >= 5:
        items = l2
    elif len(l3) >= 5:
        items = l3
    else:
        items = l1

    if len(l2) < 5:
        l1_for_sections = l1 if len(l1) > len(items) else []
    else:
        l1_for_sections = l1

    m = _RE_ANY_MARKER.search(body)
    first_pos = m.start() if m else -1

    return l1_for_sections, items, first_pos


def _lead_of(body: str, first_pos: int, title: str = "") -> str:
    if first_pos > 0:
        head = body[:first_pos]
        # 取最后一句完整的话，避免把通知抬头（"各省、自治区…厅（局、委）："）当成导语
        parts = [p for p in re.split(r"(?<=[。！？])", head) if p.strip()]
        lead = parts[-1] if parts else head
        if len(lead) < 15:                       # 太短就把前一句也带上
            lead = "".join(parts[-2:]) if len(parts) >= 2 else lead
    else:
        # 整篇没有编号（新闻稿、技术意见这一类）：导语就是正文开头两句话
        parts = [p for p in re.split(r"(?<=[。！？])", body) if p.strip()]
        lead = "".join(parts[:2]) if len(parts) >= 2 else (parts[0] if parts else "")
        if len(lead) < 20 and len(parts) >= 3:
            lead = "".join(parts[:3])

    # 去掉公文噪声。顺序有讲究：先砍日期戳（它后面紧跟重复的公文标题），
    # 再砍抬头，最后砍标题 —— 反过来的话日期戳会藏在抬头中间砍不干净。
    lead = re.sub(r"^(?:本网讯|本报讯)[，,]?", "", lead)
    lead = re.sub(r"^.*?\d{4}年\d{1,2}月\d{1,2}日", "", lead)
    # 「各省、自治区、直辖市人民政府，新疆生产建设兵团，国务院有关部委…：」这类抬头
    m = _RE_SALUTATION.match(lead)
    if m:
        lead = lead[m.end():]
    # 去空白后「重复的标题 + 正文首句」会粘成一句：『关于…的实施方案为进一步…』。
    # 只有当标题后面紧跟着正文起头词（为/现/按照…）时才砍，避免误伤正文本身。
    lead = re.sub(r"^[^，。；]{0,60}?"
                  r"(?:实施方案|方案|通知|意见|办法|规划|规定|细则|计划|要点|纲要|方案)"
                  r"(?=为|现|按照|根据|以|坚持|为深入)", "", lead)
    if title:
        if lead.startswith(title):
            lead = lead[len(title):]
        elif title.endswith("的通知") and lead.startswith(title[:-3]):
            lead = lead[len(title) - 3:]

    lead = _normalize_punct(lead)
    # 标点重复（正文被切碎后常见的「，。」）
    lead = re.sub(r"[，,、；;]+(?=[。！？])", "", lead)
    # 超长时退到最近的逗号处收尾，宁可少一句也不要截出「全力夯实夏粮稳」这种半句话
    if len(lead) > 120:
        cut = max(lead.rfind(p, 0, 120) for p in "，；、")
        lead = lead[:cut] if cut >= 60 else lead[:120]
    return lead.rstrip("，、；")


# 公文抬头：以「各省…：」开头，冒号前必须出现厅/局/委/政府这类机关后缀
_RE_SALUTATION = re.compile(
    r"^.{0,80}?(?:人民政府|政府|兵团|厅|局|委|部|办公室|中心|院|校|社|会)"
    r"[^一-鿿]{0,4}[：:]"
)


def _normalize_punct(s: str) -> str:
    """公文页面里中英标点混用（半角逗号夹在汉字中间），统一成全角，读起来才像中文。

    只改标点宽度、不动一个字，所以摘出来的要点仍算原话。
    前半字既可能是汉字，也可能是一个收尾的引号/顿号（如『…"隐秘流通",整治…』），
    因此两侧字符集都要放宽，否则这类半角逗号会漏网。
    """
    before = r"[一-鿿" + _CJK_PUNCT + r"]"
    s = re.sub(rf"(?<={before}),(?=[一-鿿{_CJK_PUNCT}])", "，", s)
    s = re.sub(rf"(?<={before});(?=[一-鿿{_CJK_PUNCT}])", "；", s)
    s = re.sub(rf"(?<={before}):(?=[一-鿿{_CJK_PUNCT}])", "：", s)
    return s


def classify(text: str) -> str:
    scores = {key: sum(text.count(kw) for kw in kws) for key, kws in _TYPE_KEYWORDS.items()}
    best = max(scores, key=lambda k: scores[k])
    return best if scores[best] > 0 else "other"


def build_summary(lead: str, sections: list[dict], items: list[dict]) -> str:
    """核心内容摘要 = 原文导语 + 条目/章节标题清单。"""
    lead = lead.rstrip("，。；")
    names_src = sections if len(sections) >= 3 else items
    names = "、".join(x["title"] for x in names_src[:8])
    more = f" 等 {len(items)} 项" if len(items) > len(names_src[:8]) else ""
    if names:
        return f"{lead}。主要内容包括：{names}{more}。"[:260]
    return f"{lead}。"[:260]


# ------------------------------------------------------------------ 原文要点

_MAX_POINT_CHARS = 110        # 单条要点上限，超了在逗号处收尾
_MAX_POINTS = 6               # 每条政策最多摘几段原话

# 条目名后面紧跟的句读符：正文里是「一、耕地地力保护补贴。补贴对象为…」，
# 摘要点时要把中间的句号吃掉，否则要点会以「。」开头。
_RE_LEADING_PUNCT = re.compile(r"^[。．.：:，,、；;　\s]+")

# 中文语境里的标点（_normalize_punct 判断"这个半角逗号是不是夹在中文中间"时要用）
_CJK_PUNCT = "，。、；：？！“”‘’（）《》〈〉【】〔〕…—"


def _sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[。！？])", text) if s.strip()]


def _first_sentence_after(body: str, pos: int) -> str:
    """取 pos 之后的第一个完整句子（原话）。

    过长时退到最近的逗号并用「……」标出截断 —— 省略号只表示"这里掐掉了"，
    不改变任何一个字，所以整条要点仍可逐字回原文核对。
    """
    seg = _RE_LEADING_PUNCT.sub("", body[pos:])
    if not seg:
        return ""
    end = seg.find("。")
    if end < 0:
        return _normalize_punct(seg[:_MAX_POINT_CHARS].rstrip("，、；")) + "……"
    if end <= _MAX_POINT_CHARS:
        return _normalize_punct(seg[:end + 1])
    cut = max(seg.rfind(p, 0, _MAX_POINT_CHARS) for p in "，；、")
    if cut >= 30:
        return _normalize_punct(seg[:cut]) + "……"
    return _normalize_punct(seg[:end + 1])


def _same_sentence(a: str, b: str) -> bool:
    """两句是否指向原文同一处。

    导语是正文前两三句拼出来的，本身就与正文的句子重叠；而两边的写法又不一样
    （导语已洗掉「本网讯」、可能被截断），所以不能直接比字符串，要先把新闻前缀
    和行末标点都去掉，再看包含关系。低于 12 字的短句不参与判断，避免"是"这类
    子串误命中。
    """
    def _core(s: str) -> str:
        s = re.sub(r"^(?:本网讯|本报讯|（本网讯）|\(本网讯\))[，,]?", "", s.strip())
        return _RE_LEADING_PUNCT.sub("", s).rstrip("。．.…")

    x, y = _core(a), _core(b)
    if len(x) < 12 or len(y) < 12:
        return False
    return x in y or y in x


def extract_points(body: str, items: list[dict], lead: str) -> list[dict]:
    """从原文里逐条摘出要点句子，**不改写、不概括**。

    有编号条目的页面：一条要点 = 条目名 + 该条正文的第一句话，
        如「耕地地力保护补贴」+「补贴对象为实际种粮农民……」，读的人既知道是哪一条，
        看到的又是原文本身（含金额、比例、期限这些"重要部分"）。
    无编号的新闻稿/技术意见：直接摘正文开头几句。

    返回 [{"title": 条目名或空, "text": 原文句子}, ...]，最多 _MAX_POINTS 条。
    """
    out: list[dict] = []
    seen: set[str] = set()
    cursor = 0

    for it in items:
        title = (it.get("title") or "").strip()
        if not title:
            continue
        hit = body.find(title, cursor)
        if hit < 0:
            # 同名条目在别处（如章节标题里）先出现过，或正文顺序与解析顺序不一致
            hit = body.find(title)
        if hit < 0:
            continue
        cursor = hit + len(title)
        sent = _first_sentence_after(body, cursor)
        if len(sent) < 8 or sent in seen:      # 太短的（如只有条目名）不值得当要点
            continue
        seen.add(sent)
        out.append({"title": title, "text": sent})
        if len(out) >= _MAX_POINTS:
            break

    if not out:
        # 整篇没有编号（新闻稿、技术意见）：summary 就是导语本身（build_summary 在
        # 没有条目名时只输出导语），所以导语只用来排重、不产出要点 —— 否则卡片上
        # 第一行摘要和第一条要点会是同一句话，挨着重读一遍很廉价。
        if len(lead) >= 12:
            seen.add(lead)
        for raw in _sentences(body):
            sent = _normalize_punct(raw.strip())
            if len(sent) < 12 or any(_same_sentence(sent, s) for s in seen):
                continue
            if len(sent) > _MAX_POINT_CHARS:      # 段落式长句，退到最近的逗号
                cut = max(sent.rfind(p, 0, _MAX_POINT_CHARS) for p in "，；、")
                sent = sent[:cut] + "……" if cut >= 30 else sent[:_MAX_POINT_CHARS] + "……"
            seen.add(sent)
            out.append({"title": "", "text": sent})
            if len(out) >= _MAX_POINTS:
                break

        # 整篇正文就一句话（如短讯）：宁可与摘要重复，也好过这一栏空着。
        if not out and len(lead) >= 12:
            out.append({"title": "", "text": lead if lead.endswith(("。", "！", "？")) else lead + "……"})

    return out


# ------------------------------------------------------------------ 主流程

def build_policy_list() -> dict:
    items: list[dict] = []
    failures: list[str] = []

    for src in SOURCES:
        try:
            html = fetch_html(src.url)
            text = html_to_text(html)
            date_iso, unit = extract_meta(text, src)
            body = extract_body(text)
            sections, leaf_items, first_pos = _extract_structure(body)
            lead = _lead_of(body, first_pos, src.title)
            summary = build_summary(lead, sections, leaf_items)
            points = extract_points(body, leaf_items or sections, lead)

            if src.expected_items and len(leaf_items) != src.expected_items:
                logger.warning("%s 解析出 %d 个条目，与清单标注的 %d 项不符，请复核页面结构",
                               src.pid, len(leaf_items), src.expected_items)

            items.append(
                {
                    "id": src.pid,
                    "title": src.title,
                    "publish_unit": unit,
                    "publish_date": src.publish_date,
                    "publish_date_iso": date_iso,
                    "type": classify(src.title + summary),
                    "type_name": "",
                    "summary": summary,
                    "sections": sections,
                    "items": leaf_items,
                    "item_count": len(leaf_items),
                    "lead": lead,
                    "points": points,
                    "source_url": src.url,
                    "fetched": True,
                }
            )
            logger.info("抓取成功 %s：%s（%s，%d 章节 / %d 条目，摘要 %d 字，原文要点 %d 条）",
                        src.pid, src.title, date_iso, len(sections), len(leaf_items), len(summary),
                        len(points))
        except Exception as e:  # noqa: BLE001
            failures.append(src.pid)
            logger.error("抓取失败 %s（%s）：%s，使用回退条目", src.pid, src.title, e)
            items.append(
                {
                    "id": src.pid,
                    "title": src.title,
                    "publish_unit": src.publish_unit,
                    "publish_date": src.publish_date,
                    "publish_date_iso": src.publish_date_iso,
                    "type": "other",
                    "type_name": "",
                    "summary": "（联网抓取原文失败，摘要待补充；标题、发布日期与发布单位为已核验的官方信息）",
                    "sections": [],
                    "items": [],
                    "item_count": 0,
                    "lead": "",
                    "points": [],
                    "source_url": src.url,
                    "fetched": False,
                }
            )

    type_names = {t["key"]: t["name"] for t in POLICY_TYPES}
    for it in items:
        it["type_name"] = type_names.get(it["type"], "其他")

    return {
        "version": 1,
        "year": 2026,
        "demo_data": bool(failures),
        "disclaimer": (
            "摘要与要点均由官方原文自动摘录（导语 + 条目清单 + 逐字照抄的关键句），"
            "未做改写，可点标题前往 source_url 逐字核对。"
            if not failures else
            f"部分条目（{', '.join(failures)}）联网抓取失败，摘要待补充。"
        ),
        "types": POLICY_TYPES,
        "total": len(items),
        "items": items,
    }


def build_and_save() -> dict:
    data = build_policy_list()
    out = path_of("policy_list")
    write_json(out, data)
    logger.info("已写出政策数据：%s（%d 条，demo_data=%s）", out, data["total"], data["demo_data"])
    for it in data["items"]:
        logger.info("  %s [%s] %s | %s | %s", it["id"], it["type_name"],
                    it["title"], it["publish_unit"], it["publish_date"])
        logger.info("      摘要：%s", it["summary"])
    return data


if __name__ == "__main__":
    build_and_save()
