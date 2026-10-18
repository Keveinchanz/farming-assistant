"""任务 4.5 的数据侧 —— 构建知识图书库：病害防治知识 + 虫害防治知识 + 农作物养护知识。

三个分类（对应前端 5.4 的标签栏）：
    pest  病害防治知识：271 个类别里除健康样本外的 250 个病害，每类一张卡片
    insect 虫害防治知识：IP102 的 102 类虫害各一张卡片（与病害是两个独立的标签空间）
    care  农作物养护知识：47 个作物大类各一张卡片，含该作物全部病害索引与健康植株特征

⚠ 内容来源必须说清楚
    卡片正文分两截，来源不同，每张卡片的 content_source 字段照实写两截：
      · 发病规律 / 危害规律 / 防治方法 / 养护要点：**按病原类型、取食方式与作物类别套用
        规则模板生成的演示内容**，不是引用自某份官方植保手册。
      · 用药提醒：摘自**农业农村部农药管理司《农药登记信息》**的公开登记条目
        （data/pesticide_registry.json，由 scripts/26 抓取），按防治对象名称匹配，
        列出真实存在的登记农药（含登记证号与企业）。匹配不到对应登记的卡片
        （营养失调、生理性病害等）保留通用提示。无论哪种，末尾都强制带
        「具体剂型与用量以农药登记标签和当地植保部门指导为准」的安全句 ——
        登记说明的是"这个药能防治这种病"，不是"就该这么用"，剂量仍须照标签与当地指导。
    真正来自官方的文本还有两处：任务 1.6 抓取的惠农政策原文摘要（已单独标注），
    以及你把官方植保文档放进 knowledge/documents/ 后由 4.4 建索引检索出来的引文。

产出：
    knowledge/library/library_items.json
    knowledge/documents/*.txt        （4.4 的 RAG 切分源，一卡一文件）
"""

from __future__ import annotations

import json
from pathlib import Path

from src.common.io_utils import ensure_dir, write_json, write_text
from src.common.logging_utils import setup_logger
from src.common.paths import PROJECT_ROOT, path_of
from src.content.frontend_bundle import build_library_preload

logger = setup_logger("agri.library")

# 卡片的用药部分来源：「用药提醒」取自农业农村部农药管理司《农药登记信息》里的真实
# 登记条目，按防治对象匹配。匹配不到对应登记的卡片（营养失调、生理性病害这类本来就没有
# 对症农药）保留通用提示，括注里说清楚这一点，免得读者以为卡片列出的农药名都是登记数据。
CONTENT_SOURCE = (
    "用药提醒摘自农业农村部农药管理司《农药登记信息》公开登记（无对应登记的为通用提示）"
)

SAFETY_NOTE = (
    "用药提醒：具体农药品种、剂型与用量请以当地植保部门指导、农药登记标签和最新禁限用名录为准；"
    "注意轮换用药以延缓抗性，严格遵守安全间隔期，采收前停药。"
)

# 「用药提醒」段落的固定收尾。它必须是每一版用药提醒的最后一句话 ——
# 后半句「以农药登记标签…为准」是硬约束：无论前面填的是通用提示还是真实登记的农药，
# 都不能让读者以为照着卡片买药就行，剂型与用量终究要看标签和当地植保部门。
_MEDICATION_TAIL = (
    "具体剂型与用量以农药登记标签和当地植保部门指导为准；注意轮换用药以延缓抗性，"
    "严格遵守安全间隔期，采收前停药。"
)

# 每张卡片最多列几个登记农药。列全了动辄上百个（「一年生杂草」有 400 多个登记），
# 塞进卡片既读不完也没有意义 —— 给几个真实存在的例子、并说明总数，信息量刚刚好。
_MAX_EXAMPLES = 4

# 「用药提醒」段落的标题。写死而不是从 SAFETY_NOTE 里切 —— 标题只此一处好改，
# 切字符串的写法一改文案就会跟着变。
_MEDICATION_TITLE = "用药提醒"

# 病原类型 -> (发病规律, 防治方法)
PATHOGEN_TEXT: dict[str, tuple[str, str]] = {
    "真菌": (
        "多在高湿、适温条件下流行。田间湿度大、通风差、植株密度过高时孢子快速繁殖传播；"
        "连阴雨、结露时间长或灌溉方式不当（如大水漫灌、叶面长时间持水）后 5～10 天常出现发病高峰。"
        "病菌多以菌丝或孢子在病残体、土壤或种子上越冬，次年借风雨、气流和农事操作传播。",
        "① 农业防治：选用抗病品种与无病种子；实行轮作，避免连作；及时清除病叶病株并带出田外销毁；"
        "合理密植、改善通风透光；采用滴灌或膜下灌溉，避免叶面长时间持水；平衡施肥，避免偏施氮肥。"
        "② 物理/生物防治：地膜覆盖阻隔土壤病菌；发病初期可使用枯草芽孢杆菌、木霉菌等生物制剂。"
        "③ 化学防治：发病初期开始用药，重点喷施叶背与植株中下部，7～10 天一次，连续 2～3 次，注意轮换。"
    ),
    "细菌": (
        "病原细菌多在种子、病残体和土壤中越冬，通过伤口、气孔和自然孔口侵入。"
        "高温高湿、暴雨或大水漫灌后、田间作业造成伤口时最易侵染扩散；"
        "借助雨水飞溅、灌溉水、昆虫和农具传播，往往呈点片发生后迅速蔓延。",
        "① 农业防治：使用无病种子并进行种子处理；与非寄主作物轮作 2～3 年；"
        "及时拔除并销毁病株，病穴撒石灰消毒；避免雨后或露水未干时进田作业，减少人为伤口；"
        "改漫灌为沟灌或滴灌，雨后及时排水。"
        "② 生物防治：可选用多粘类芽孢杆菌、荧光假单胞菌等制剂灌根或喷雾。"
        "③ 化学防治：发病初期使用对细菌有效的登记药剂，重点保护新生组织与伤口部位，注意轮换用药。"
    ),
    "病毒": (
        "由蚜虫、粉虱、蓟马等介体昆虫或汁液摩擦传播，种子带毒也是重要初侵染源。"
        "高温干旱年份介体繁殖快、迁飞频繁，往往伴随介体高峰出现成片发病；"
        "田间杂草和邻近病株是重要的病毒中间寄主，一旦感染无法治愈，只能控制传播。",
        "① 农业防治：选用抗耐病品种与无毒种子；及时拔除零星病株；清除田边杂草，减少介体栖息场所；"
        "覆盖银灰色地膜或防虫网避蚜；加强肥水管理提高植株抗性。"
        "② 介体防治（关键）：在介体迁飞高峰期及时防治蚜虫、粉虱等传毒昆虫，"
        "优先使用黄板诱杀、生物天敌等绿色防控手段。"
        "③ 化学防治：以治虫防病为主，发病后可辅助使用氨基寡糖素、宁南霉素等抗病毒制剂，"
        "但需明确：病毒病无法根治，重在预防介体传播。"
    ),
    "生理性": (
        "非病原侵染引起，由营养失衡（缺素或元素拮抗）、水分逆境、温度胁迫（低温冷害、高温日灼）"
        "以及土壤板结、盐渍化等环境因素导致。通常成片、对称发生，与地形、土壤质地、施肥历史的"
        "分布高度相关，且不会在植株间传染蔓延。",
        "① 测土配方施肥，针对性补充所缺元素，注意元素间的拮抗关系（如过量施钾会诱发缺镁）。"
        "② 改良土壤结构，增施有机肥，合理深耕；酸性土施石灰、碱性土施硫磺或石膏调节 pH。"
        "③ 水分管理：避免忽干忽湿，推广滴灌与水肥一体化；高温期遮阳、低温期覆盖保温。"
        "④ 已出现症状的植株可叶面喷施对应微量元素进行缓解，但根本措施是消除逆境成因。"
    ),
    "药害肥害": (
        "由农药或肥料使用不当造成：包括浓度过高、混配不当、高温时段施药、"
        "重复喷施、器械残留（如除草剂漂移）以及未腐熟有机肥烧根等。"
        "通常在施药施肥后 3～7 天集中显现，田块内分布与施药路径、风向高度一致，"
        "边界往往呈明显的条带状或扇形。",
        "① 立即措施：发现后尽快喷清水冲洗叶面，降低植株表面残留；适度遮阴、加强通风。"
        "② 缓解措施：喷施芸苔素内酯、氨基酸或海藻酸类叶面肥帮助恢复长势；"
        "土壤施肥过量时及时灌水淋洗（注意避免养分流失污染地下水）。"
        "③ 预防：严格按标签浓度配药，避免高温强光时段施药；施药前清洗器械，除草剂专用器械单独存放；"
        "有机肥必须充分腐熟后施用。"
    ),
    "其他": (
        "该类别没有单一明确的病原，可能是多种因素叠加或命名上未细分的情形。"
        "建议结合田间实际症状、发生分布与环境条件综合判断，必要时送当地植保部门鉴定。",
        "① 先确诊再用药：观察症状在植株上的部位、形状、颜色与扩展速度，结合近期天气与农事操作判断诱因。"
        "② 综合管理：选用健康种苗、合理轮作、平衡施肥、科学灌排，提升植株整体抗逆能力。"
        "③ 记录归档：对发病时间、地块、品种、用药历史做记录，便于逐年优化管理方案。"
    ),
    "健康": (
        "本类为健康植株样本，用于帮助识别正常长势，作为田间对比的参照。",
        "保持健康的栽培管理即可：合理轮作、平衡施肥、科学灌排、及时清除田间杂草与病残体，"
        "并定期巡田以便在发病初期就发现异常。"
    ),
}

# 虫害类群 -> (危害规律, 防治方法)
#
# 与上面的 PATHOGEN_TEXT 是**并列的另一套**，不是它的子集：上面按病原分（真菌/细菌/病毒…），
# 这里按**口器与取食方式**分（ chewing 的吃叶子、刺吸的吸汁液、潜叶的钻在叶肉里…）。
# 这个分法不是随意选的 —— 防治要点几乎完全由取食方式决定：
# 潜叶类必须在幼虫潜入前打药，刺吸类必须打叶背，地下害虫必须做土壤处理。
# 按病原那套分类去套虫害会得出完全错误的用药时机。
#
# 内容同样是**规则模板生成的演示内容**，非官方植保手册原文（见本文件顶部的说明）。
PEST_TEXT: dict[str, tuple[str, str]] = {
    "螟蛾类": (
        "幼虫钻蛀茎秆或卷叶取食叶肉，造成枯心苗、白穗与虫苞，分蘖期至孕穗期危害最重。"
        "成虫有趋光性，夜间活动并把卵产于叶背；多以老熟幼虫在稻桩、秸秆或杂草中越冬，"
        "次年春季化蛹羽化，田间往往呈零星发生后逐步扩大的态势。",
        "① 农业防治：收获后及时清除并处理秸秆与田边杂草，压低越冬虫源；合理施肥避免贪青徒长。"
        "② 物理防治：安装杀虫灯或使用性诱剂诱杀成虫，掌握在羽化高峰期前布设。"
        "③ 生物防治：在产卵初期释放赤眼蜂寄生卵粒，或使用苏云金杆菌（Bt）制剂。"
        "④ 化学防治：抓住卵孵化高峰至低龄幼虫期施药，药液重点喷施心叶与叶鞘，"
        "此时幼虫尚未蛀入茎秆，是唯一有效窗口。"
    ),
    "刺吸类": (
        "成虫和若虫聚集在叶背刺吸汁液，造成叶片失绿发黄、卷曲，植株矮化；"
        "分泌的蜜露还会诱发煤污病，部分种类同时传播病毒病。"
        "高温干旱或田间郁闭、通风不良时繁殖极快，世代重叠明显，常在短期内暴发。",
        "① 农业防治：清除田边杂草寄主，合理密植改善通风透光，避免偏施氮肥导致植株徒长招虫。"
        "② 物理防治：田间悬挂黄板诱杀，注意随植株长高调整高度。"
        "③ 生物防治：保护利用瓢虫、草蛉、蜘蛛等自然天敌；发生初期可选用印楝素等植物源药剂。"
        "④ 化学防治：这类虫子藏在叶背，喷药必须由下向上、重点打透叶背，"
        "并轮换不同作用机理的药剂以延缓抗性。"
    ),
    "螨类": (
        "体型极小，肉眼难以察觉，聚集在叶背刺吸汁液。受害叶面初现失绿小斑点，"
        "随后连片变黄褐、叶片失去光泽，严重时枯焦脱落。"
        "高温干旱条件下繁殖速度极快，世代重叠，往往一片叶上同时存在卵、若螨与成螨。",
        "① 农业防治：适时灌溉提高田间湿度可明显抑制其繁殖；清除田边杂草减少虫源。"
        "② 生物防治：释放捕食螨，或使用矿物油、苦参碱等对天敌友好的药剂。"
        "③ 化学防治：必须选用**同时杀卵和杀成螨**的药剂配合使用，"
        "只杀成螨的话卵会很快孵化导致反弹；不同类型杀螨剂轮换，避免连用同一种。"
    ),
    "蓟马类": (
        "以锉吸式口器锉破植物表皮吸食汁液，叶面出现银白色斑点与条斑，叶片皱缩畸形；"
        "花器受害造成落花落果，果实受害留下疤痕。"
        "喜温暖干旱环境，体型小且善于隐蔽，多在花内、叶背和心叶处活动。",
        "① 农业防治：覆盖地膜阻断入土化蛹，清除杂草寄主；适度灌溉提高湿度抑制其活动。"
        "② 物理防治：利用趋蓝性悬挂蓝板诱杀成虫。"
        "③ 生物防治：保护小花蝽、捕食螨等天敌；可选用多杀霉素等生物源药剂。"
        "④ 化学防治：因其隐蔽性强，务必在**早晨或傍晚**施药，"
        "药液要能触及花器内部与叶背褶皱处，常规叶面喷洒往往打不到虫。"
    ),
    "地下害虫": (
        "在土壤中取食种子、幼根和茎基部，造成缺苗断垄、植株萎蔫倒伏，"
        "受害株轻轻一拔即断，断口整齐。春秋两季土温适宜时活动最盛，多为夜间出洞取食，"
        "白天潜伏土中，因此地表往往看不到虫体。",
        "① 农业防治：播前深耕晒垡，直接杀死部分土中虫体；**施用充分腐熟的有机肥** —— "
        "未腐熟的粪肥会聚集大量成虫产卵，是诱发这类虫害最主要的人为原因。"
        "② 物理防治：用糖醋液或毒饵诱杀，傍晚撒于田间。"
        "③ 生物防治：使用白僵菌、绿僵菌或昆虫病原线虫进行土壤处理。"
        "④ 化学防治：以播前土壤处理和种子包衣为主；生长期发现危害可用药液灌根，"
        "灌后适当浇水让药剂下渗到根系层。"
    ),
    "夜蛾类": (
        "幼虫食量大，低龄时群集啃食叶肉，只留表皮呈窗孔状；"
        "高龄后分散蚕食，把叶片吃成缺刻甚至只剩叶脉。"
        "有假死性和群集迁飞习性，多在傍晚和夜间取食，白天藏于土缝或叶片背面。",
        "① 农业防治：清除田边杂草，秋冬深翻灭蛹；可结合田间作业人工捕捉卵块和低龄幼虫。"
        "② 物理防治：杀虫灯、性诱剂或糖醋液诱杀成虫。"
        "③ 生物防治：苏云金杆菌（Bt）、核多角体病毒对低龄幼虫效果好，"
        "也可在产卵期释放赤眼蜂。"
        "④ 化学防治：**低龄幼虫期（3 龄前）** 是防治关键，此时抗药性最弱；"
        "选择傍晚施药，药液才能接触到出洞取食的幼虫。"
    ),
    "甲虫类": (
        "成虫啃食叶片造成孔洞和缺刻，严重时叶脉外全部被吃光；"
        "幼虫则多在土壤中取食根系，或钻入植物组织内部危害（如象甲在果实内产卵）。"
        "多数成虫有假死性，受惊即坠落，部分种类具明显趋光性。",
        "① 农业防治：与非寄主作物轮作，清除田间残株落叶等越冬场所。"
        "② 物理防治：利用假死性在清晨震荡植株使成虫坠落集中捕杀；配合诱虫灯诱杀。"
        "③ 生物防治：使用绿僵菌、白僵菌制剂处理土壤或叶面。"
        "④ 化学防治：掌握在成虫盛发期、产卵前施药效果最好；"
        "已钻入果实或茎秆的幼虫药剂很难触及，此时应优先人工摘除处理。"
    ),
    "实蝇类": (
        "雌虫用产卵器刺破果皮将卵产入果内，幼虫在果实中蛀食，"
        "导致果实腐烂、未熟先黄并大量落果，是果树上危害最重的害虫类群之一，"
        "也属于重要检疫对象。成虫全年可多次产卵，随调运的带虫果实远距离传播。",
        "① 检疫措施：严禁从疫区调运带虫果实，这是防止扩散最有效的手段。"
        "② 农业防治：及时摘除虫果、捡拾落果并深埋或销毁，"
        "**不能就地堆放** —— 堆放的虫果是下一代的虫源。"
        "③ 物理防治：使用性诱剂或蛋白诱饵诱杀成虫，果实膨大期套袋保护。"
        "④ 化学防治：以诱杀成虫为主；如需喷药，务必严格遵守安全间隔期，"
        "采收前停药，保障果品安全。"
    ),
    "蝽蝗类": (
        "蝽类以刺吸式口器吸食嫩梢和果实汁液，造成果实畸形、僵化与落果；"
        "蝗虫则暴食叶片和茎秆，群集迁飞，可在极短时间内把大片田块吃成光秆。"
        "蝗虫在土中产卵，卵块孵化出的蝗蝻**三龄前**群集且不会飞，是防治的关键窗口。",
        "① 农业防治：清除田边杂草与荒地，破坏蝗虫产卵场所；秋冬季深耕翻土杀灭越冬卵块。"
        "② 物理防治：蝗虫应在**蝗蝻三龄前**群集期集中围歼；蝽类可用诱虫灯诱杀。"
        "③ 生物防治：蝗虫微孢子虫、绿僵菌等生物制剂对蝗虫可持续控制，"
        "且对农田生态友好，适合在地块周边使用。"
        "④ 化学防治：蝗虫防治必须**联防联治**（按地块连片统一行动），"
        "零星防治只治标不治本；蝽类用药要注意果实的采收安全间隔期。"
    ),
    "潜叶类": (
        "幼虫潜居在叶片上下表皮之间蛀食叶肉，形成弯曲的白色蛇形虫道，"
        "严重时叶片枯焦、脱落，光合作用大幅下降。"
        "各虫态同时存在，世代重叠，成虫白天活动并在嫩叶上产卵。",
        "① 农业防治：及时摘除并销毁有虫道的叶片，清除田边杂草与老叶残体。"
        "② 物理防治：成虫有趋黄性，悬挂黄板诱杀可显著降低落卵量；"
        "覆盖防虫网阻断成虫入侵。"
        "③ 生物防治：保护潜叶蜂等寄生性天敌，可选用阿维菌素等生物源药剂。"
        "④ 化学防治：**成虫盛发期和幼虫孵化初期**是唯一的有效窗口 —— "
        "幼虫一旦潜入叶肉，药剂根本接触不到虫体，之后打药基本无效，"
        "因此防治必须打在上一次，靠监测预警而不是事后补救。"
    ),
    "其他": (
        "该类群没有单一明确的危害模式，可能是多种虫类混合发生，或分类上未进一步细分的情形。"
        "建议结合田间虫体形态、取食痕迹（啃食/刺吸/潜蛀）与发生分布综合判断。",
        "① 先识别再用药：观察虫体形态、危害部位与痕迹类型，判断是按叶面害虫还是蛀食性害虫处置。"
        "② 综合管理：轮作清园、合理水肥、保护天敌，提升田块整体的抗逆与自控能力。"
        "③ 记录归档：对发生时间、地块、虫态、用药历史做记录，便于逐年优化防治方案。"
        "④ 临机处置：实在难以判定时，可先人工摘除受害部位并带到当地植保部门鉴定，避免盲目施药。"
    ),
}

# 作物类别 -> 养护要点
CARE_TEXT: dict[str, str] = {
    "粮食作物": (
        "① 土壤与整地：深耕晒垡，打破犁底层；播前精细整地，做到地平、土碎、墒情足。"
        "② 品种与播期：选用适合当地积温与茬口的审定品种，适期播种，避免过早遭遇低温或过晚贪青。"
        "③ 肥水管理：测土配方施肥，氮磷钾配合，重视中后期磷钾肥；拔节/抽穗等关键期保证水分供应，"
        "同时注意雨后排水防渍。"
        "④ 病虫草害：以抗病品种和轮作为基础，重点抓好播种期种子处理和关键生育期的监测预警。"
        "⑤ 收获与贮藏：适期收获，及时晾晒降水到安全水分，仓储注意防潮防虫防霉。"
    ),
    "经济作物": (
        "① 选地与轮作：选择土层深厚、排水良好的地块，避免与同科作物连作，减少土传病害积累。"
        "② 育苗与移栽：培育壮苗，移栽时保护根系，缩短缓苗期。"
        "③ 水肥调控：根据需肥规律分期施用，苗期控氮防徒长，中后期增施磷钾促品质。"
        "④ 植株管理：及时整枝、打杈、摘心，改善通风透光，降低田间湿度。"
        "⑤ 采收：按成熟度分批采收，采后及时预冷与分级，减少机械损伤。"
    ),
    "蔬菜": (
        "① 茬口安排：合理轮作换茬，避免同科蔬菜连作；保护地要注意夏季高温闷棚消毒。"
        "② 土壤改良：增施腐熟有机肥，调节土壤 pH，改善团粒结构，防止连作障碍与盐渍化。"
        "③ 温湿管理：保护地控制好温湿度，避免叶面长时间结露，是预防霜霉、灰霉等病害的关键。"
        "④ 水肥一体化：小水勤浇，避免大水漫灌；追肥少量多次，采收期注意追施钾肥提升品质。"
        "⑤ 植株调整：及时摘除老叶病叶，合理密植，改善通风透光条件。"
    ),
    "果树": (
        "① 建园与土壤管理：选择排水良好的坡地或高垄栽培，行间生草或覆草，改善根际环境。"
        "② 整形修剪：通过冬剪与夏剪培养合理树形，打开光路，保持树冠通风透光，"
        "及时疏除病虫枝、交叉枝、下垂枝。"
        "③ 花果管理：合理疏花疏果，控制负载量，避免大小年；必要时进行套袋保护。"
        "④ 肥水管理：秋施基肥为主，生长期分期追肥，重视钙、硼、锌等中微量元素；"
        "萌芽期与果实膨大期保证水分，雨季注意排水防涝。"
        "⑤ 清园：落叶后彻底清除枯枝落叶与病果并集中销毁，树干涂白，减少越冬病虫基数。"
    ),
}


def _url_for(rel_path: str) -> str:
    """把 label_map 里的项目相对路径转成 /media 静态 URL。

    后端会把 data/processed/dataset 挂载到 /media，因此不需要把 271 张封面复制一遍。
    """
    p = rel_path.replace("\\", "/")
    marker = "data/processed/dataset/"
    if marker in p:
        return "/media/" + p.split(marker, 1)[1]
    return f"/media/{Path(p).name}"


def _images_of(label_map: dict, entry: dict, limit: int = 3) -> list[str]:
    d = PROJECT_ROOT / entry["out_dir"]
    if not d.exists():
        return []
    files = sorted(
        f for f in d.iterdir() if f.is_file() and f.suffix.lower() in {".jpg", ".jpeg", ".png"}
    )
    return [_url_for(f.relative_to(PROJECT_ROOT).as_posix()) for f in files[:limit]]


# ------------------------------------------------------- 用药提醒（农药登记数据）

_REGISTRY: dict | None = None


def load_pesticide_registry() -> dict:
    """读 data/pesticide_registry.json（scripts/26_fetch_pesticide_registry.py 的产物）。

    读不到就返回 {}，调用方一律回退到 SAFETY_NOTE —— 索引是抓来的运行期数据，
    它缺席不该让知识图书库建不出来。
    """
    global _REGISTRY
    if _REGISTRY is not None:
        return _REGISTRY
    try:
        path = path_of("pesticide_registry")
    except KeyError:
        logger.warning("configs/default.yaml 里没有 pesticide_registry 这个路径，用药提醒走通用文案")
        _REGISTRY = {}
        return _REGISTRY
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        _REGISTRY = data if isinstance(data, dict) and data.get("targets") else {}
    except FileNotFoundError:
        logger.warning("没有 %s，用药提醒走通用文案（跑 scripts/26_fetch_pesticide_registry.py 可补上）", path)
        _REGISTRY = {}
    except Exception as e:  # noqa: BLE001
        logger.error("农药登记索引读不出来（%s）：%s，用药提醒走通用文案", path, e)
        _REGISTRY = {}
    if _REGISTRY:
        logger.info("农药登记索引：%d 条登记 / %d 个防治对象",
                    _REGISTRY.get("total_records", 0), _REGISTRY.get("target_count", 0))
    return _REGISTRY


def _sorted_keys(targets: dict) -> list[str]:
    """按长度倒序：先匹配更具体的键（「一年生阔叶杂草」优先于「杂草」）。"""
    return sorted(targets, key=len, reverse=True)


def match_target(condition: str, targets: dict) -> str | None:
    """卡片上的病害/虫害名 -> 登记索引里的防治对象键。

    三级：全等 → 键被名字包含 → 名字被键包含。
    键长下限取 2 是实测出来的：用 3 的话，「白背飞虱」「黑尾叶蝉」「稻管蓟马」这些
    IP102 的种名就找不到登记里对应的类群名（飞虱 / 叶蝉 / 蓟马），虫害卡片的覆盖率
    从 52% 掉到 33%；放宽到 2 之后抽检的匹配（亚洲玉米螟→玉米螟、红蜘蛛（叶螨）→红蜘蛛）
    都是对的。
    """
    condition = (condition or "").strip()
    if not condition or not targets:
        return None
    if condition in targets:
        return condition
    keys = _sorted_keys(targets)
    for key in keys:
        if len(key) >= 2 and key in condition:
            return key
    for key in keys:
        if len(key) >= 2 and condition in key:
            return key
    return None


def medication_text(condition: str, crop: str = "") -> str:
    """按防治对象写用药提醒：优先用真实登记的农药，匹配不到就回退通用提示。

    登记明细是农业农村部农药管理司公告附件的原文，逐条照录（含登记证号），
    所以卡片上的农药名、有效成分、企业都是可核对的事实，不是模板编的。
    """
    registry = load_pesticide_registry()
    targets = registry.get("targets") or {}
    key = match_target(condition, targets)
    if not key:
        return SAFETY_NOTE

    rows = targets[key]
    # 索引里给的是「登记条目 × 防治对象」的展开对，同一张登记证如果在一份附件里
    # 列了多个防治对象就会重复出现。卡片上说的是「共 N 个产品」，所以先按
    # 登记证号 + 产品名去重，报出来的数目才是真正的条数，不会虚高。
    total = len({(r.get("reg_no") or r.get("product"), r.get("product")) for r in rows})

    # 同一作物的登记排在前面 —— 卡片讲的是这种作物的这种病，先给对得上作物的例子更有用
    crop = (crop or "").strip()
    if crop:
        rows = sorted(rows, key=lambda r: 0 if crop in (r.get("crop") or "") else 1)

    examples = []
    seen_products = set()
    for r in rows:
        product = (r.get("product") or "").strip()
        if not product or product in seen_products:
            continue
        seen_products.add(product)
        detail = "，".join(x for x in (
            (r.get("ingredient") or "").strip(),
            (r.get("form") or "").strip(),
            (r.get("toxicity") or "").strip(),
        ) if x)
        reg_no = (r.get("reg_no") or "").strip()
        holder = (r.get("holder") or "").strip()
        tail = "，".join(x for x in (f"登记证号 {reg_no}" if reg_no else "",
                                     holder) if x)
        examples.append(f"{product}（{detail}；{tail}）" if detail else f"{product}（{tail}）")
        if len(examples) >= _MAX_EXAMPLES:
            break

    if not examples:
        return SAFETY_NOTE

    source = registry.get("source_name", "农业农村部农药管理司《农药登记信息》")
    where = crop if crop else ""
    return (
        f"用药提醒：{source}中，登记用于防治{where}「{key}」的农药产品共 {total} 个。"
        f"例如：{'；'.join(examples)}。"
        f"登记信息随公告更新，以上仅为可查证的真实登记，不构成推广建议 —— {_MEDICATION_TAIL}"
    )


_MARKERS = "①②③④⑤⑥"


def split_control(control: str) -> list[dict]:
    """把模板里的 ①…②…③… 分条文本拆成结构化小节。

    模板统一用 ①②③ 编号，因此按编号切分比按关键词匹配稳得多
    （后者的写法一改就失效）。

    这个函数原来在 `src/llm/mock_provider.py` 里叫 `_split_control`（私有）。
    虫害当主结论时（`src/llm/fusion.py::_apply_pest_primary`）也要用它把虫害防治
    拼成同样形状的段落，所以提到这里成为公开函数 —— 两边各留一份实现迟早会分叉。
    """
    chunks: list[str] = []
    cur: list[str] = []
    for ch in control:
        if ch in _MARKERS:
            if cur:
                chunks.append("".join(cur).strip())
            cur = [ch]
        else:
            cur.append(ch)
    if cur:
        chunks.append("".join(cur).strip())

    sections: list[dict] = []
    for chunk in chunks:
        if not chunk:
            continue
        head, sep, rest = chunk.partition("：")
        title = head.lstrip(_MARKERS).strip()
        sections.append({
            "title": title if sep and title else "防治措施",
            "content": (rest if sep else chunk).strip(),
        })
    return sections or [{"title": "防治措施", "content": control}]


def flatten_plan(sections: list[dict]) -> str:
    """把结构化防治方案压成纯文本，供方言 TTS 直接朗读。

    与 `split_control` 一样，原来在 `src/llm/mock_provider.py` 里是私有的 `_flatten`。
    虫害当主结论时 fusion 会换掉 `treatment_sections`，**必须**同时用这里重算
    `report["treatment_plan"]` —— 只换前者会让两者对不上，而 `treatment_plan` 是被契约检查的。
    """
    return " ".join(f"{s['title']}：{s['content']}" for s in sections)


def pest_plan_sections(pest_type: str) -> list[dict]:
    """按虫害类群（pest_type）给出**报告口径**的防治小节。

    返回的是 `treatment_sections` 的形状（`title` / `content`），与 mock 病害报告完全一致，
    于是前端 `_primaryAction` 的 `ACTION_RE` 能原样匹配到「农业防治 / 物理防治 / 生物防治 /
    化学防治」，不需要为虫害再写一套渲染分支。

    内容与那 102 张虫害知识卡片取自同一处（卡片本身就是由 PEST_TEXT + medication_text 生成的），
    所以这里直接调函数，不去读 1.7 MB 的 library_items.json —— 读卡片只会得到同样的文字，
    还要在 `src/` 里新引入一层对知识库的耦合（`src/` 下本来就没有库加载器）。
    """
    pattern, control = PEST_TEXT.get(pest_type or "", PEST_TEXT["其他"])
    return [
        {"title": "危害规律与判断依据", "content": pattern},
        *split_control(control),
    ]


def _pest_card(entry: dict, label_map: dict) -> dict:
    pt = entry["pathogen_type"]
    pattern, control = PATHOGEN_TEXT.get(pt, PATHOGEN_TEXT["其他"])
    crop, cond = entry["crop"], entry["condition"]
    # 用药提醒优先用农药登记数据里针对本防治对象的真实登记；索引里没有这一项
    # （营养失调、生理性病害等本来就没有对症农药）时回落到通用提示。
    medication = medication_text(cond, crop)

    sections = [
        {"type": "pattern", "title": "发病规律",
         "text": f"{crop}{cond}：{pattern}", "images": []},
        {"type": "control", "title": "防治方法",
         "text": control, "images": _images_of(label_map, entry, limit=3)},
        {"type": "medication", "title": _MEDICATION_TITLE,
         "text": medication, "images": []},
    ]
    if entry["is_mixed"]:
        sections.insert(0, {
            "type": "notice", "title": "复合病害提示",
            "text": f"该类别为复合病害（{cond}），田间往往同时存在多种病原，"
                    "防治时需兼顾各组分病害，不能只针对其中一种。",
            "images": [],
        })

    summary = f"{crop}的{cond}（{pt}性病害）。{pattern[:60]}…"
    return {
        "id": f"lib_pest_{entry['index']}",
        "category": "pest",
        "category_name": "病害防治知识",
        "title": f"{crop}{cond}防治",
        "crop": crop,
        "crop_category": entry["crop_category"],
        "condition": cond,
        "pathogen_type": pt,
        "is_mixed": entry["is_mixed"],
        "is_healthy": False,
        "index": entry["index"],
        "cover": _url_for(entry["cover"]) if entry.get("cover") else None,
        "summary": summary,
        "tags": [crop, cond, pt, entry["crop_category"]],
        "sections": sections,
        "read_text": f"{crop}{cond}。发病规律：{pattern} 防治方法：{control} {medication}",
        "content_source": CONTENT_SOURCE,
    }


def _insect_cover_index() -> dict[int, str]:
    """IP102 数据集里「虫害类 id -> 一张含该类虫体的图片 URL」。

    102 张虫害卡片如果全都没有配图，网格会出现一整片纯文字，和病害卡片的视觉
    严重不一致；而虫害恰恰是**看图认虫**最直接。这里从 data/pest 的 YOLO 标注里
    现取：label txt 每行一个框、行首是该框的类 id，扫一遍就能记下每个类第一次
    出现时对应的那张图。

    按 train → val → test 的顺序扫，setdefault 保证先命中的优先：长尾类在 train 里
    可能一个样本都没有（实测 102 类里有 5 类如此），补扫后两档能把封面凑齐。
    只是读取这些图当缩略图，不参与任何评估计算，评估口径不受影响。

    数据集不存在时返回空字典（卡片退化为无封面），**不抛错**：07 号脚本不该
    因为没跑过 16 号（下载 IP102）就跑不动。
    """
    ds = path_of("pest_dataset")
    if not (ds / "images").is_dir() or not (ds / "labels").is_dir():
        logger.warning("虫害数据集不在位，虫害卡片将没有封面：%s"
                       "（如需封面请先跑 scripts/16_download_ip102.py）", ds)
        return {}

    covers: dict[int, str] = {}
    for split in ("train", "val", "test"):
        img_dir, lbl_dir = ds / "images" / split, ds / "labels" / split
        if not lbl_dir.is_dir():
            continue
        for txt in sorted(lbl_dir.glob("*.txt")):
            img = img_dir / f"{txt.stem}.jpg"
            if not img.exists():
                continue
            try:
                lines = txt.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for line in lines:
                parts = line.split()
                if not parts:
                    continue
                try:
                    cid = int(parts[0])
                except ValueError:
                    continue
                covers.setdefault(cid, f"/media-pest/{split}/{img.name}")
    logger.info("虫害卡片封面索引：%d / 102 个类别找到样图", len(covers))
    return covers


def _insect_card(index: int, entry: dict, cover: str | None) -> dict:
    """一张虫害卡片（IP102 的 102 类之一）。

    与 _pest_card 并列而非复用：正文模板不同源 —— 病害按**病原类型**套 PATHOGEN_TEXT，
    虫害按**取食方式**套 PEST_TEXT（潜叶类必须打在潜入前、刺吸类必须打叶背，
    防治时机完全由口器决定）。但两者的卡片结构刻意做成同构，前端详情页不必分叉。
    """
    ptype = entry["pest_type"]
    pattern, control = PEST_TEXT.get(ptype, PEST_TEXT["其他"])
    name, crop = entry["label_cn"], entry["crop"]
    latin = entry.get("latin", "")
    # 虫害卡片按虫名匹配登记（登记表里「防治对象」就写着稻飞虱、蚜虫这类虫名）。
    # 卡片名多是 IP102 的种名（白背飞虱），匹配靠 match_target 的包含规则归到类群名。
    medication = medication_text(name, crop)

    sections = [
        {"type": "damage", "title": "危害规律",
         "text": f"{crop}{name}（{ptype}）：{pattern}", "images": []},
        {"type": "control", "title": "防治方法",
         "text": control, "images": [cover] if cover else []},
        {"type": "medication", "title": _MEDICATION_TITLE,
         "text": medication, "images": []},
    ]

    return {
        "id": f"lib_insect_{index}",          # 与 lib_pest_<index> 前缀不同，0..101 不会撞 0..270
        "category": "insect",
        "category_name": "虫害防治知识",
        "title": f"{name}防治",
        "crop": crop,
        "crop_category": "",                  # 虫害作物词表与 PDD271 的 47 作物是两套，这里刻意留空
        "condition": name,
        "pathogen_type": "",                  # 虫害没有「病原类型」，类群走 pest_type
        "pest_type": ptype,
        "latin": latin,
        "is_mixed": False,
        "is_healthy": False,
        "index": index,
        "cover": cover,
        "summary": f"{crop}的{name}（{ptype}）。{pattern[:60]}…",
        # 学名过滤掉空串：102 类里有 2 类（灰象甲、橘瘿蚊）在词表里没有可靠的拉丁学名，
        # 硬凑一个属名比留空更糟 —— 留空只会少一个检索词，编错了是事实错误。
        "tags": [t for t in [name, crop, ptype, latin, "虫害"] if t],
        "sections": sections,
        "read_text": f"{crop}{name}。危害规律：{pattern} 防治方法：{control} {medication}",
        "content_source": CONTENT_SOURCE,
    }


def _care_card(crop: str, info: dict, label_map: dict) -> dict:
    classes = info["classes"]
    healthy = [
        label_map["base_info"][c]["condition"]
        for c in classes if label_map["base_info"][c]["is_healthy"]
    ]
    diseases = [c for c in classes if not label_map["base_info"][c]["is_healthy"]]

    care = CARE_TEXT.get(info["category"], CARE_TEXT["蔬菜"])
    sections = [
        {"type": "care", "title": "养护要点", "text": f"{crop}（{info['category']}）：{care}", "images": []},
        {"type": "pattern", "title": "常见病害索引",
         "text": "该作物在本知识库中收录的病害：" + "、".join(diseases) + "。"
                 "点击下方「病害防治知识」标签可查看每一类的详细防治方案。",
         "images": []},
    ]
    if healthy:
        sections.append({
            "type": "notice", "title": "健康植株识别",
            "text": f"本知识库收录了该作物的健康样本类别：{'、'.join(healthy)}。"
                    "田间巡田时可与之对比，发现长势或叶色偏离即需进一步排查。",
            "images": [],
        })

    # 取该作物第一个有封面的类别作为养护卡片的封面
    cover = None
    for name in classes:
        entry = label_map["index_to_label"][str(label_map["label_to_index"][name])]
        if entry.get("cover"):
            cover = _url_for(entry["cover"])
            break

    return {
        "id": f"lib_care_{crop}",
        "category": "care",
        "category_name": "农作物养护知识",
        "title": f"{crop}养护与病害总览",
        "crop": crop,
        "crop_category": info["category"],
        "condition": "养护",
        "pathogen_type": "",
        "is_mixed": False,
        "is_healthy": False,
        "index": None,
        "cover": cover,
        "summary": f"{crop}（{info['category']}）的栽培养护要点，以及该作物已收录的 {len(diseases)} 种病害索引。",
        "tags": [crop, info["category"], "养护", "栽培管理"],
        "sections": sections,
        "read_text": f"{crop}养护要点：{care} 该作物已收录的病害包括：{'、'.join(diseases)}。",
        "content_source": CONTENT_SOURCE,
    }


def _load_pest_label_map() -> dict | None:
    """读 102 类虫害词表；没有就返回 None（知识库退化为只有病害 + 养护）。

    与 label_map.json 是**两套互相独立**的标签空间，所以缺任何一套都不该让
    另一套的卡片生不出来。
    """
    path = path_of("pest_label_map")
    if not path.exists():
        logger.warning("虫害词表不存在，本次不生成虫害卡片：%s"
                       "（如需虫害知识请先跑 scripts/16_download_ip102.py）", path)
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def build_library() -> dict:
    label_map = json.loads(path_of("label_map").read_text(encoding="utf-8"))

    pest_cards = [
        _pest_card(e, label_map)
        for e in (label_map["index_to_label"][str(i)]
                  for i in range(label_map["num_classes"]))
        if not e["is_healthy"]
    ]
    care_cards = [
        _care_card(c["name"], c, label_map) for c in label_map["crop_classes"]
    ]

    pest_lm = _load_pest_label_map()
    insect_cards: list[dict] = []
    if pest_lm is not None:
        covers = _insect_cover_index()
        insect_cards = [
            _insect_card(i, pest_lm["index_to_label"][str(i)], covers.get(i))
            for i in range(pest_lm["num_classes"])
        ]

    items = pest_cards + insect_cards + care_cards

    # 名字里的坑：这个分类键叫 pest，装的却**全是病害**（虫害在 insect 里）。
    # 以前它叫「病虫害防治知识」，加了 102 张真·虫害卡片之后就成了自相矛盾的值，
    # 所以改成「病害防治知识」。分类键不动，首页地图的 lib_pest_* 深链与
    # ?category=pest 过滤都照旧。
    categories = [
        {"key": "pest", "name": "病害防治知识", "count": len(pest_cards)},
        {"key": "insect", "name": "虫害防治知识", "count": len(insect_cards)},
        {"key": "care", "name": "农作物养护知识", "count": len(care_cards)},
    ]
    data = {
        "version": 2,
        "content_source": CONTENT_SOURCE,
        "disclaimer": (
            "「用药提醒」中的农药为农业农村部农药管理司《农药登记信息》的公开登记条目"
            "（无对应登记的卡片为通用提示），具体剂型与用量请以当地植保部门指导与"
            "农药登记标签为准。"
        ),
        "categories": categories,
        "total": len(items),
        "items": items,
    }
    return data


def build_and_save() -> dict:
    data = build_library()
    out = path_of("library_dir") / "library_items.json"
    write_json(out, data)
    logger.info("已写出知识图书库：%s（%d 张卡片）", out, data["total"])
    for c in data["categories"]:
        logger.info("    %-8s %s：%d 张", c["key"], c["name"], c["count"])

    docs = _dump_rag_documents(data)
    logger.info("已写出 RAG 切分源：%d 个文本文件 -> %s", len(docs), path_of("documents_dir"))

    # 图书库页的本地快照跟着重打一次 —— 与 province_pest.py 末尾重打首页快照是同一套约定，
    # 免得出现「重建了知识库、页面却还显示旧快照」却没人管的情况。
    build_library_preload(data, quiet=True)
    return data


def _dump_rag_documents(data: dict) -> list[Path]:
    """把卡片正文渲染成纯文本，供 4.4 的 RAG 建索引使用（一卡一文件）。"""
    out_dir = ensure_dir(path_of("documents_dir"))
    paths: list[Path] = []
    for item in data["items"]:
        lines = [
            f"标题：{item['title']}",
            f"分类：{item['category_name']}",
            f"作物：{item['crop']}（{item['crop_category']}）",
            f"内容来源：{item['content_source']}",
            "",
        ]
        for sec in item["sections"]:
            lines += [f"【{sec['title']}】", sec["text"], ""]
        path = out_dir / f"{item['id']}.txt"
        write_text(path, "\n".join(lines))
        paths.append(path)
    return paths


if __name__ == "__main__":
    build_and_save()
