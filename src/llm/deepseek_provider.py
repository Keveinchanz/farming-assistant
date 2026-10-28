"""任务 3.3 —— 封装大模型 API（DeepSeek，OpenAI 兼容协议）。

为什么用 OpenAI 兼容协议：DeepSeek 的 /chat/completions 与 OpenAI 一致，
因此这里写一次，日后换任何兼容服务（通义千问兼容模式、Kimi、本地 vLLM 等）只改
.env 里的 DEEPSEEK_BASE_URL 与 DEEPSEEK_MODEL 即可，不用动代码。

两个稳健性措施：
    · response_format={"type": "json_object"} 强制 JSON 输出；
    · 解析失败时追加一条"只输出 JSON"的纠正消息重试一次，仍失败才抛错 ——
      避免因为模型偶发多说一句话就让整个诊断接口 500。
"""

from __future__ import annotations

import json
import re
from typing import Any

from src.common.config import env
from src.common.logging_utils import setup_logger
from src.llm.base import BaseLLM
from src.llm.prompts import (
    ASSISTANT_SYSTEM_PROMPT,
    QA_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
    build_assistant_user_prompt,
    build_qa_prompt,
    build_user_prompt,
)

logger = setup_logger("agri.llm.deepseek")

REQUIRED_FIELDS = ("disease_name", "confidence", "symptom_match",
                   "treatment_sections", "medication_reminder")

# 追问回答若被模型包成 JSON，按这个顺序取正文。未在此列出的键（如 disease_name、
# confidence）是元信息，不进正文。
ANSWER_TEXT_KEYS = ("symptom_match", "answer", "conclusion", "summary",
                    "prevention_advice", "treatment_plan", "treatment_sections",
                    "medication_reminder", "additional_info", "safety_note")


class DeepSeekProvider(BaseLLM):
    provider = "deepseek"

    def __init__(self, *, api_key: str | None = None) -> None:
        self.api_key = api_key or env("DEEPSEEK_API_KEY")
        if not self.api_key:
            raise ValueError("缺少 DEEPSEEK_API_KEY，请写入 .env")

        self.base_url = str(env("DEEPSEEK_BASE_URL", "https://api.deepseek.com")).rstrip("/")
        self.model = str(env("DEEPSEEK_MODEL", "deepseek-chat"))
        self.timeout = int(env("LLM_TIMEOUT", 60, cast="int") or 60)
        logger.info("大模型：DeepSeek（%s / %s）", self.base_url, self.model)

    # ------------------------------------------------------------ 底层调用

    def _chat(self, messages: list[dict], *, json_mode: bool = True,
              temperature: float = 0.3, max_tokens: int = 1600,
              timeout: float | None = None) -> str:
        import requests

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        r = requests.post(
            f"{self.base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=timeout or self.timeout,
        )
        if r.status_code >= 400:
            raise RuntimeError(f"DeepSeek 调用失败 HTTP {r.status_code}：{r.text[:300]}")
        return r.json()["choices"][0]["message"]["content"]

    @staticmethod
    def _parse_json(text: str) -> dict:
        """容错解析：模型偶尔会套一层 markdown 代码块，这里先剥掉再解析。"""
        text = (text or "").strip()
        if text.startswith("```"):
            text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            m = re.search(r"\{.*\}", text, re.S)
            if not m:
                raise
            return json.loads(m.group(0))

    @staticmethod
    def _plain_text(text: str) -> str:
        """把模型可能返回的 JSON 收敛成可直接显示的正文。

        追问走的是问答提示，正常情况下返回纯文本。但模型仍有概率把答案包成
        {"disease_name": "", "symptom_match": "...", "prevention_advice": "..."}
        —— 前端按纯文本渲染，用户看到的就是一坨花括号。这里按 ANSWER_TEXT_KEYS
        取出正文拼成段落，并把「。2.」这种挤在一行的编号拆到新行。

        不是 JSON 就原样返回，不做任何改写。
        """
        raw = (text or "").strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```[a-zA-Z]*\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw).strip()

        if raw.startswith("{"):
            data = None
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                m = re.search(r"\{.*\}", raw, re.S)
                if m:
                    try:
                        data = json.loads(m.group(0))
                    except json.JSONDecodeError:
                        data = None
            if isinstance(data, dict):
                raw = "\n".join(DeepSeekProvider._answer_parts(data))

        # 编号挤在同一行时拆行：句读（。；！？）之后紧跟「2. 」「3、」才断
        raw = re.sub(r"(?<=[。；;！!？?])\s*(?=[1-9]\d?[.、)）]\s*\S)", "\n", raw)
        return raw.strip()

    @staticmethod
    def _answer_parts(data: dict) -> list[str]:
        """按 ANSWER_TEXT_KEYS 顺序取出正文，去重后返回。"""
        parts: list[str] = []
        for key in ANSWER_TEXT_KEYS:
            val = data.get(key)
            if isinstance(val, str) and val.strip():
                parts.append(val.strip())
            elif isinstance(val, list):
                for item in val:
                    if isinstance(item, dict):
                        content = str(item.get("content") or "").strip()
                        title = str(item.get("title") or "").strip()
                        if content:
                            parts.append(f"{title}：{content}" if title else content)
                    elif isinstance(item, str) and item.strip():
                        parts.append(item.strip())

        # 模型常把同一段话既写进 summary 又写进 symptom_match，去掉重复
        seen: set[str] = set()
        unique: list[str] = []
        for p in parts:
            if p in seen or p in "".join(unique):
                continue
            seen.add(p)
            unique.append(p)
        return unique

    # ------------------------------------------------------------ 对外


    def generate_report(self, context: dict[str, Any]) -> dict:
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_user_prompt(context)},
        ]

        content = self._chat(messages)
        try:
            data = self._parse_json(content)
        except json.JSONDecodeError:
            logger.warning("大模型返回的不是合法 JSON，追加纠正指令后重试一次")
            messages.append({"role": "assistant", "content": content})
            messages.append({"role": "user",
                             "content": "你上一次的输出不是合法 JSON。请只输出 JSON 对象本身，"
                                        "不要任何解释文字或代码块标记。"})
            data = self._parse_json(self._chat(messages))

        return self._normalize(data, context)

    def answer(self, question: str, *, context: str = "",
               system: str | None = None) -> str:
        """问答。

        `system` 为空时沿用诊断的系统提示（调用方要的是可解析 JSON）；
        传入 QA_SYSTEM_PROMPT 时返回可直接显示的正文。
        不能无条件换成问答提示：叙述层与语音解析都在解析这条返回值的 JSON。
        """
        messages = [
            {"role": "system", "content": system or SYSTEM_PROMPT},
            {"role": "user", "content": build_qa_prompt(question, context)},
        ]
        text = self._chat(messages, json_mode=False, temperature=0.5, max_tokens=800)
        return self._plain_text(text) if system else text.strip()

    # ------------------------------------------------------------ AI 助手（宠物）

    def reply(self, message: str, *, context: str = "",
              history: list[dict[str, str]] | None = None) -> str:
        """自由对话。超时压到 10 秒：宠物是交互入口，宁可回退模板也不让用户干等。"""
        messages: list[dict] = [{"role": "system", "content": ASSISTANT_SYSTEM_PROMPT}]
        for turn in (history or [])[-8:]:
            role = str(turn.get("role") or "")
            content = str(turn.get("content") or "")[:400]
            if role in ("user", "assistant") and content:
                messages.append({"role": role, "content": content})
        messages.append({"role": "user",
                         "content": build_assistant_user_prompt(message, page=context)})
        return self._chat(messages, json_mode=False, temperature=0.6,
                          max_tokens=300, timeout=10).strip()

    def classify_intent(self, text: str, labels: list[str]) -> dict:
        prompt = (
            "你是农业平台的意图分类器。只输出 JSON，不要解释。\n"
            '格式：{"intent": "<标签>", "confidence": 0.0}\n'
            f"可选标签：{', '.join(labels)}\n"
            "拿不准就输出 chat，并把 confidence 给低一点。\n"
            f"用户原话：{text}"
        )
        raw = self._chat(
            [{"role": "system", "content": "你是严格的 JSON 分类器，只输出 JSON。"},
             {"role": "user", "content": prompt}],
            json_mode=True, temperature=0, max_tokens=100, timeout=8,
        )
        data = self._parse_json(raw)
        name = str(data.get("intent") or "")
        if name not in labels:
            return {}
        try:
            confidence = float(data.get("confidence") or 0.7)
        except (TypeError, ValueError):
            confidence = 0.7
        return {"intent": name, "confidence": confidence}

    # ------------------------------------------------------------ 结果规整

    def _normalize(self, data: dict, context: dict) -> dict:
        """把模型输出对齐到接口契约，并做一次闭集校验。"""
        missing = [f for f in REQUIRED_FIELDS if not data.get(f)]
        if missing:
            logger.warning("大模型输出缺少字段 %s，将由后续默认值补齐", missing)

        candidates = {c["label_cn"] for c in (context.get("candidates") or [])}
        name = str(data.get("disease_name", "")).strip()

        if candidates and name not in candidates:
            # 模型跑出了候选集 —— 换成图像识别的 top1，并如实记录这次纠正
            fallback = (context.get("image_result") or {}).get("top1")
            corrected = fallback["label_cn"] if fallback else (sorted(candidates)[0] if candidates else name)
            logger.warning("大模型给出候选集之外的病害名 %r，已纠正为 %r", name, corrected)
            data["disease_name"] = corrected
            data["_corrected_from"] = name
        else:
            data["disease_name"] = name

        try:
            data["confidence"] = round(float(data.get("confidence", 0.0)), 4)
        except (TypeError, ValueError):
            data["confidence"] = 0.0
        data["confidence"] = min(1.0, max(0.0, data["confidence"]))

        sections = data.get("treatment_sections") or []
        if isinstance(sections, dict):        # 模型偶尔给成 {"农业防治": "..."} 形式
            sections = [{"title": k, "content": v} for k, v in sections.items()]
        clean_sections = []
        for s in sections:
            if isinstance(s, dict) and s.get("content"):
                clean_sections.append({"title": str(s.get("title", "防治措施")),
                                       "content": str(s["content"])})
            elif isinstance(s, str) and s.strip():
                clean_sections.append({"title": "防治措施", "content": s})
        data["treatment_sections"] = clean_sections

        diff = data.get("differential") or []
        data["differential"] = [str(x) for x in diff][:5] if isinstance(diff, list) else []

        self._normalize_pest(data, context)

        data.setdefault("medication_reminder", "")
        data.setdefault("safety_note", "具体用药请以当地植保部门指导和农药登记标签为准。")
        data.setdefault("summary", "")
        data["source"] = self.provider
        return data

    @staticmethod
    def _normalize_pest(data: dict, context: dict) -> None:
        """规整虫害字段，并做一次**白名单**过滤。

        病害那边靠「候选列表」这个闭集防幻觉；虫害这边没有等价的闭集
        （102 类昆虫不可能全塞进提示词），于是换成**对着检测结果反查**：
        模型给出的虫害名必须在检测结果里出现过，否则一律丢掉。
        效果一样 —— 模型不能凭空造出一条检测器没看见的虫害。

        三种状态照原样保持，不在这里"补默认值"：
            检测器没装 → 删掉这个字段（模型若自己编了，正好在这里被清掉）
            装了但没检出 → 空列表
            装了且有检出 → 过滤后的列表
        """
        detections = context.get("pest_detections")
        if detections is None:
            # 没跑检测，就不该有这个字段。模型幻觉出来的直接删。
            if data.pop("pest_findings", None):
                logger.warning("本次未运行虫害检测，但大模型返回了 pest_findings，已丢弃")
            return

        allowed = {str(d.get("label_cn") or "").strip() for d in detections}
        raw = data.get("pest_findings") or []
        if not isinstance(raw, list):
            raw = []

        clean: list[dict] = []
        dropped: list[str] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "")).strip()
            if not name:
                continue
            if name not in allowed:
                dropped.append(name)
                continue
            try:
                conf = round(float(item.get("confidence", 0.0)), 4)
            except (TypeError, ValueError):
                conf = 0.0
            clean.append({
                "name": name,
                "confidence": min(1.0, max(0.0, conf)),
                "advice": str(item.get("advice", "")).strip(),
            })
            if len(clean) >= 3:
                break

        if dropped:
            logger.warning("大模型给出了检测结果之外的虫害名 %s，已丢弃", dropped)
        data["pest_findings"] = clean

    def describe(self) -> dict:
        return {
            "provider": self.provider,
            "base_url": self.base_url,
            "model": self.model,
            "json_mode": True,
        }
