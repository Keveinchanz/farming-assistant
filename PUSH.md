# 推送到 GitHub 与部署到 Hugging Face Spaces

> 目标仓库：<https://github.com/Keveinchanz/farm-language-and-gaining-predict-assistant.git>
> 本机**尚未安装 git**，下面的命令装完 git 后才能执行。

## 0. 先装 Git（二选一）

```powershell
# 方式 A：winget（Windows 10/11 自带）
winget install --id Git.Git -e --source winget --accept-source-agreements --accept-package-agreements

# 方式 B：官网下载安装包
# https://git-scm.com/download/win
```

装完**新开一个终端**（PATH 才会刷新），确认：

```powershell
git --version
```

首次使用还要设一下身份（只需一次）：

```powershell
git config --global user.name  "Keveinchanz"
git config --global user.email "你的GitHub邮箱"
```

## 1. 推代码到 GitHub

在 `D:\code file\insert` 下执行：

```powershell
git init -b main
git add -A
git commit -m "feat: 多方言 AI 农技与农田数字孪生平台"
git remote add origin https://github.com/Keveinchanz/farm-language-and-gaining-predict-assistant.git
git push -u origin main
```

**如果 GitHub 上那个仓库已经有内容**（比如建仓时勾了 README），第一次 push 会被拒绝。先合并再推：

```powershell
git pull --rebase origin main
git push -u origin main
```

### 会推上去多少

`.gitignore` 已经把大的与敏感的挡住了。推之前可以先看一眼：

```powershell
git status --short | Measure-Object -Line
git count-objects -vH
```

预期：**代码 + `runtime_assets/`（175 MB）+ `deploy/hf/`**，没有 `.env`、`certs/`、
`2026AIC-submission/`、`data/pest_cls_raw/`（6 GB）、`Sample/`、`models/checkpoints/`。

> 注意：`runtime_assets/models/onnx/inference_model.onnx` 是 91.9 MB ——
> **低于 GitHub 单文件 100 MB 的硬上限**，不用 Git LFS。如果以后它变大了，得改用 LFS。

## 2. 部署到 Hugging Face Spaces

1. 在 <https://huggingface.co/new-space> 建一个 **Docker** 类型的 Space（免费档即可）。
2. 把组装好的目录推上去（`deploy/space/` 已经是自洽的：代码 + 资源 + Dockerfile + README）：

```powershell
cd "D:\code file\insert\deploy\space"
git init -b main
git add -A
git commit -m "deploy: 田语智农 demo"
git remote add origin https://huggingface.co/spaces/<你的用户名>/<space名>
git push -u origin main
```

3. Space 会自动按 `Dockerfile` 构建，约 10–20 分钟。构建完访问
   `https://<用户名>-<space名>.hf.space`。

### 可选：把密钥配进 Space（不建议给公开演示）

Space 的 **Settings → Variables and secrets** 里可以填 `IFLYTEK_APPID` / `IFLYTEK_API_KEY` /
`IFLYTEK_API_SECRET` / `DEEPSEEK_API_KEY`。**不填也能跑** —— 语音与大模型会走降级路径
（浏览器朗读、规则模板成文），其余功能不受影响。公开演示建议**不填**，避免密钥被滥用。

## 3. 改完代码后重新发布

```powershell
python scripts/50_build_runtime_assets.py   # 资源有变动时才需要
python scripts/51_build_space.py            # 重新组装 deploy/space
```

然后重复第 2 步的 `git add -A && git commit && git push`。
