"""技能层错误类型。

与 HTTP 层解耦：技能只抛自己的异常，由 `backend/app/routers/skills.py` 映射成
统一的 `ApiError`。这样 CLI 直接运行时不会看到 FastAPI 的依赖。
"""

from __future__ import annotations


class SkillError(Exception):
    """所有技能异常的基类。"""

    code = "skill_error"
    http_status = 400

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class SkillInputError(SkillError):
    """输入不合法：缺字段、base64 损坏、HTTP 传了本地路径、日期倒置等。"""

    code = "skill_bad_input"
    http_status = 400


class SkillNotFoundError(SkillError):
    """请求了不存在的技能名。"""

    code = "skill_not_found"
    http_status = 404


class SkillUnavailableError(SkillError):
    """技能依赖的模型不可用。"""

    code = "skill_unavailable"
    http_status = 503


class SkillPayloadTooLargeError(SkillError):
    """请求体或解码后的文件超过上限。"""

    code = "skill_payload_too_large"
    http_status = 413


__all__ = [
    "SkillError",
    "SkillInputError",
    "SkillNotFoundError",
    "SkillUnavailableError",
    "SkillPayloadTooLargeError",
]
