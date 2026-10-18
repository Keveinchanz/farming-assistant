#!/usr/bin/env python
"""任务 1.4 —— 建立标签映射字典：中文名称 ↔ 数字索引 ↔ 病虫害基础信息。

产出：data/label_map.json（全项目单一事实源）

用法：
    python scripts/03_build_label_map.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.common.logging_utils import setup_logger   # noqa: E402
from src.data.label_map import build_and_save       # noqa: E402

logger = setup_logger("agri.label_map")


def main() -> int:
    build_and_save()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
