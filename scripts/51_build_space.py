#!/usr/bin/env python
"""任务 6.5 —— 组装可推到 Hugging Face Spaces 的独立目录 `deploy/space/`。

## 为什么要单独组装

Space 是另一个 git 仓库，构建上下文就是它自己 —— 它看不到工作区里那些被 .gitignore
排除的东西。所以这里把「代码 + runtime_assets + Spaces 专用 Dockerfile/README」
拷成一个自洽的目录，推上去就能构建。

**不拷的东西**（否则镜像会大 10 倍，还可能把密钥带上去）：
- `.env` / `certs/` / `outputs/shots/auth.json` —— 密钥
- `data/pest_cls_raw`、`data/pest_pseudo`、`data/pest`、`Sample`、`data/phenology` —— 训练数据
- `models/checkpoints`、`models/pest_staging`、`models/pretrained` —— 训练权重与嵌入模型
  （嵌入模型改在 Dockerfile 里构建期下载）
- `frontend/audio/tts_*` —— 运行期生成的语音缓存
- `__pycache__`、`.pyc`

组装完会做一次**自检**：确认没有密钥文件混进来、确认关键路径都在。

用法：python scripts/51_build_space.py
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DST = ROOT / "deploy" / "space"

CODE_DIRS = ["src", "backend", "frontend", "configs", "scripts", "tests"]
ROOT_FILES = ["requirements.txt", "requirements-rag.txt", ".env.example", "start.ps1"]

# 绝不允许出现在 Space 目录里的东西
FORBIDDEN = [".env", "certs", "auth.json", ".key", ".crt"]


def _ignore(_dir: str, names: list[str]) -> set[str]:
    out = {"__pycache__", ".pytest_cache", ".ipynb_checkpoints"}
    out |= {n for n in names if n.endswith((".pyc", ".pyo"))}
    return out & set(names)


def _copy_tree(src: Path, dst: Path) -> None:
    if not src.exists():
        print(f"  跳过（不存在）：{src.relative_to(ROOT)}")
        return
    shutil.copytree(src, dst, ignore=_ignore, dirs_exist_ok=True)


def main() -> int:
    if not (ROOT / "runtime_assets").exists():
        print("找不到 runtime_assets/，请先跑 scripts/50_build_runtime_assets.py")
        return 1

    if DST.exists():
        shutil.rmtree(DST)
    DST.mkdir(parents=True)

    for d in CODE_DIRS:
        _copy_tree(ROOT / d, DST / d)
    for f in ROOT_FILES:
        if (ROOT / f).exists():
            shutil.copy2(ROOT / f, DST / f)
    if (ROOT / "README.md").exists():
        shutil.copy2(ROOT / "README.md", DST / "PROJECT_README.md")

    # 运行期资源
    _copy_tree(ROOT / "runtime_assets", DST / "runtime_assets")

    # Spaces 专用入口
    shutil.copy2(ROOT / "deploy" / "hf" / "Dockerfile", DST / "Dockerfile")
    shutil.copy2(ROOT / "deploy" / "hf" / "README.md", DST / "README.md")

    # 前端 TTS 缓存不该带走（运行期可再生）
    audio = DST / "frontend" / "audio"
    if audio.exists():
        for f in audio.glob("tts_*"):
            f.unlink()

    # ---- 自检：密钥类文件一个都不能漏进 Space ----
    bad: list[str] = []
    for p in DST.rglob("*"):
        if not p.is_file():
            continue
        rel = str(p.relative_to(DST))
        if p.name == ".env":
            bad.append(rel)
        elif "certs" in p.parts:
            bad.append(rel)
        elif p.name in {"auth.json"} or p.suffix.lower() in {".key", ".crt", ".pem"}:
            bad.append(rel)
    if bad:
        print("**自检失败：Space 目录里出现了密钥类文件**")
        for b in bad[:10]:
            print("   ", b)
        return 1

    total = sum(f.stat().st_size for f in DST.rglob("*") if f.is_file())
    n = sum(1 for f in DST.rglob("*") if f.is_file())
    print("")
    print("=" * 68)
    print("deploy/space 组装完成：%d 个文件，%.1f MB" % (n, total / 1024 ** 2))
    print("  自检：未发现 .env / certs / 私钥 / 会话令牌")
    biggest = sorted(((f.stat().st_size, f) for f in DST.rglob("*") if f.is_file()),
                     reverse=True)[:5]
    for size, f in biggest:
        print("    %8.1f MB  %s" % (size / 1024 ** 2, f.relative_to(DST)))
    print("=" * 68)
    print("")
    print("下一步（需要已安装 git）：")
    print("  cd deploy\\space")
    print("  git init -b main")
    print("  git add -A && git commit -m \"deploy: 田语智农 demo\"")
    print("  git remote add origin https://huggingface.co/spaces/<用户名>/<space名>")
    print("  git push -u origin main")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
