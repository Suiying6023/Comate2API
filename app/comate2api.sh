#!/usr/bin/env bash
#
# comate2api 服务控制脚本（Linux / macOS）
#
# 用法：
#   ./comate2api.sh start     后台启动
#   ./comate2api.sh stop      停止
#   ./comate2api.sh restart   重启
#   ./comate2api.sh status    查看状态
#   ./comate2api.sh log       看日志
#   ./comate2api.sh install   装成 systemd 服务（保活 + 开机自启）
#   ./comate2api.sh uninstall 卸载 systemd 服务
#

set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PIDFILE="$DIR/server.pid"
LOGFILE="$DIR/server.log"
SERVICE_NAME="comate2api"
PYTHON_BIN="${PYTHON_BIN:-}"

# 找 Python
find_python() {
    if [ -n "$PYTHON_BIN" ]; then
        echo "$PYTHON_BIN"
        return 0
    fi
    for c in python3 python; do
        if command -v "$c" >/dev/null 2>&1; then
            if "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3,8) else 1)' 2>/dev/null; then
                echo "$c"
                return 0
            fi
        fi
    done
    return 1
}

is_running() {
    if [ ! -f "$PIDFILE" ]; then
        return 1
    fi
    local pid
    pid="$(cat "$PIDFILE" 2>/dev/null || true)"
    if [ -z "$pid" ]; then
        return 1
    fi
    if kill -0 "$pid" 2>/dev/null; then
        return 0
    fi
    # PID 文件残留
    rm -f "$PIDFILE"
    return 1
}

show_info() {
    echo
    echo "============================================================"
    echo "  连接信息"
    echo "============================================================"
    echo
    if [ -f "$DIR/connection.json" ]; then
        cat "$DIR/connection.json"
        echo
    else
        echo "  (connection.json 尚未生成，可能启动失败)"
        echo
    fi
}

start() {
    if is_running; then
        echo "服务已在运行（PID $(cat "$PIDFILE")）"
        exit 0
    fi

    local py
    if ! py="$(find_python)"; then
        echo "[错误] 没找到 Python 3.8+"
        echo "  Ubuntu/Debian: sudo apt install -y python3"
        echo "  CentOS/RHEL  : sudo yum install -y python3"
        exit 1
    fi

    echo "使用 Python: $py ($($py --version 2>&1))"

    if [ -f "$DIR/service.env" ]; then
        set -a
        source "$DIR/service.env"
        set +a
    fi

    # 检查关键文件
    if [ ! -f "$DIR/start.py" ] || [ ! -f "$DIR/comate2api.py" ]; then
        echo "[错误] 缺少 start.py 或 comate2api.py"
        echo "  请确认整个目录完整，不要只拷单个文件"
        exit 1
    fi

    echo "正在后台启动..."
    nohup "$py" "$DIR/start.py" >> "$LOGFILE" 2>&1 &
    local pid=$!
    echo "$pid" > "$PIDFILE"

    # 等端口就绪
    local i=0
    while [ $i -lt 30 ]; do
        sleep 1
        i=$((i+1))
        if ! kill -0 "$pid" 2>/dev/null; then
            echo
            echo "[错误] 进程启动后立即退出，看日志："
            echo "  tail -30 $LOGFILE"
            rm -f "$PIDFILE"
            exit 1
        fi
        if grep -q "已启动" "$LOGFILE" 2>/dev/null; then
            break
        fi
    done

    echo "服务已启动（PID $pid）"
    show_info
    echo "  日志: $LOGFILE"
    echo "  停止: $0 stop"
    echo
}

stop() {
    if ! is_running; then
        echo "服务未在运行"
        return 0
    fi
    local pid
    pid="$(cat "$PIDFILE")"
    echo "正在停止 PID $pid ..."
    kill "$pid" 2>/dev/null || true

    local i=0
    while [ $i -lt 15 ]; do
        sleep 1
        i=$((i+1))
        if ! kill -0 "$pid" 2>/dev/null; then
            break
        fi
    done

    if kill -0 "$pid" 2>/dev/null; then
        echo "优雅停止超时，强制结束..."
        kill -9 "$pid" 2>/dev/null || true
    fi

    rm -f "$PIDFILE"
    echo "已停止"
}

status() {
    echo
    echo "============================================================"
    echo "  服务状态"
    echo "============================================================"
    echo
    if is_running; then
        echo "  [运行中] PID $(cat "$PIDFILE")"
        echo
        # 尝试拿端口和 URL
        if [ -f "$DIR/connection.json" ]; then
            echo "  连接信息:"
            cat "$DIR/connection.json"
            echo
        fi
    else
        echo "  [未运行]"
        echo
    fi

    # systemd 服务状态（如果装了）
    if command -v systemctl >/dev/null 2>&1; then
        if systemctl list-unit-files 2>/dev/null | grep -q "^${SERVICE_NAME}\.service"; then
            echo "  systemd 服务: $(systemctl is-active ${SERVICE_NAME} 2>/dev/null || echo unknown)"
            echo "  （systemd 管理时上面 PID 不适用）"
            echo
        fi
    fi
}

log() {
    if [ ! -f "$LOGFILE" ]; then
        echo "还没有日志文件"
        exit 0
    fi
    tail -f "$LOGFILE"
}

install_service() {
    if ! command -v systemctl >/dev/null 2>&1; then
        echo "[错误] 这台机器没有 systemctl，无法安装 systemd 服务"
        echo "  用 $0 start 手动后台运行，或用 nohup / supervisor"
        exit 1
    fi

    local py
    py="$(find_python)" || {
        echo "[错误] 没找到 Python"
        exit 1
    }

    local unit="/etc/systemd/system/${SERVICE_NAME}.service"

    echo "生成 systemd 服务文件: $unit"
    echo "  （需要 sudo 权限）"
    echo

    sudo tee "$unit" > /dev/null <<EOF
[Unit]
Description=comate2api - Baidu Comate OpenAI-compatible API
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$DIR
ExecStart=$py $DIR/start.py
Restart=always
RestartSec=5
StandardOutput=append:$LOGFILE
StandardError=append:$LOGFILE
EnvironmentFile=-$DIR/service.env

# 安全加固
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

    echo "重载 systemd 并启动..."
    sudo systemctl daemon-reload
    sudo systemctl enable "$SERVICE_NAME" >/dev/null 2>&1 || true
    sudo systemctl restart "$SERVICE_NAME"

    sleep 2
    echo
    echo "已安装并启动。常用命令："
    echo "  sudo systemctl status  $SERVICE_NAME"
    echo "  sudo systemctl restart $SERVICE_NAME"
    echo "  sudo journalctl -u $SERVICE_NAME -f"
    echo "  sudo systemctl disable --now $SERVICE_NAME   # 卸载"
    echo
    echo "注意：凭证放 $DIR/service.env，权限设 600："
    echo "  echo 'COMATE_LICENSE=你的license' > $DIR/service.env"
    echo "  chmod 600 $DIR/service.env"
    echo

    show_info
}

uninstall_service() {
    if ! command -v systemctl >/dev/null 2>&1; then
        echo "没有 systemctl"
        exit 1
    fi
    echo "停止并卸载 systemd 服务..."
    sudo systemctl disable --now "$SERVICE_NAME" 2>/dev/null || true
    sudo rm -f "/etc/systemd/system/${SERVICE_NAME}.service"
    sudo systemctl daemon-reload
    echo "已卸载"
}

case "${1:-}" in
    start)    start ;;
    stop)     stop ;;
    restart)  stop; start ;;
    status)   status ;;
    log)      log ;;
    install)  install_service ;;
    uninstall) uninstall_service ;;
    *)
        echo "用法: $0 {start|stop|restart|status|log|install|uninstall}"
        echo
        echo "  start      后台启动"
        echo "  stop       停止"
        echo "  restart    重启"
        echo "  status     查看状态"
        echo "  log        实时日志"
        echo "  install    装成 systemd 服务（保活 + 开机自启）"
        echo "  uninstall  卸载 systemd 服务"
        exit 1
        ;;
esac
