#!/usr/bin/env python3
"""
一键生成部署文件。

生成后直接交给朋友，或者拷到 Linux 服务器上用：
  python deploy.py --docker         生成 Dockerfile 和 docker-compose.yml
  python deploy.py --systemd        生成 systemd service
  python deploy.py --windows        生成 Windows 开机自启脚本

不会生成任何含凭证的内容 —— 凭证由使用者自己填。
"""

import argparse
import json
import os
import stat
from pathlib import Path

BASE = Path(__file__).resolve().parent


def gen_docker():
    dockerfile = """\
FROM python:3.11-slim

WORKDIR /app

# 只拷源码，不拷凭证
COPY riskguard.py comate2api.py start.py license_finder.py ./

ENV PYTHONUNBUFFERED=1 \\
    PYTHONDONTWRITEBYTECODE=1 \\
    COMATE_HOST=0.0.0.0 \\
    COMATE_PORT=8181 \\
    COMATE_DATA_DIR=/app/data

EXPOSE 8181

# 健康检查用官方 /health
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \\
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8181/health',timeout=3).status==200 else 1)"

CMD ["python", "start.py"]
"""
    (BASE / "Dockerfile").write_text(dockerfile, encoding="utf-8")

    compose = """\
services:
  comate2api:
    build: .
    container_name: comate2api
    restart: unless-stopped
    ports:
      - "8181:8181"
    environment:
      # 凭证用 .env 或下面直接写，别提交到 git
      COMATE_LICENSE: ${COMATE_LICENSE:-}
      COMATE_LICENSES: ${COMATE_LICENSES:-}
      COMATE_API_TOKEN: ${COMATE_API_TOKEN:-}
      COMATE_RPS: ${COMATE_RPS:-2}
      COMATE_COOLDOWN: ${COMATE_COOLDOWN:-300}
      COMATE_HOST: 0.0.0.0
      COMATE_PORT: 8181
      COMATE_DATA_DIR: /app/data
    volumes:
      # 持久化 config.json，key 不会因为重建容器而变
      - ./data:/app/data
    healthcheck:
      test:
        - CMD
        - python
        - -c
        - "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8181/health',timeout=3).status==200 else 1)"
      interval: 30s
      timeout: 5s
      retries: 3
"""
    (BASE / "docker-compose.yml").write_text(compose, encoding="utf-8")

    env_example = """\
# 复制为 .env 后填入真实值，.env 不要提交到 git
COMATE_LICENSE=
# 多凭证用逗号分隔（可选）
COMATE_LICENSES=
# 客户端访问本服务的 token，留空则自动生成
COMATE_API_TOKEN=
# 限流：每秒请求数
COMATE_RPS=2
# 凭证冷却基础秒数
COMATE_COOLDOWN=300
"""
    (BASE / ".env.example").write_text(env_example, encoding="utf-8")

    gitignore = BASE / ".gitignore"
    entries = [".env", "service.env", "cred.json", "config.json", "connection.json",
               "server.pid", "server.log", "data/", "__pycache__/", ".venv/"]
    existing = ""
    if gitignore.exists():
        existing = gitignore.read_text(encoding="utf-8")
    add = [e for e in entries if e not in existing]
    if add:
        with gitignore.open("a", encoding="utf-8") as f:
            if existing and not existing.endswith("\n"):
                f.write("\n")
            f.write("\n".join(add) + "\n")

    print("已生成 Docker 部署文件：")
    print("  Dockerfile")
    print("  docker-compose.yml")
    print("  .env.example")
    print("  .gitignore（已加入凭证保护）")
    print()
    print("用法：")
    print("  cp .env.example .env")
    print("  # 编辑 .env 填入 license")
    print("  docker compose up -d")


def gen_systemd():
    unit = """\
[Unit]
Description=comate2api - Baidu Comate OpenAI-compatible API
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
# 按实际安装路径改这两行
WorkingDirectory=__DIR__
ExecStart=__PYTHON__ __DIR__/start.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

# 凭证从独立文件读，不写死在 unit 里
EnvironmentFile=-__DIR__/service.env

# 安全加固：服务不需要写系统任何东西
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=read-only
ReadWritePaths=__DIR__

[Install]
WantedBy=multi-user.target
"""
    import sys

    install_dir = "/opt/comate2api" if os.name == "nt" else str(BASE)
    python_bin = "/usr/bin/python3" if os.name == "nt" else sys.executable
    content = unit.replace("__DIR__", install_dir).replace("__PYTHON__", python_bin)
    out = BASE / "comate2api.service"
    out.write_text(content, encoding="utf-8")

    env_file = BASE / "service.env.example"
    env_file.write_text(
        "# 复制为 service.env 后填写，权限设为 600\n"
        "COMATE_LICENSE=\n"
        "COMATE_API_TOKEN=\n"
        "COMATE_RPS=2\n"
        "COMATE_HOST=0.0.0.0\n"
        "COMATE_PORT=8181\n",
        encoding="utf-8",
    )

    print("已生成 systemd 部署文件：")
    print("  comate2api.service")
    print("  service.env.example")
    print()
    print("用法：")
    print("  cp service.env.example service.env")
    print("  chmod 600 service.env")
    print("  # 编辑 service.env 填入 license 和 token")
    print("  sudo cp comate2api.service /etc/systemd/system/")
    print("  sudo systemctl daemon-reload")
    print("  sudo systemctl enable --now comate2api")
    print("  journalctl -u comate2api -f")


def gen_windows_autostart():
    """Windows 不提供开机自启，只做启停脚本。

    原因：计划任务在非交互会话里跑 .bat 会被 pause 卡住，
    且用户注销后行为不一致。启停脚本更可靠也更直观。
    """
    print("Windows 使用启停脚本（不做开机自启）：")
    print("  启动服务.bat   双击启动，自动记录 PID")
    print("  停止服务.bat   停止服务（支持 stop/restart/status）")
    print()
    print("  Windows 下如需常驻，建议用 Docker Desktop 跑：")
    print("    docker compose up -d    # 自带 restart: unless-stopped")
    print()


def main():
    ap = argparse.ArgumentParser(description="生成部署文件")
    ap.add_argument("--docker", action="store_true", help="生成 Docker 部署")
    ap.add_argument("--systemd", action="store_true", help="生成 systemd 部署")
    ap.add_argument("--windows", action="store_true", help="说明 Windows 启停用法")
    ap.add_argument("--all", action="store_true", help="全部生成")
    args = ap.parse_args()

    if not any([args.docker, args.systemd, args.windows, args.all]):
        args.all = True

    print()
    if args.docker or args.all:
        gen_docker()
        print()
    if args.systemd or args.all:
        gen_systemd()
        print()
    if args.windows or args.all:
        gen_windows_autostart()
        print()

    print("注意：以上文件均不含任何凭证，需要使用者自己填。")
    print()


if __name__ == "__main__":
    main()
