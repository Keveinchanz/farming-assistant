# 推送到 GitHub，并用 Codespaces 运行演示

## 在线演示地址

```
https://comfortable-tuesday-remove-islamic.trycloudflare.com
```

由 Codespace 内的 `cloudflared` 临时隧道提供，**仅在隧道进程运行期间有效**。
Codespace 停止、终端关闭或隧道进程退出后地址失效，重新运行一次即可得到新地址：

```bash
cd /workspaces/farm-language-and-gaining-predict-assistant
/tmp/cloudflared tunnel --url http://localhost:8000
```

> 之所以用隧道：Codespaces 的原生转发地址 `…-8000.app.github.dev` 目前返回 404，
> 端口可见性仍为 Private。隧道路径不经过该代理，已验证可正常访问。

## 当前状态

| 项 | 状态 |
|---|---|
| Git | 已安装，代理已配（`http://127.0.0.1:7897`） |
| 主仓库 | 已提交，远程指向 `Keveinchanz/farm-language-and-gaining-predict-assistant` |
| Codespaces 配置 | `.devcontainer/devcontainer.json` 与两个启动脚本已就绪 |
| `deploy/space/` | 保留。开通 Hugging Face PRO 后可直接推上去 |

推送需要你的 GitHub 账号授权 —— 我这边的执行环境没有交互式终端，登录窗口弹不出来。

## 1. 推送到 GitHub

```powershell
cd "D:\code file\insert"
git push -u origin main
```

## 2. 创建 Codespace

打开仓库页面 → **Code** → **Codespaces** → **Create codespace on main**。

首次创建会自动执行 `.devcontainer/devcontainer.json` 里的 `postCreateCommand`：
把 `runtime_assets/` 铺到应用期望的位置，安装 CPU 版 torch 与其余依赖。约 **8–15 分钟**，
之后每次启动只需十几秒。

## 3. 获取公开地址

启动完成后，编辑器底部的 **Ports（端口）** 面板会出现 `8000`。
`devcontainer.json` 已把它设为 `visibility: public`；若组织策略未生效，右键该端口 →
**Port Visibility** → **Public**。

地址形如：

```
https://<codespace 名>-8000.app.github.dev
```

## 4. 日常使用

- Codespace **闲置约 30 分钟会自动停止**，期间不计时（免费额度为每月 120 核·小时，2 核机器即 60 小时）。
- 重新打开后 `postStartCommand` 会自动拉起服务，**地址不变**，等待十几秒即可访问。
- 不使用时在 GitHub 页面点 **Stop codespace**，停止状态不消耗额度。

## 5. 故障处理

服务没起来时，在 Codespace 的终端里查看：

```bash
tail -n 100 outputs/logs/codespace_server.log
```

依赖没装好时重新执行：

```bash
bash scripts/52_codespace_setup.sh
```

## 附：为什么不用 Hugging Face Spaces

HF 官方的 `new-space/agents.md` 指向的技能文档写明：**Docker 与 Gradio Space 需要付费计划
（个人账号为 PRO）**，免费账号只能建 Static Space 或最多 2 个 ZeroGPU（仅 Gradio）Space。
本项目是 FastAPI 多页应用，两条免费通道都不适用，因此改用 Codespaces。

`deploy/space/` 与 `deploy/hf/Dockerfile` 均为 Docker 部署准备好，开通 PRO 后可直接推送：

```powershell
cd "D:\code file\insert\deploy\space"
git init -b main
git add -A
git commit -m "deploy: 田语智农 demo"
git remote add origin https://huggingface.co/spaces/<用户名>/<space名>
git push -u origin main
```
