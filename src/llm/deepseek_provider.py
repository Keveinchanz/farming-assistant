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
from src.llm.prompts import SYSTEM_PROMPT, build_qa_prompt, build_user_prompt

logger = setup_logger("agri.llm.deepseek")

REQUIRED_FIELDS = ("disease_name", "confidence", "symptom_match",
                   "treatment_sections", "medication_reminder")


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
              temperature: float = 0.3, max_tokens: int = 1600) -> str:
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
            timeout=self.timeout,
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

    def answer(self, question: str, *, context: str = "") -> str:
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_qa_prompt(question, context)},
        ]
        return self._chat(messages, json_mode=False, temperature=0.5, max_tokens=800).strip()

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
