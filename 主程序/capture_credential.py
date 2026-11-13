#!/usr/bin/env python3
"""
从已安装文心快码的机器上抓取登录态凭证。

用途：换机器 / 重装后，从旧机器导出一份凭证，新机器无需安装 IDE 即可跑 2api。

抓取来源（按优先级）：
  1. %APPDATA%\\Comate\\User\\settings.json     -> baidu.comate.license   （主要）
  2. %USERPROFILE%\\.comate\\cli-login.json      -> license                （CLI 登录时才有）
  3. %USERPROFILE%\\.comate\\login               -> 内网 PToken              （内网账号才有）
  4. %USERPROFILE%\\.comate\\login-user\\<user>   -> 内网 PToken

用法：
  python capture_credential.py                 # 打印抓取结果
  python capture_credential.py --json          # 输出 JSON，便于管道处理
  python capture_credential.py --out cred.json # 写入文件

注意：
  * License 是账号级凭证，等同密码，别提交到 Git，别外发。
  * 内网 PToken 只在内网有效，公网环境抓不到也用不了。
"""

import argparse
import json
import os
import sys
from pathlib import Path


def read_json(path):
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def capture():
    """返回抓到的凭证 dict。字段可能缺失，调用方自行判空。"""
    home = Path.home()
    appdata = Path(os.environ.get("APPDATA", "")) if os.environ.get("APPDATA") else None

    found = {}

    # 1) IDE settings.json 里的 license —— 最常见的一处
    if appdata:
        settings_path = appdata / "Comate" / "User" / "settings.json"
        data = read_json(settings_path)
        if isinstance(data, dict):
            lic = data.get("baidu.comate.license")
            user = data.get("baidu.comate.username")
            if lic:
                found["license"] = lic
                found["source"] = str(settings_path)
            if user:
                found["username"] = user

    # 2) CLI 登录落盘的 license
    cli_login = home / ".comate" / "cli-login.json"
    data = read_json(cli_login)
    if isinstance(data, dict):
        if data.get("license") and "license" not in found:
            found["license"] = data["license"]
            found["source"] = str(cli_login)
        if data.get("displayName") and "username" not in found:
            found["username"] = data["displayName"]

    # 3) 内网 PToken：默认文件
    token_file = home / ".comate" / "login"
    ptoken = read_text(token_file)
    if ptoken:
        found["ptoken"] = ptoken
        found.setdefault("ptoken_source", str(token_file))

    # 4) 内网 PToken：按用户分文件，取最近修改的一个
    user_dir = home / ".comate" / "login-user"
    if user_dir.is_dir():
        candidates = [p for p in user_dir.iterdir() if p.is_file()]
        if candidates:
            newest = max(candidates, key=lambda p: p.stat().st_mtime)
            tok = read_text(newest)
            if tok:
                found["ptoken"] = tok
                found["ptoken_source"] = str(newest)
                found.setdefault("username", newest.name)

    return found


def read_text(path):
    try:
        return path.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def main():
    ap = argparse.ArgumentParser(description="抓取文心快码登录态凭证")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出")
    ap.add_argument("--out", help="写入指定文件（权限自动设为仅本人可读）")
    args = ap.parse_args()

    cred = capture()

    if args.out:
        try:
            with open(args.out, "w", encoding="utf-8") as f:
                json.dump(cred, f, ensure_ascii=False, indent=2)
            # Windows 下退化为继承 ACL，仅作尽力而为的限制
            try:
                os.chmod(args.out, 0o600)
            except OSError:
                pass
            print(f"已写入 {args.out}（含敏感信息，注意保管）", file=sys.stderr)
        except OSError as exc:
            print(f"写入失败: {exc}", file=sys.stderr)
            return 1

    if args.json:
        print(json.dumps(cred, ensure_ascii=False, indent=2))
        return 0 if cred else 1

    if not cred:
        print("未抓到任何凭证。", file=sys.stderr)
        print()
        print("可能原因：", file=sys.stderr)
        print("  1. 这台机器没装文心快码，或从未登录过", file=sys.stderr)
        print("  2. 装的是精简版，凭证存在别的位置", file=sys.stderr)
        return 1

    print("抓到以下凭证：\n")
    if cred.get("license"):
        lic = cred["license"]
        masked = lic[:8] + "..." + lic[-4:] if len(lic) > 14 else "****"
        print(f"  License : {masked}")
        print(f"  来源    : {cred.get('source', '未知')}")
    if cred.get("username"):
        print(f"  用户名  : {cred['username']}")
    if cred.get("ptoken"):
        tok = cred["ptoken"]
        masked = tok[:6] + "..." + tok[-4:] if len(tok) > 12 else "****"
        print(f"  PToken  : {masked}（仅内网有效）")
        print(f"  来源    : {cred.get('ptoken_source', '未知')}")
    print()
    print("设置环境变量后即可运行 2api：")
    print(f'  set COMATE_LICENSE={cred.get("license", "你的license")}')
    print("  python comate2api.py --port 8181")
    return 0


if __name__ == "__main__":
    sys.exit(main())