"""命令行入口：python -m skills list / show / run。

从任意目录运行都可以：先把项目根放进 sys.path，再导入 skills。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _read_payload(path: str) -> dict:
    if path == "-":
        return json.load(sys.stdin)
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

    from skills.errors import SkillError
    from skills.registry import SKILLS, execute, get_skill

    parser = argparse.ArgumentParser(prog="python -m skills", description="模型技能命令行")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="列出全部技能")

    show = sub.add_parser("show", help="查看技能 manifest 与 JSON Schema")
    show.add_argument("name")

    run = sub.add_parser("run", help="执行技能")
    run.add_argument("name")
    run.add_argument("--input", required=True, help="JSON 文件路径；- 表示 stdin")
    run.add_argument("--pretty", action="store_true", help="缩进输出")

    args = parser.parse_args(argv)

    if args.cmd == "list":
        for spec in SKILLS.values():
            from skills.availability import is_available

            flag = "可用" if is_available(spec.name) else "不可用"
            print(f"{spec.name}\t{spec.version}\t{flag}\t{spec.title}")
        return 0

    try:
        if args.cmd == "show":
            print(json.dumps(get_skill(args.name).manifest(), ensure_ascii=False, indent=2))
            return 0

        payload = _read_payload(args.input)
        result = execute(args.name, payload, allow_paths=True)
        print(json.dumps(result, ensure_ascii=False, indent=2 if args.pretty else None,
                         default=str))
        return 0
    except SkillError as e:
        print(f"技能失败：{e.message}", file=sys.stderr)
        return 2
    except json.JSONDecodeError as e:
        print(f"输入不是合法 JSON：{e}", file=sys.stderr)
        return 2
    except Exception as e:  # noqa: BLE001 —— CLI 把可读错误给用户，不甩整段 traceback
        print(f"执行失败：{type(e).__name__}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
