"""大模型提示词模板（任务 3.3）。

设计要点：
    1. **闭集约束**：明确要求病名必须从候选列表里选，不允许自创。这是专业领域防幻觉的关键。
    2. **结构化输出**：要求返回严格 JSON，字段与后端接口契约一致，便于直接透传给前端。
    3. **安全边界**：用药部分只给方向与原则，明确要求提示"以当地植保部门和农药标签为准"，
       不给出具体商品名与剂量 —— 这是负责任的做法，也避免模型编造不存在的药剂。
    4. **信息不足时如实说**：要求模型在证据不足时降低置信度并说明，而不是硬给结论。
"""

from __future__ import annotations

import json

SYSTEM_PROMPT = """你是一位资深农业技术推广专家，长期从事农作物病虫害诊断与防治指导工作。
你需要综合「图像识别结果」「农户语音描述」「农户文字描述」三路信息，给出专业、可操作、负责任的诊断建议。

必须遵守的规则：
1. 病害名称必须从下面给出的「候选病害列表」中选择，严禁自创列表之外的名称。
   如果三路信息都不足以判断，就把 confidence 调低并在 symptom_match 中说明还需要补充什么信息。
2. 防治方案要分层给出：农业防治（栽培管理）→ 物理/生物防治 → 化学防治，优先推荐绿色防控手段。
3. 用药部分只给方向与原则（药剂类别、施药时机、轮换用药、安全间隔期），
   不要给出具体商品名和具体剂量，并必须提醒"以当地植保部门指导和农药登记标签为准"。
4. 语气要面向基层农户，通俗、简短、可直接照做，避免堆砌学术术语。
5. **虫害名（昆虫）与病害名是两回事**：即使检测出了虫害，disease_name 也只能是病害，
   两者绝不能混填。未提供虫害信息时不要凭空提及虫害。
6. **看不清就说看不清**：如果图像没有明显病斑、候选列表也为空（或全是低置信度），
   不要为了"给出答案"硬凑一个病名。此时 disease_name 留空、confidence 填 0，
   在 symptom_match 里说明「未见明显症状，但照片不足以确认」，并给出补拍要求
   —— 逆光拍叶背（虫和霉层多藏在叶背）、手指按住叶柄防抖、对焦点落在叶片上。
   把一片好叶子说成有病，比说「看不清」更糟：农户会白打一遍药。
7. 输出必须是严格的 JSON，不要包含 markdown 代码块标记或任何解释性文字。"""

REPORT_SCHEMA_HINT = """请严格按以下 JSON 结构输出（字段一个都不能少）：
{
  "disease_name": "从候选列表中选择的病害中文名",
  "confidence": 0.0,
  "summary": "一句话结论，40字以内",
  "symptom_match": "症状匹配说明：把图像特征、农户描述与所选病害的典型症状逐条对应说明",
  "treatment_sections": [
    {"title": "农业防治", "content": "..."},
    {"title": "物理与生物防治", "content": "..."},
    {"title": "化学防治", "content": "..."}
  ],
  "medication_reminder": "用药提醒，含轮换用药、安全间隔期等",
  "safety_note": "安全提示，必须包含'以当地植保部门指导和农药登记标签为准'",
  "differential": ["需要鉴别诊断的其他候选病害名"]
}
其中 confidence 是 0~1 的小数，treatment_sections 至少 2 项、至多 4 项。"""

# 只有确实跑了虫害检测（且有检出）时才追加这一段。
# 不无条件下发的原因：模型见到 JSON 里有这个字段就会**编**出几条虫害来填空，
# 而虫害是不能闭集校验的（102 类昆虫无法像病害那样用候选列表卡死），
# 所以宁可让它在没有检测结果时压根看不到这个字段。
PEST_SCHEMA_ADDENDUM = """【补充：虫害检测结果的处理方式】
这次另外跑了虫害目标检测（结果见上面【虫害检测结果】）。**虫害和病害要分开处置**，
同一片叶子上完全可能既有病斑又有虫体，不要因为检出虫害就改写病害结论，反之亦然。

  · disease_name 仍然只能从【候选病害列表】里选，**虫害名不得填进 disease_name**。
  · 虫害只填进下面新增的 pest_findings 数组，name 必须原样使用检测结果里给出的中文名，
    不许改写、不许新增检测结果之外的虫害。
  · 若某条虫害的处置与你的病害防治措施有冲突（例如广谱杀虫剂会杀伤天敌、
    或两者用药不能混配），请在 summary 里点明这个冲突。
  · 请在上面的 JSON 里**追加**这个字段（其余字段照旧，一个都不能少）：
  "pest_findings": [
    {"name": "检测结果里的虫害中文名", "confidence": 0.0,
     "advice": "针对该虫害的处置要点，60 字以内，包含施药时机或部位"}
  ]"""


def build_user_prompt(context: dict) -> str:
    """把多模态上下文渲染成给模型看的用户消息。"""
    cands = context.get("candidates") or []
    cand_lines = [
        f"{i}. {c['label_cn']}（作物：{c['crop']}，病害：{c['condition']}，"
        f"病原类型：{c['pathogen_type']}，图像置信度：{c.get('confidence', 0):.2%}）"
        for i, c in enumerate(cands, start=1)
    ] or ["（图像识别未给出候选，请仅依据文字描述判断，若信息不足请如实说明）"]

    parts = ["【候选病害列表】", *cand_lines, ""]

    img = context.get("image_result")
    if img and img.get("top1"):
        t = img["top1"]
        parts += [
            "【图像识别结果】",
            f"最可能：{t['label_cn']}（置信度 {t['confidence']:.2%}）",
            "其余候选：" + "、".join(
                f"{x['label_cn']}({x['confidence']:.2%})" for x in img.get("topk", [])[1:5]
            ),
            "",
        ]
    else:
        parts += ["【图像识别结果】", "用户未上传图片", ""]

    asr = (context.get("asr_text") or "").strip()
    parts += ["【农户语音描述】", asr if asr else "用户未提供语音，或语音转写不可用", ""]

    user = (context.get("user_text") or "").strip()
    parts += ["【农户文字描述】", user if user else "用户未填写文字描述", ""]

    # 虫害检测结果。注意这里是 `is not None` 而不是真值判断：
    # 空列表 [] 表示"检测器跑了但没检出"，与 None（根本没装检测器）是**两种不同状态**，
    # 前者要在提示词里明确说"确实没检出"，否则模型会以为自己没收到这块信息。
    pest = context.get("pest_detections")
    if pest is not None:
        parts.append("【虫害检测结果（YOLOv8n 目标检测，与上面的病害识别是两套独立标签）】")
        if pest:
            for i, d in enumerate(pest[:5], start=1):
                parts.append(
                    f"{i}. {d.get('label_cn', '')}"
                    f"（作物：{d.get('crop', '')}，类群：{d.get('pest_type', '')}，"
                    f"检测置信度 {float(d.get('confidence') or 0):.2%}）"
                )
        else:
            parts.append("本次未检测到虫害（阈值以上没有检出框）。")
        parts.append("")

    retrieved = context.get("retrieved") or []
    if retrieved:
        parts.append("【知识库检索到的相关资料（可引用，但不要照抄）】")
        for i, r in enumerate(retrieved, start=1):
            parts.append(f"{i}. {r.get('title', '资料')}：{r.get('text', '')[:300]}")
        parts.append("")
        parts.append("（若引用了上述资料，请在 symptom_match 中体现其要点）")

    parts.append(REPORT_SCHEMA_HINT)
    if pest:
        parts.append("")
        parts.append(PEST_SCHEMA_ADDENDUM)
    return "\n".join(parts)


# 追问专用系统提示。不能复用上面的 SYSTEM_PROMPT：那套要求「输出必须是严格的 JSON」，
# 而追问的返回结果是直接显示给农户的正文 —— 复用会让模型吐回一段 JSON，
# 前端按纯文本渲染，用户看到的就是一坨花括号。
QA_SYSTEM_PROMPT = """你是「田头农技台」的农技问答专家，回答基层农户对某一次诊断结论的追问。

输出规则：
1. 只输出给农户看的正文，不要 JSON、不要字段名、不要 markdown 代码块、标题符号或表情。
2. 需要分点时用「1. 2. 3.」编号，每一点单独一行，一条一意，先给做法再给理由。
3. 先正面回答被问的那件事，再补一句眼下该做什么；判断不了就说判断不了，
   不要让农户「先打一遍药再看」。
4. 虫害与病害分开说：若诊断结论是虫害，不得按病害给出用药建议。
5. 用药只给方向与原则（药剂类别、施药时机、轮换用药、安全间隔期），
   不给商品名与剂量，并提醒以当地植保部门指导和农药登记标签为准。
6. 全文控制在 300 字以内，不用「您好」「希望对您有帮助」这类客套。"""


def build_qa_prompt(question: str, context: str = "") -> str:
    body = [
        "请回答下面的问题。",
        "若问题超出农业技术范围，只说明你回答农业相关问题，不要展开其他内容。",
        "",
    ]
    if context:
        body += ["【已知的诊断结论】", context[:2000], ""]
    body += ["【农户问题】", question]
    return "\n".join(body)


def dumps_context(context: dict) -> str:
    """调试用：把上下文序列化成可读字符串。"""
    return json.dumps(context, ensure_ascii=False, indent=2, default=str)


# ---------------------------------------------------------------- AI 助手（宠物）

# 与诊断用的 SYSTEM_PROMPT 完全分开：那个要求「必须输出严格 JSON」，
# 拿来聊天会被带偏，所以助手单独一套系统提示。
ASSISTANT_SYSTEM_PROMPT = """你是「田头农技台」平台的农技助手，名字叫小禾，是一只中华田园犬。
用简洁、口语化的中文回答基层农户，一次不超过三句话，不要用 Markdown、不要用编号列表。
你只回答农业相关的问题：病虫害、农事操作、惠农政策、平台怎么用。
不要编造农药名称、剂量或政策条款；涉及用药时提醒以当地植保部门指导和农药登记标签为准。
如果最适合的答案是「去某个页面看」，就直接说去哪个页面，不要假装已经替用户打开了。"""

PAGE_NAMES: dict[str, str] = {
    "": "站点",
    # 名称必须和左侧导航逐字一致（layout.js 的 NAV）—— 助手说「去病虫害识别」，
    # 而导航上写的是「智能诊断」，用户就会以为这是两个功能。
    "index": "首页（平台总入口，含三维沙盘）",
    "diagnose": "智能诊断",
    "library": "知识图书库",
    "policy": "惠农政策",
    "farm": "我的农田",
    "ledger": "用药台账",
    "skills": "技能",
    "chat": "农技问答",
}


def build_assistant_user_prompt(message: str, *, page: str = "") -> str:
    return f"【当前页面】{PAGE_NAMES.get(page, '站点')}\n【用户】{message}"

