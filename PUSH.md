# 推送到 GitHub 与部署到 Hugging Face Spaces

> 目标仓库：<https://github.com/Keveinchanz/farm-language-and-gaining-predict-assistant.git>

## 当前状态

两个本地仓库都已提交完毕，只差推送（推送需要你的账号授权，我这边没有交互式终端）。

| 位置 | 提交 | 内容 |
|---|---|---|
| `D:\code file\insert` | `4079732` | 代码 + `runtime_assets/` + `deploy/hf/` + README，共 1,882 个文件 |
| `D:\code file\insert\deploy\space` | `5a6f343` | 自洽的 Space 内容（代码 + 运行资源 + Dockerfile），1,872 个文件 |

已配置：

- `git config --global http.proxy http://127.0.0.1:7897`（**必须走代理**，直连 github.com 会被重置）
- `git config --global credential.helper manager`（推送时弹出 GitHub 登录窗口）
- `.env`、`certs/`、`auth.json`、`2026AIC-submission/`、训练数据均已排除

## 1. 推 GitHub

```powershell
cd "D:\code file\insert"
git push -u origin main
```

## 2. 建 Hugging Face Space

在 <https://huggingface.co/new-space> 新建 **Docker** 类型的 Space（免费档）。
创建后记下地址：`https://huggingface.co/spaces/<用户名>/<space名>`。

## 3. 推 Space

```powershell
cd "D:\code file\insert\deploy\space"
git remote add origin https://huggingface.co/spaces/<用户名>/<space名>
git push -u origin main
```

如果 huggingface.co 连不上，先给这个仓库单独配代理：

```powershell
git config --local http.proxy http://127.0.0.1:7897
```

推送约 180 MB，视上行带宽需要 5–15 分钟。推送完成后 Space 自动构建，约 10–20 分钟。

## 4. 把演示地址发给我

构建完成后访问 `https://<用户名>-<space名>.hf.space`。把这个地址告诉我，我会：

1. 在 `README.md` 和 `deploy/hf/README.md` 里加入在线演示链接；
2. 帮你验证 `/api/health`、图书库、地图、诊断接口是否正常；
3. 提交这次改动，你再 `git push` 一次即可。

## 5. 改完代码后重新发布

```powershell
python scripts/50_build_runtime_assets.py   # 资源有变动时才需要
python scripts/51_build_space.py            # 重新组装 deploy/space
cd deploy\space
git add -A && git commit -m "update" && git push
```

`51_build_space.py` 组装时会自检密钥类文件，命中就中止。

## 附：推送内容核对

- **会推**：`src/ backend/ frontend/ configs/ scripts/ tests/ docs/ docker/`、
  `runtime_assets/`（175 MB）、`deploy/hf/`、`requirements*.txt`、`.gitignore`、`README.md`
- **不会推**：`.env`、`certs/`、`outputs/`、`2026AIC-submission/`、`data/pest_cls_raw/`（6 GB）、
  `data/pest/`（781 MB）、`Sample/`、`models/checkpoints/`、`*.pt`
- 单文件最大 91.9 MB（`inference_model.onnx`），低于 GitHub 的 100 MB 上限，不需要 LFS
