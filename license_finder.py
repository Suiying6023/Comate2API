#!/usr/bin/env python3
"""
凭证自动发现。

背景：很多人装过文心快码但不知道 License 在哪。
这个模块负责「把本机的 license 找出来」，而不是让用户去翻文件。

它能查的地方，按可靠性排序：

  1. Comate IDE 的 settings.json        —— 最可靠，明文
  2. Comate CLI 的 cli-login.json       —— CLI 登录时才有
  3. 从 IDE 进程的环境变量里挖           —— 兜底的兜底

同时提供 license 合法性校验：不是所有 UUID 都能用，
所以拿到就验一次，避免用户填了错的值还以为是网络问题。

重要：这个模块只读不写，不会修改任何 IDE 配置。
"""

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

HOME = Path.home()
APPDATA = Path(os.environ.get("APPDATA", "")) if os.environ.get("APPDATA") else None


# ==================== License 发现 ====================


def find_licenses():
    """扫描本机所有可能的 License 位置。

    返回 [{license, username, source, detail}]，去重。
    """
    found = []
    seen = set()

    def add(license_key, username, source, detail=""):
        license_key = (license_key or "").strip()
        if not license_key:
            return
        # License 是 UUID 形态，粗过滤一下明显的占位符
        if not re.match(r"^[0-9a-fA-F-]{8,64}$", license_key):
            return
        low = license_key.lower()
        if low in seen:
            return
        seen.add(low)
        found.append(
            {
                "license": license_key,
                "username": (username or "").strip(),
                "source": source,
                "detail": detail,
            }
        )

    # ---- 1. IDE settings.json（最可靠）----
    candidates = []
    if APPDATA:
        candidates.append(APPDATA / "Comate" / "User" / "settings.json")
        # 多 profile 的情况
        try:
            for p in (APPDATA / "Comate" / "User" / "profiles").glob("*/settings.json"):
                candidates.append(p)
        except OSError:
            pass

    for path in candidates:
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        lic = data.get("baidu.comate.license")
        user = data.get("baidu.comate.username")
        if lic:
            add(
                lic,
                user,
                "Comate IDE 设置",
                f"来自 {path}",
            )

    # ---- 2. CLI 登录凭证 ----
    cli_login = HOME / ".comate" / "cli-login.json"
    if cli_login.exists():
        try:
            data = json.loads(cli_login.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                add(
                    data.get("license"),
                    data.get("displayName") or data.get("username"),
                    "Comate CLI 登录",
                    f"来自 {cli_login}",
                )
        except (OSError, json.JSONDecodeError):
            pass

    # ---- 3. 从 IDE 进程环境变量挖（兜底）----
    add_from_processes(add)

    return found


def add_from_processes(add):
    """从运行中的 Comate 进程里挖 license。

    这是最后手段：只有 IDE 正在运行时才有效。
    用 WMI 拿命令行，有些版本会把凭证放在参数里。
    """
    if os.name != "nt":
        return

    try:
        # 显式指定编码，避免在非 UTF-8 区域设置下 GBK 解码崩溃
        r = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
                "Get-CimInstance Win32_Process "
                "| Where-Object { $_.CommandLine -like '*comate*' "
                " -or $_.CommandLine -like '*zulu*' } "
                "| Select-Object -ExpandProperty CommandLine",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=12,
        )
        for line in (r.stdout or "").splitlines():
            # 匹配 --license <uuid> 或 license=<uuid>
            for m in re.finditer(
                r"(?:--license[= ]+|license[\"']?\s*[:=]\s*[\"']?)"
                r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})",
                line,
            ):
                add(m.group(1), "", "运行中的 IDE 进程", "从命令行参数提取")
    except Exception:
        pass


# ==================== License 校验 ====================


def validate_license(license_key, timeout=12):
    """向服务端校验 License 是否有效。

    返回 (ok, message)。
    ok=True 表示服务端承认这个 License。

    用官方校验端点：GET /api/key/valid/<license>
    """
    url = f"https://comate.baidu.com/api/key/valid/{license_key}"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "axios/1.7.9",
            "Accept": "application/json, text/plain, */*",
            "X-Source": "COMATE",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode("utf-8", "replace"))
            msg = body.get("message") or f"HTTP {exc.code}"
        except Exception:
            msg = f"HTTP {exc.code}"
        return False, msg
    except (urllib.error.URLError, TimeoutError) as exc:
        return False, f"网络不可达: {exc}"
    except json.JSONDecodeError:
        return False, "响应不是合法 JSON"

    if data.get("status") == "OK" and data.get("data") is True:
        return True, "有效"
    return False, data.get("message") or "服务端未确认有效"


def describe_license(license_key):
    """返回 License 的概要信息（脱敏），用于给用户确认。

    这里不去查 username，因为 License 本身不绑定可读用户名；
    username 在 IDE 设置里是单独存的。
    """
    return {
        "masked": mask(license_key),
        "length": len(license_key),
        "format_ok": bool(
            re.match(
                r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$",
                license_key,
            )
        ),
    }


def mask(value):
    if not value:
        return ""
    if len(value) <= 12:
        return value[:3] + "***"
    return f"{value[:8]}...{value[-4:]}"


# ==================== 写入凭证文件 ====================


def save_to_cred_json(licenses, username="", path=None):
    """把找到的 License 写入 cred.json。

    已有内容会保留（不覆盖用户手填的），只补没有的。
    """
    target = Path(path) if path else (Path(__file__).resolve().parent / "cred.json")

    existing_licenses = []
    existing_user = ""
    if target.exists():
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                raw = data.get("licenses")
                if raw is None and data.get("license"):
                    raw = [data["license"]]
                if isinstance(raw, str):
                    raw = [raw]
                if isinstance(raw, list):
                    existing_licenses = [x for x in raw if isinstance(x, str)]
                existing_user = data.get("username", "") or ""
            elif isinstance(data, list):
                existing_licenses = [x for x in data if isinstance(x, str)]
        except (OSError, json.JSONDecodeError):
            pass

    # 占位符要清掉，否则会被当成真凭证
    placeholders = {
        "在这里填你的 license",
        "在这里填你的license",
        "你的license",
        "your-license",
        "your_license",
        "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
    }
    merged = [
        x
        for x in existing_licenses
        if x.strip() and x.strip().lower() not in placeholders
    ]
    for item in licenses:
        key = item["license"] if isinstance(item, dict) else item
        key = (key or "").strip()
        if key and key not in merged:
            merged.append(key)

    payload = {}
    if len(merged) == 1:
        payload["license"] = merged[0]
    else:
        payload["licenses"] = merged
    user = username or existing_user
    if user:
        payload["username"] = user

    try:
        target.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return True, str(target)
    except OSError as exc:
        return False, str(exc)


# ==================== CLI ====================


def main():
    import argparse

    ap = argparse.ArgumentParser(description="发现并校验本机的文心快码 License")
    ap.add_argument("--save", action="store_true", help="把找到的写入 cred.json")
    ap.add_argument("--json", action="store_true", help="JSON 输出")
    ap.add_argument("--no-validate", action="store_true", help="跳过在线校验")
    args = ap.parse_args()

    print()
    print("=" * 58)
    print("  查找本机的文心快码 License")
    print("=" * 58)
    print()

    found = find_licenses()

    if not found:
        print("  没找到任何 License。")
        print()
        print("  可能原因：")
        print("    1. 这台机器没装文心快码")
        print("    2. 装了但从未登录过")
        print("    3. 装的是精简版，凭证存在别处")
        print()
        print("  手动获取方式见 README.md「License 从哪来」一节。")
        print()
        if args.json:
            print(json.dumps({"found": [], "error": "no license found"}, ensure_ascii=False))
        return 1

    if args.json:
        out = []
        for item in found:
            entry = dict(item)
            entry["license"] = mask(item["license"])
            if not args.no_validate:
                ok, msg = validate_license(item["license"])
                entry["valid"] = ok
                entry["validation"] = msg
            out.append(entry)
        print(json.dumps({"found": out, "count": len(out)}, ensure_ascii=False, indent=2))
    else:
        for i, item in enumerate(found, 1):
            print(f"  [{i}] {item['source']}")
            print(f"      License : {mask(item['license'])}")
            if item.get("username"):
                print(f"      用户    : {item['username']}")
            if item.get("detail"):
                print(f"      位置    : {item['detail']}")
            if not args.no_validate:
                ok, msg = validate_license(item["license"])
                mark = "有效" if ok else "无效"
                print(f"      校验    : {mark}" + ("" if ok else f"（{msg}）"))
            print()

    if args.save:
        licenses = [x["license"] for x in found]
        username = next((x["username"] for x in found if x.get("username")), "")
        ok, detail = save_to_cred_json(found, username)
        print()
        if ok:
            print(f"  已写入 {detail}")
        else:
            print(f"  写入失败: {detail}")

    print()
    print("=" * 58)
    if not args.no_validate:
        valid_count = sum(1 for x in found if validate_license(x["license"])[0])
        print(f"  共找到 {len(found)} 个，其中 {valid_count} 个校验有效")
    else:
        print(f"  共找到 {len(found)} 个（未校验）")
    print("=" * 58)
    print()

    return 0


if __name__ == "__main__":
    sys.exit(main())