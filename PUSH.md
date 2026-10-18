# 推送到 GitHub 与部署到 Hugging Face Spaces

> 目标仓库：<https://github.com/Keveinchanz/farm-language-and-gaining-predict-assistant.git>

## 当前状态（2026-10-18）

已经做完的：

- ✅ Git 2.55 已装好
- ✅ 本地仓库已初始化，远程 `origin` 已指向上面的地址
- ✅ **提交已完成**：`62198b7`，1,882 个文件，压缩后 129.9 MiB
- ✅ 代理已配置：`git config --global http.proxy http://127.0.0.1:7897`
      （**必须走代理** —— 直连 github.com 会被重置）
- ✅ 远程只有一个 GitHub 自动生成的 `Initial commit`，我们的提交已经接在它上面

**只剩推送这一步**，它需要你的 GitHub 账号授权，必须在**你自己的终端**里执行 ——
我这个执行环境没有交互式终端，登录窗口弹不出来。

## 1. 推送（在你自己的 PowerShell 里跑）

```powershell
cd "D:\code file\insert"
git push -u origin main
```

会弹出 GitHub 登录窗口（Git Credential Manager），授权后自动完成推送。
成功后访问 <https://github.com/Keveinchanz/farm-language-and-gaining-predict-assistant>
应该能看到完整的项目。

## 2. 部署到 Hugging Face Spaces

1. 在 <https://huggingface.co/new-space> 建一个 **Docker** 类型的 Space（免费档）。
2. 推送组装好的目录（`deploy/space/` 已自洽：代码 + 资源 + Dockerfile + README）：

```powershell
cd "D:\code file\insert\deploy\space"
git init -b main
git add -A
git commit -m "deploy: 田语智农 demo"
git remote add origin https://huggingface.co/spaces/<你的用户名>/<space名>
git push -u origin main
```

> 如果 huggingface.co 也连不上，先给 git 配代理：
> `git config --global http.proxy http://127.0.0.1:7897`（已配过就跳过）

3. Space 会自动按 `Dockerfile` 构建，约 10–20 分钟。构建完访问
   `https://<用户名>-<space名>.hf.space`。

### 可选：把密钥配进 Space（不建议给公开演示）

Space 的 **Settings → Variables and secrets** 里可以填 `IFLYTEK_APPID` / `IFLYTEK_API_KEY` /
`IFLYTEK_API_SECRET` / `DEEPSEEK_API_KEY`。**不填也能跑** —— 语音与大模型会走降级路径
（浏览器朗读、规则模板成文），其余功能不受影响。公开演示建议**不填**。

## 3. 改完代码后重新发布

```powershell
python scripts/50_build_runtime_assets.py   # 资源有变动时才需要
python scripts/51_build_space.py            # 重新组装 deploy/space
```

然后重复第 2 步的 `git add -A && git commit && git push`。

## 附：这次的推送内容核对

- **会推**：`src/ backend/ frontend/ configs/ scripts/ tests/ docs/ docker/`、
  `runtime_assets/`（175 MB）、`deploy/hf/`、`requirements*.txt`、`.gitignore`、`README.md`
- **不会推**：`.env`、`certs/`、`outputs/`、`2026AIC-submission/`、`data/pest_cls_raw/`（6 GB）、
  `data/pest/`（781 MB）、`Sample/`、`models/checkpoints/`、`*.pt`
- 单文件最大 91.9 MB（`inference_model.onnx`），低于 GitHub 的 100 MB 硬上限，**不需要 LFS**

