"""病虫害检测 + 多模态融合诊断技能。

输入可以是图片、语音、文字中的任意组合。HTTP 层只允许 base64（避免读取服务器
任意文件），CLI 允许本地路径。真正的编排仍走 `src.llm.fusion.DiagnosisFusion`，
技能层不复制任何推理逻辑。
"""

from __future__ import annotations

import base64
import binascii
from pathlib import Path

from skills.errors import (
    SkillInputError,
    SkillPayloadTooLargeError,
    SkillUnavailableError,
)
from skills.schemas import CropDiagnosisInput

# 与后端上传上限对齐。base64 会膨胀约 33%，HTTP 路由另有 30 MB 的请求体上限。
MAX_FILE_BYTES = 20 * 1024 * 1024


def _decode_base64(value: str, label: str) -> bytes:
    text = (value or "").strip()
    if text.lower().startswith("data:") and "," in text:
        text = text.split(",", 1)[1]
    try:
        data = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError) as e:
        raise SkillInputError(f"{label}的 base64 解码失败") from e
    if not data:
        raise SkillInputError(f"{label}为空")
    if len(data) > MAX_FILE_BYTES:
        raise SkillPayloadTooLargeError(f"{label}超过 {MAX_FILE_BYTES // 1024 // 1024} MB")
    return data


def _read_path(value: str, label: str) -> bytes:
    path = Path(value).expanduser()
    if not path.is_file():
        raise SkillInputError(f"{label}文件不存在：{path}")
    if path.stat().st_size > MAX_FILE_BYTES:
        raise SkillPayloadTooLargeError(f"{label}超过 {MAX_FILE_BYTES // 1024 // 1024} MB")
    try:
        return path.read_bytes()
    except OSError as e:
        raise SkillInputError(f"{label}读取失败：{e}") from e


def run(payload: dict, *, allow_paths: bool = True) -> dict:
    req = CropDiagnosisInput.model_validate(payload)

    if not allow_paths and (req.image_path or req.audio_path):
        raise SkillInputError(
            "HTTP 调用不支持本地路径，请改用 image_base64 / audio_base64"
        )

    image = (
        _decode_base64(req.image_base64, "图片")
        if req.image_base64 else
        _read_path(req.image_path, "图片") if req.image_path else None
    )
    audio = (
        _decode_base64(req.audio_base64, "音频")
        if req.audio_base64 else
        _read_path(req.audio_path, "音频") if req.audio_path else None
    )

    if image is None and audio is None and not (req.text or "").strip():
        raise SkillInputError("请至少提供图片、语音或文字中的一项")

    from src.llm.fusion import get_fusion

    try:
        fusion = get_fusion()
    except FileNotFoundError as e:
        raise SkillUnavailableError(f"图像识别模型不可用：{e}") from e

    return fusion.diagnose(
        image_bytes=image,
        audio_bytes=audio,
        audio_format=req.audio_format,
        user_text=(req.text or "").strip(),
        dialect=req.dialect,
        topk=req.topk,
    )


__all__ = ["run", "MAX_FILE_BYTES"]
