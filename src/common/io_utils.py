"""读写工具：JSON（UTF-8 无转义）、CSV、目录创建、图片后缀判定。"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Iterable, Sequence

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}


def ensure_dir(path: str | Path) -> Path:
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def read_json(path: str | Path, default: Any = None) -> Any:
    p = Path(path)
    if not p.exists():
        if default is not None:
            return default
        raise FileNotFoundError(f"JSON 文件不存在：{p}")
    with p.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: str | Path, obj: Any, *, indent: int = 2) -> Path:
    """写 JSON。ensure_ascii=False 让中文类别名保持可读，不转成 \\uXXXX。"""
    p = Path(path)
    ensure_dir(p.parent)
    with p.open("w", encoding="utf-8", newline="\n") as f:
        json.dump(obj, f, ensure_ascii=False, indent=indent)
        f.write("\n")
    return p


def write_text(path: str | Path, text: str) -> Path:
    p = Path(path)
    ensure_dir(p.parent)
    p.write_text(text, encoding="utf-8")
    return p


def read_csv(path: str | Path) -> list[dict]:
    with Path(path).open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def write_csv(path: str | Path, rows: Iterable[dict], fieldnames: Sequence[str]) -> Path:
    p = Path(path)
    ensure_dir(p.parent)
    with p.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(fieldnames))
        writer.writeheader()
        writer.writerows(rows)
    return p


def list_images(root: str | Path, *, recursive: bool = True) -> list[Path]:
    """收集目录下的图片。

    用 iterdir/rglob('*') 后按 suffix.lower() 过滤，而不是 rglob('*.JPG')：
    本数据集扩展名全是大写 .JPG，且部分类别多嵌套一层目录，这样写两种情况都不会漏。
    """
    root = Path(root)
    it = root.rglob("*") if recursive else root.glob("*")
    return sorted(p for p in it if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES)
