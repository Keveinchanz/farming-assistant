#!/usr/bin/env python
"""阶段 7 / 7.7 —— 生成一张**只给局域网调试用**的自签 https 证书。

为什么需要它：
    摄像头取景走浏览器的媒体采集接口，而这个接口只在**安全上下文**下存在 ——
    https，或者 http 的 localhost / 127.0.0.1。手机访问
    `http://192.168.x.x:8000` 时 `navigator.mediaDevices` 就是 undefined，
    诊断页只好回落到「选择图片」。桌面本来就用 127.0.0.1，不受影响。

    https 不是可选的美化，是让手机也能调摄像头的**唯一**办法。真证书要域名，
    局域网内没有域名可用，所以自签。

产物（**只用于本机/局域网调试，不要拿去对外服务**）：
    certs/dev.crt   自签证书（含 SAN：localhost、127.0.0.1、本机全部局域网 IPv4）
    certs/dev.key   私钥
    certs/          整个目录已在 .gitignore 里，**不要提交、不要外发**

    SAN 必须逐条写全：现代浏览器不认 CN，只认 SAN；漏了手机访问的那个 IP，
    浏览器会判定证书与访问地址不匹配，直接拒绝，取景照样用不了。

用法：
    python scripts/33_make_dev_cert.py            # 已存在就提示，不覆盖
    python scripts/33_make_dev_cert.py --force    # 重新生成（换网络后会拿到新 IP）

然后：
    python scripts/20_run_backend.py --https --no-browser
手机浏览器打开脚本打印出来的 https://<局域网IP>:8443/ ，首次会提示
「不是私密连接」—— 选「高级 → 继续前往」，之后诊断页的摄像头就能用了。
（本机/局域网自签证书被接受后，页面即为安全上下文这一条，只能靠实机验证，
  所以诊断页保留了打不开就回落文件选择器的分支，功能不会因此废掉。）

依赖：只用系统上的 openssl 命令行，不新增 Python 依赖。
"""

from __future__ import annotations

import argparse
import ipaddress
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.common.logging_utils import setup_logger               # noqa: E402
from src.common.paths import PROJECT_ROOT                       # noqa: E402

logger = setup_logger("agri.dev_cert")

CERT_DIR = PROJECT_ROOT / "certs"
CERT_FILE = CERT_DIR / "dev.crt"
KEY_FILE = CERT_DIR / "dev.key"

# 825 天是 Apple 平台对服务器证书的硬上限，超了 iOS/macOS 直接不认。
VALID_DAYS = 825


def local_ipv4() -> list[str]:
    """本机所有可能被手机访问到的 IPv4，去重后按「越像主网卡越靠前」排。

    两条路子合起来用，缺一条就可能在某个网络环境下漏掉真正的那个 IP：
      1) 向外「连」一个不可达的地址（UDP，不发包），从本地端点反查主网卡地址；
      2) 解析主机名拿到所有绑定地址（多网卡 / 有线+无线同时在用时会有多个）。
    """
    found: list[str] = []

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))          # 只是让内核挑一张出口网卡，不发任何数据
        found.append(sock.getsockname()[0])
    except OSError:
        pass
    finally:
        sock.close()

    try:
        _, _, addrs = socket.gethostbyname_ex(socket.gethostname())
        found.extend(addrs)
    except OSError:
        pass

    out: list[str] = []
    for ip in found:
        try:
            addr = ipaddress.IPv4Address(ip)
        except ipaddress.AddressValueError:
            continue
        # 169.254.x.x 是「没拿到 DHCP」时的自动地址，放进去只会让 SAN 变长；回环另算
        if addr.is_link_local or addr.is_loopback or str(addr) in out:
            continue
        out.append(str(addr))
    return out


def openssl_exe() -> str | None:
    """优先用 conda 环境自带的，其次 PATH 上的。"""
    conda = Path(sys.prefix) / "Library" / "bin" / "openssl.exe"
    if conda.exists():
        return str(conda)
    return shutil.which("openssl") or shutil.which("openssl.exe")


def build_san(ips: list[str]) -> str:
    parts = ["DNS:localhost", "IP:127.0.0.1"]
    parts.extend(f"IP:{ip}" for ip in ips)
    return ",".join(parts)


def write_config(cfg_path: Path, ips: list[str]) -> None:
    """自己带一份 openssl 配置。

    **不能省**：本机系统上 `OPENSSL_CONF` 指向一个不存在的
    `C:\\Program Files\\Common Files\\ssl\\openssl.cnf`，openssl 一启动就读配置失败；
    不显式给 `-config` 的话，连 `-subj` 都到不了就先报错退出。
    顺带把 SAN 也放进配置里写清楚，比一串逗号拼接的 `-addext` 好读得多。"""
    alt = "\n".join(f"IP.{i + 2} = {ip}" for i, ip in enumerate(ips))
    cfg_path.write_text(
        "[req]\n"
        "distinguished_name = dn\n"
        "prompt = no\n"
        "\n"
        "[dn]\n"
        "C  = CN\n"
        "ST = Hubei\n"
        "L  = Wuhan\n"
        "O  = Agri Dev\n"
        "CN = agri-dev.local\n"
        "\n"
        "[v3]\n"
        "basicConstraints = critical, CA:FALSE\n"
        "keyUsage = critical, digitalSignature, keyEncipherment\n"
        "extendedKeyUsage = serverAuth\n"
        "subjectAltName = @alt\n"
        "\n"
        "[alt]\n"
        "DNS.1 = localhost\n"
        "IP.1  = 127.0.0.1\n"
        + (alt + "\n" if alt else ""),
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="生成局域网调试用自签 https 证书")
    parser.add_argument("--force", action="store_true", help="已存在也重新生成")
    args = parser.parse_args()

    if not args.force and CERT_FILE.exists() and KEY_FILE.exists():
        logger.info("证书已存在：%s", CERT_FILE)
        logger.info("换过网络（IP 变了）或想换一张，加 --force 重新生成。")
        return 0

    exe = openssl_exe()
    if not exe:
        logger.error("找不到 openssl 命令行 —— 没法生成证书。")
        logger.error("  装一个（conda 环境自带：conda install -c conda-forge openssl），")
        logger.error("  或从 Git for Windows 的 usr/bin 里拿一个放到 PATH 上。")
        return 1

    ips = local_ipv4()
    san = build_san(ips)
    CERT_DIR.mkdir(parents=True, exist_ok=True)
    cfg_path = CERT_DIR / "dev.cnf"
    write_config(cfg_path, ips)

    cmd = [
        exe, "req", "-x509", "-newkey", "rsa:2048", "-nodes",
        "-keyout", str(KEY_FILE), "-out", str(CERT_FILE),
        "-days", str(VALID_DAYS),
        "-config", str(cfg_path),
        "-extensions", "v3",
    ]
    logger.info("正在生成自签证书（有效期 %d 天）…", VALID_DAYS)
    logger.info("  SAN：%s", san)

    # 环境里继承下来的 OPENSSL_CONF 可能指向一个不存在的文件（本机就是），
    # 直接指到我们刚写的那份，免得 openssl 先读默认配置再失败。
    env = {**os.environ, "OPENSSL_CONF": str(cfg_path)}
    proc = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env
    )
    if proc.returncode != 0:
        logger.error("openssl 退出码 %s，生成失败。", proc.returncode)
        for line in (proc.stderr or "").splitlines():
            if line.strip():
                logger.error("  %s", line.strip())
        return 1

    if not (CERT_FILE.exists() and KEY_FILE.exists()):
        logger.error("openssl 报成功，但证书/私钥文件没落盘 —— 不继续。")
        return 1

    logger.info("完成：")
    logger.info("  证书  %s", CERT_FILE)
    logger.info("  私钥  %s", KEY_FILE)
    logger.info("")
    logger.info("下一步：")
    logger.info("  python scripts/20_run_backend.py --https --port 8443 --no-browser")
    logger.info("  手机与电脑连同一个 WiFi，用下面的地址打开：")
    for ip in ips or ["<没探测到局域网 IP，用 ipconfig 看一下>"]:
        logger.info("      https://%s:8443/diagnose.html", ip)
    logger.info("  首次会提示证书不受信任 —— 选「高级 → 继续前往」。")
    logger.info("")
    logger.info("  certs/ 已在 .gitignore 里：这张证书只给局域网调试用，不要提交、不要外发。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
