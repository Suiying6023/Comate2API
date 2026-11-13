#!/usr/bin/env python3
"""
一键启动器。

职责：
  1. 检查 Python 版本
  2. 首次运行自动生成 API Key 并持久化（之后每次启动都是同一个 key）
  3. 读取凭证（环境变量优先，否则从 cred.json）
  4. 启动服务，打印内网 URL、key、支持的协议

设计原则：这个脚本本身不碰任何凭证内容，
凭证由使用者自己导入（见 README 的「导入凭证」一节）。
"""

import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

DATA_DIR = Path(os.environ.get("COMATE_DATA_DIR") or BASE_DIR)
CONFIG_FILE = DATA_DIR / "config.json"
CRED_FILE = BASE_DIR / "cred.json"

# 默认配置。config.json 存在时以文件为准。
DEFAULTS = {
    "host": "0.0.0.0",
    "port": 8181,
    "rps": 2.0,
    "cooldown": 300,
    "api_key": "",
    "device": "",
}


def log(msg=""):
    print(msg, flush=True)


def load_config():
    cfg = dict(DEFAULTS)
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                cfg.update({k: v for k, v in data.items() if k in DEFAULTS})
        except (OSError, json.JSONDecodeError) as exc:
            log(f"[warn] config.json 读取失败，使用默认值: {exc}")
    return cfg


def save_config(cfg):
    try:
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        if os.name != "nt":
            CONFIG_FILE.chmod(0o600)
    except OSError as exc:
        log(f"[warn] config.json 写入失败: {exc}")


def load_credentials():
    """凭证来源优先级：环境变量 > cred.json。

    这里只读不打印内容，避免凭证泄漏到日志或截图里。
    """
    env_raw = os.environ.get("COMATE_LICENSES") or os.environ.get("COMATE_LICENSE") or ""
    licenses = []
    for part in env_raw.replace(";", ",").split(","):
        part = part.strip()
        if part and part not in licenses:
            licenses.append(part)

    source = "环境变量"
    username = os.environ.get("COMATE_USERNAME", "")

    if not licenses and CRED_FILE.exists():
        try:
            data = json.loads(CRED_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log(f"[error] cred.json 解析失败: {exc}")
            return [], "", "cred.json（解析失败）"

        if isinstance(data, dict):
            raw = data.get("licenses")
            if raw is None and data.get("license"):
                raw = [data["license"]]
            if isinstance(raw, str):
                raw = [raw]
            if isinstance(raw, list):
                for item in raw:
                    if isinstance(item, str) and item.strip():
                        v = item.strip()
                        if v not in licenses:
                            licenses.append(v)
            username = username or data.get("username", "")
        elif isinstance(data, list):
            for item in data:
                if isinstance(item, str) and item.strip():
                    v = item.strip()
                    if v not in licenses:
                        licenses.append(v)

        source = "cred.json"

    # 占位符不是真凭证，别让它进凭证池
    PLACEHOLDERS = {
        "在这里填你的 license",
        "在这里填你的license",
        "你的license",
        "your-license",
        "your_license",
        "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
    }
    real = [x for x in licenses if x.lower() not in PLACEHOLDERS]
    if len(real) != len(licenses):
        licenses = real
        if not licenses:
            source = "cred.json（仍是占位内容）"

    # username 也可能是占位符，一并清掉
    PLACEHOLDER_USERS = {
        "可选",
        "可选，仅用于日志标识",
        "your-name",
        "username",
    }
    if username.strip() in PLACEHOLDER_USERS:
        username = ""

    # 最后兜底：自动从本机 IDE 配置里找
    if not licenses:
        discovered = _auto_discover()
        if discovered:
            licenses = discovered["licenses"]
            if not username:
                username = discovered.get("username", "")
            source = "自动发现（来自本机 Comate 配置）"

    return licenses, username, source


def _auto_discover():
    """自动发现本机 License。

    找不到就静默返回 None —— 这是兜底路径，
    不该因为发现失败而让服务起不来。
    """
    try:
        import license_finder

        found = license_finder.find_licenses()
        if not found:
            return None
        # 校验一遍，只保留有效的；全都没效时也用第一个（可能只是网络不通）
        valid = []
        for item in found:
            try:
                ok, _msg = license_finder.validate_license(item["license"])
            except Exception:
                ok = False
            if ok:
                valid.append(item)
        chosen = valid[0] if valid else found[0]
        return {
            "licenses": [chosen["license"]],
            "username": chosen.get("username", ""),
        }
    except Exception:
        return None


def mask(value):
    """脱敏显示，避免完整凭证出现在终端或截图里。"""
    if not value:
        return "(未设置)"
    if len(value) <= 12:
        return value[:2] + "***"
    return f"{value[:6]}...{value[-4:]}"


def detect_lan_ips():
    """探测本机所有内网 IP，按可用性排序。

    不用「连 8.8.8.8 看出口地址」那套：在装了代理 / VPN / 虚拟网卡
    的机器上，那样会拿到 TAP 适配器的地址（例如 198.18.0.1），
    别人根本连不上。

    做法：列出所有 IPv4 地址，过滤掉虚拟网卡段，优先私有网段。
    """
    found = []
    try:
        import subprocess

        if os.name == "nt":
            out = subprocess.run(
                ["ipconfig"], capture_output=True, text=True,
                encoding="mbcs", errors="replace", timeout=8
            ).stdout
            for line in out.splitlines():
                line = line.strip()
                if "IPv4" in line and "地址" in line:
                    # 中文 Windows: "IPv4 地址 . . . . . . . . . . . . : 192.168.1.5"
                    ip = line.split(":")[-1].strip()
                    if _is_usable_ipv4(ip):
                        found.append(ip)
                elif line.startswith("IPv4 Address") and ":" in line:
                    ip = line.split(":")[-1].strip()
                    if _is_usable_ipv4(ip):
                        found.append(ip)
        else:
            out = subprocess.run(
                ["ip", "-4", "addr"], capture_output=True, text=True, timeout=8
            ).stdout
            for line in out.splitlines():
                line = line.strip()
                if line.startswith("inet "):
                    ip = line.split()[1].split("/")[0]
                    if _is_usable_ipv4(ip):
                        found.append(ip)
    except Exception:
        pass

    if not found:
        # 兜底：socket 选路
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("223.5.5.5", 80))
            ip = s.getsockname()[0]
            if _is_usable_ipv4(ip):
                found.append(ip)
        except OSError:
            pass
        finally:
            s.close()

    if not found:
        try:
            ip = socket.gethostbyname(socket.gethostname())
            if _is_usable_ipv4(ip):
                found.append(ip)
        except OSError:
            pass

    # 去重且保序
    seen = []
    for ip in found:
        if ip not in seen:
            seen.append(ip)

    # 排序：192.168.* 和 10.* 优先（家庭/公司网最可能互通），
    # 172.16-31.* 次之
    def score(ip):
        o = ip.split(".")
        if o[0] == "192" and o[1] == "168":
            return 0
        if o[0] == "10":
            return 1
        if o[0] == "172" and 16 <= int(o[1]) <= 31:
            return 2
        return 3

    seen.sort(key=score)
    return seen


def _is_usable_ipv4(ip):
    """过滤掉回环、链路本地、以及虚拟网卡常见段。"""
    parts = ip.split(".")
    if len(parts) != 4:
        return False
    try:
        o = [int(p) for p in parts]
    except ValueError:
        return False

    if o[0] == 127:  # loopback
        return False
    if o[0] == 169 and o[1] == 254:  # link-local
        return False
    if o[0] == 0:
        return False
    # Clash / Tailscale / 各类 TAP 常用段，排除掉
    if o[0] == 198 and o[1] in (18, 19):
        return False
    if o[0] == 100 and 64 <= o[1] <= 127:  # CGNAT，常见于 Tailscale
        return False
    return True


def check_port(host, port):
    """返回占用端口的进程信息，未占用返回 None。"""
    if os.name == "nt":
        cmd = f'netstat -ano | findstr LISTENING | findstr :{port}'
        try:
            import subprocess

            out = subprocess.run(
                cmd, shell=True, capture_output=True, text=True,
                encoding="mbcs", errors="replace", timeout=5
            ).stdout
            for line in out.splitlines():
                if f":{port}" in line:
                    parts = line.split()
                    if parts:
                        pid = parts[-1]
                        try:
                            p = subprocess.run(
                                f"tasklist /FI \"PID eq {pid}\" /FO CSV /NH",
                                shell=True,
                                capture_output=True,
                                text=True,
                                encoding="mbcs",
                                errors="replace",
                                timeout=5,
                            ).stdout.strip()
                            name = p.split('","')[0].strip('"') if p else pid
                            return f"PID {pid} ({name})"
                        except Exception:
                            return f"PID {pid}"
        except Exception:
            pass
    return None


def _pid_alive(pid):
    """判断进程是否存活。跨平台。"""
    if os.name == "nt":
        import subprocess

        try:
            r = subprocess.run(
                f'tasklist /FI "PID eq {pid}" /FO CSV /NH',
                shell=True, capture_output=True, text=True,
                encoding="mbcs", errors="replace", timeout=5,
            )
            return str(pid) in r.stdout
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _write_pidfile(path, logfile):
    """写 PID 文件，退出时自动清理。"""
    try:
        path.write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        return

    def cleanup():
        try:
            if path.exists() and path.read_text(encoding="utf-8").strip() == str(
                os.getpid()
            ):
                path.unlink()
        except OSError:
            pass

    import atexit

    atexit.register(cleanup)

    # 捕获常见信号，保证 Ctrl+C / 服务停止时也能清理
    try:
        import signal

        def handler(signum, frame):
            cleanup()
            raise KeyboardInterrupt

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError, AttributeError):
                pass
    except Exception:
        pass


def open_browser(url):
    """尽力打开浏览器，失败不影响服务。"""
    try:
        import subprocess

        if os.name == "nt":
            subprocess.Popen(
                ["cmd", "/c", "start", "", url], shell=False,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        elif sys.platform == "darwin":
            subprocess.Popen(["open", url])
        else:
            subprocess.Popen(["xdg-open", url])
        return True
    except Exception:
        return False


def main():
    log()
    log("=" * 58)
    log("  comate2api 一键启动")
    log("=" * 58)
    log()

    # ---- Python 版本 ----
    if sys.version_info < (3, 8):
        log("[error] 需要 Python 3.8 或更高版本")
        log(f"        当前版本 {sys.version.split()[0]}")
        log("        下载: https://www.python.org/downloads/")
        input("\n按回车退出...")
        return 1

    # ---- 依赖检查（本项目零依赖，但主服务需要标准库）----
    try:
        import riskguard  # noqa: F401
    except ImportError as exc:
        log(f"[error] 缺少 riskguard.py: {exc}")
        log("        请确认整个目录完整复制过来，不要只拷 comate2api.py")
        input("\n按回车退出...")
        return 1

    # ---- 单实例检查：避免重复启动占端口 ----
    pidfile = BASE_DIR / "server.pid"
    logfile = BASE_DIR / "server.log"
    if pidfile.exists() and "--status" not in sys.argv:
        try:
            old_pid = int(pidfile.read_text(encoding="utf-8").strip())
        except (ValueError, OSError):
            old_pid = None
        if old_pid and old_pid != os.getpid() and _pid_alive(old_pid):
            log(f"[warn] 服务已在运行（PID {old_pid}）")
            log()
            log("  如需重启：")
            log("    Windows: 在项目根目录运行 启动服务.bat restart")
            log("    Linux  : ./comate2api.sh restart")
            log()
            log("  查看状态： ./comate2api.sh status  或  项目根目录的 启动服务.bat status")
            log()
            return 0
        # PID 文件是残留的，清掉
        try:
            pidfile.unlink()
        except OSError:
            pass

    cfg = load_config()

    # ---- API Key：首次生成并持久化 ----
    api_key = os.environ.get("COMATE_API_TOKEN", "").strip() or cfg.get("api_key", "")
    if not api_key:
        import secrets

        api_key = "sk-comate-" + secrets.token_urlsafe(32)
        cfg["api_key"] = api_key
        save_config(cfg)
        key_note = "新生成（已保存到 config.json）"
    else:
        key_note = "来自 config.json / 环境变量"

    # 每份安装生成独立指纹，重启沿用；环境变量仍可覆盖。
    if not cfg.get("device"):
        import uuid

        cfg["device"] = uuid.uuid4().hex
        save_config(cfg)
    os.environ["COMATE_DEVICE"] = os.environ.get("COMATE_DEVICE", "").strip() or cfg["device"]

    # ---- 凭证 ----
    licenses, username, cred_source = load_credentials()

    host = os.environ.get("COMATE_HOST", "").strip() or cfg["host"]
    port = int(os.environ.get("COMATE_PORT", "") or cfg["port"])
    rps = float(os.environ.get("COMATE_RPS", "") or cfg["rps"])
    cooldown = int(os.environ.get("COMATE_COOLDOWN", "") or cfg["cooldown"])

    # ---- 端口检查 ----
    occupied = check_port(host, port)
    if occupied:
        log(f"[error] 端口 {port} 已被占用: {occupied}")
        log()
        log("  解决办法：")
        log(f"    1. 换个端口：编辑 config.json，把 \"port\": {port} 改成别的")
        log("    2. 或关掉占用它的程序")
        log()
        input("按回车退出...")
        return 1

    # ---- 打印配置摘要（不含凭证明文）----
    log("【配置】")
    log(f"  监听地址  : {host}:{port}")
    log(f"  限流      : {rps} req/s")
    log(f"  冷却      : {cooldown}s（指数退避）")
    log()

    if licenses:
        log("【凭证】")
        log(f"  来源      : {cred_source}")
        log(f"  数量      : {len(licenses)} 个")
        for i, lic in enumerate(licenses, 1):
            log(f"    凭证{i}    : {mask(lic)}")
        if username:
            log(f"  用户标识  : {username}")
        log()
    else:
        log("【凭证】未配置")
        log()
        log("  ! 缺少凭证，服务会启动但无法调用模型。")
        log()
        log("  已尝试自动发现本机的 Comate 配置，没找到。")
        log()
        log("  三种导入方式：")
        log()
        log("    A. 先跑自动发现（如果这台机器装过 Comate）")
        log("       python license_finder.py")
        log()
        log("    B. 编辑同目录下 cred.json：")
        log('       {"license": "你的license", "username": "可选"}')
        log()
        log("    C. 设环境变量后再启动：")
        log("       set COMATE_LICENSE=你的license")
        log()
        log("  license 是什么、从哪来，见 LICENSE来源.md")
        log()

    # ---- 启动服务 ----
    try:
        import comate2api
    except Exception as exc:
        log(f"[error] 加载主服务失败: {exc}")
        input("\n按回车退出...")
        return 1

    creds = [{"license": p, "username": username} for p in licenses]

    lan_ips = detect_lan_ips()

    # 计算实际可访问的 URL 列表
    urls = []
    if host in ("0.0.0.0", ""):
        for ip in lan_ips:
            urls.append(f"http://{ip}:{port}")
        if not lan_ips:
            urls.append(f"http://127.0.0.1:{port}")
        urls.append(f"http://127.0.0.1:{port}")
    elif host in ("::", "::0"):
        for ip in lan_ips:
            urls.append(f"http://[{ip}]:{port}")
    else:
        urls.append(f"http://{host}:{port}")

    log("=" * 58)
    log("  服务地址")
    log("=" * 58)
    for u in urls:
        tag = "本机" if "127.0.0.1" in u or "localhost" in u else "内网"
        log(f"  [{tag}] {u}")
    log()
    log("=" * 58)
    log("  API Key")
    log("=" * 58)
    log(f"  {api_key}")
    log(f"  （{key_note}）")
    log()
    log("=" * 58)
    log("  可用接口")
    log("=" * 58)
    log(f"  GET  {urls[0]}/v1/models          模型列表")
    log(f"  POST {urls[0]}/v1/chat/completions 对话（OpenAI 兼容）")
    log(f"  GET  {urls[0]}/admin/pool          风控状态")
    log(f"  GET  {urls[0]}/health              健康检查")
    log()

    # 协议兼容性说明
    log("=" * 58)
    log("  支持的协议")
    log("=" * 58)
    log("  OpenAI Chat Completions  /v1/chat/completions")
    log("    · 流式 stream:true / 非流式 stream:false")
    log("    · 文本对话 / reasoning_content / 真实 token 用量")
    log("    · reasoning_content 思维链（非标准扩展，客户端会忽略）")
    log("  OpenAI Models            /v1/models")
    log("  认证方式: Authorization: Bearer <key>  或  x-api-key: <key>")
    log()

    # 把关键信息写到一个文件，方便复制
    info = {
        "urls": urls,
        "api_key": api_key,
        "models_endpoint": "/v1/models",
        "chat_endpoint": "/v1/chat/completions",
        "auth": "Authorization: Bearer <key>",
    }
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        connection_file = DATA_DIR / "connection.json"
        connection_file.write_text(
            json.dumps(info, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        if os.name != "nt":
            connection_file.chmod(0o600)
    except OSError:
        pass

    # 浏览器提示
    log("=" * 58)
    log(f"  模型列表: {urls[0].replace('http://', 'http://')}/v1/models")
    log("=" * 58)
    log()
    log("  按 Ctrl+C 停止服务")
    log()

    if not licenses:
        log("  提示：没有凭证时服务仍能启动，但调用会失败。")
        log("        可以先 Ctrl+C 停掉，配好凭证再启动。")
        log()

    # 记录 PID，供停止脚本使用
    _write_pidfile(BASE_DIR / "server.pid", BASE_DIR / "server.log")

    # 启动（阻塞）
    try:
        comate2api.serve(
            host,
            port,
            creds,
            [api_key],
            rps,
            cooldown,
        )
    except KeyboardInterrupt:
        log()
        log("已停止服务")
    except OSError as exc:
        log()
        log(f"[error] 启动失败: {exc}")
        log()
        log("  常见原因：")
        log("    1. 端口被占用 —— 换个端口（改 config.json）")
        log("    2. 权限不足 —— 以管理员身份运行")
        log()
        input("按回车退出...")
        return 1

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        import traceback

        traceback.print_exc()
        try:
            input("\n出现异常，按回车退出...")
        except EOFError:
            pass
        sys.exit(1)
