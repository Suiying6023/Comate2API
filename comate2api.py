#!/usr/bin/env python3
"""
Comate 2API — 把文心快码的登录态封装成 OpenAI 兼容接口（带风控）。

设计原则：
  1. 零第三方依赖，只用 Python 标准库。
  2. 凭证一次性配置，之后完全脱离 IDE 独立运行。
  3. 严格复刻官方客户端的三步调用链，不做协议猜测。
  4. 所有对外行为受风控模块约束，避免被判定为异常客户端。

官方调用链（从 comate-engine/server.js 逆向确认）：
  POST /rest/autowork/v2/conversation   -> data.id       会话 ID（服务端下发）
  POST /rest/autowork/v2/task           -> data.taskId   任务 ID（服务端下发）
  POST /rest/autowork/v2/execute        -> SSE 流        推理

关键约束（实测得出，每条都是踩坑换来的）：
  * conversationId / taskId 必须服务端下发，自造会被判越权（403 安全策略）。
  * toolUseResults 字段名是 id，不是 toolId；首轮也必须非空，否则 400。
  * modelKey 必须精确 modelId（带哈希后缀），用 displayName 报 model.unavailable。
  * uploadBaseInfo 必填，缺一即 400。

用法：
  # 单凭证
  set COMATE_LICENSE=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
  python comate2api.py --port 8181

  # 多凭证（逗号分隔，自动轮询 + 故障转移）
  set COMATE_LICENSES=lic1,lic2,lic3
  python comate2api.py --port 8181

  # 公网部署务必设 API 鉴权
  set COMATE_API_TOKEN=your-secret-token
  python comate2api.py --port 8181

测试：
  curl http://127.0.0.1:8181/v1/models
  curl http://127.0.0.1:8181/v1/chat/completions \
    -H "Content-Type: application/json" \
    -d '{"model":"deepseek-v4-flash_a8668ea86c3d40e7ad1f8ee45715d447",
         "messages":[{"role":"user","content":"你好"}]}'
  curl http://127.0.0.1:8181/admin/pool      # 查看凭证池与风控状态
"""

import argparse
import json
import math
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.client import IncompleteRead

import riskguard
from riskguard import (
    CircuitBreaker,
    ClientFingerprint,
    CredentialPool,
    RiskLevel,
    TokenBucket,
    UsageTracker,
    classify_biz_error,
    classify_http_error,
    load_credentials_from_env,
)

# ==================== 常量（来自官方客户端，非猜测） ====================

COMATE_BASE = "https://comate.baidu.com/api"
AUTOMATE_PATH = "/aidevops/autocomate"

# SSE 无数据中阈值。官方客户端也会遇到长时间无响应，
# 超过这个时长就主动断开，避免连接悬挂耗尽线程。
SSE_IDLE_TIMEOUT = float(os.environ.get("COMATE_SSE_IDLE_TIMEOUT", "90"))

# 单次请求总超时
REQUEST_TIMEOUT = float(os.environ.get("COMATE_REQUEST_TIMEOUT", "300"))

# 上游建立连接的超时
CONNECT_TIMEOUT = 30.0

# 服务端 model.available 返回的模型目录快照。
# 注意：这里存的是精确 modelId（含哈希后缀），不是 displayName。
DEFAULT_MODELS = [
    ("deepseek-v4-flash_a8668ea86c3d40e7ad1f8ee45715d447", "Deepseek V4 Flash"),
    ("deepseek-v4-pro_4ba5277b630243be9eeb19fe48f853fa", "Deepseek V4 Pro"),
    ("deepseek-v4.1-flash_4827e158760447029e6594bb2b1ab066", "Deepseek V4.1 Flash"),
    ("ernie-5.5-flash-preview_996d9d76bb7c45ecbff286216aaeeab2", "ERNIE 5.5 Flash Preview"),
    ("glm-4.7-fc_0b87452a03394418b5c8d5468deff6c7", "GLM-4.7"),
    ("glm-5-turbo-fc_6748c832242b4bcb8f715c7c7902d1ad", "GLM-5-Turbo"),
    ("glm-5.1_a231cc317baf4eaaa0b5afdd4de5f1d9", "GLM-5.1"),
    ("glm-5.2_50c584482e9d47edbbe47938a88b56b1", "GLM-5.2"),
    ("glm-5.3_37c550fc6d5b4fb2825abc2c1c88a6b3", "GLM-5.3"),
    ("glm-5.3-flash_805131a9381a4940ba0f2d90cad0e7eb", "GLM-5.3-Flash"),
    ("glm-5v-turbo_ea9e4f222f0a40b281953ebcb6981dda", "GLM-5v-Turbo"),
    ("kimi-k2.6-oneapi_123e4567e89b12d3a456426614174000", "Kimi-K2.6"),
    ("minimax-m2.5-fc_c99ea14f526548a8bc28717d4e854123", "MiniMax M2.5"),
    ("minimax-m3_a8f465fbedb942efadace88370ad7ff1", "MiniMax M3"),
    ("minimax-m2.7-fc_047f123f661c414bb253c05b605c391e", "MiniMax-M2.7"),
]

# 设备指纹。服务端用它做会话归属校验，必须与创建会话时使用的值保持一致。
DEFAULT_DEVICE = os.environ.get("COMATE_DEVICE") or uuid.uuid4().hex


# ==================== 上游调用封装 ====================


class UpstreamError(Exception):
    """上游调用失败，携带风险信号。"""

    def __init__(self, signal, status=None):
        super().__init__(signal.message)
        self.signal = signal
        self.status = status


class ComateClient:
    """单个凭证对应一个客户端实例。

    每次请求独立连接，不做长连接复用 —— 官方 CLI 也是短连接，
    复用连接反而会形成与真实客户端不同的特征。
    """

    def __init__(self, license_key, username, fingerprint, device=None):
        self.license = license_key
        self.username = username or "comate2api"
        self.device = device or DEFAULT_DEVICE
        self.fp = fingerprint
        self.base = COMATE_BASE + AUTOMATE_PATH

    # ---------- 内部 ----------

    def _headers(self):
        return self.fp.with_auth(self.license)

    def _signal(self, signal):
        if self.license:
            signal.message = signal.message.replace(self.license, "[REDACTED]")
        return signal

    def _decode_json(self, raw):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = None
        if not isinstance(data, dict):
            raise UpstreamError(riskguard.RiskSignal(
                RiskLevel.WARN, "bad_json", "上游响应不是 JSON 对象"
            ))
        code = data.get("code")
        if isinstance(code, str) and code.isdigit():
            code = int(code)
        if isinstance(code, int) and code not in (0, 200):
            signal = classify_http_error(code, raw) if 400 <= code < 600 else classify_biz_error(raw)
            raise UpstreamError(self._signal(signal), code)
        return data

    def _upload_base_info(self):
        """服务端必填的环境信息块，缺任一字段即 400。"""
        return {
            "os": "windows",
            "osVersion": "Windows 11",
            "extName": "zulucli",
            "extVersion": self.fp.plugin_version,
            "ideType": "zulucli",
            "ideName": "zulucli",
            "ideVersion": self.fp.ide_version,
            "username": self.username,
            "license": self.license,
            "pluginVersion": self.fp.plugin_version,
            "device": self.device,
            "triggerSource": "",
        }

    def _post_json(self, path, payload, timeout=60):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.base + path, data=body, headers=self._headers(), method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            signal = classify_http_error(exc.code, detail)
            if exc.headers.get("Retry-After", "").isdigit():
                signal.retry_after = float(exc.headers["Retry-After"])
            raise UpstreamError(self._signal(signal), exc.code)
        except (urllib.error.URLError, OSError, IncompleteRead) as exc:
            raise UpstreamError(
                riskguard.RiskSignal(
                    RiskLevel.WARN, "network", f"网络错误: {exc}", None
                )
            )
        return self._decode_json(raw)

    # ---------- 公开 ----------

    def list_models(self, timeout=20):
        """拉取在线模型目录。失败时回落到内置快照。"""
        url = f"{COMATE_BASE}/v2/api/models/available?" + urllib.parse.urlencode({
            "key": self.license, "type": "ZULU", "repoName": "api",
            "loopVersion": "v2", "zuluVersion": self.fp.plugin_version,
            "source": "plugin", "ideType": "zulucli",
        })
        req = urllib.request.Request(url, headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = self._decode_json(resp.read().decode("utf-8", "replace"))
            out = []
            for m in data.get("data") or []:
                if not isinstance(m, dict):
                    continue
                mid = m.get("modelId")
                if mid:
                    out.append((mid, m.get("displayName") or mid))
            return out or list(DEFAULT_MODELS)
        except Exception as exc:
            detail = str(exc).replace(self.license, "[REDACTED]") if self.license else str(exc)
            print(f"[warn] 在线模型列表拉取失败，使用内置快照: {detail}", file=sys.stderr)
            return list(DEFAULT_MODELS)

    def create_conversation(self, query, timeout=60):
        """第一步：让服务端分配 conversationId。"""
        payload = {
            "username": self.license,
            "ide": "zulucli",
            "ideVersion": self.fp.ide_version,
            "pluginVersion": self.fp.plugin_version,
            "query": query,
            "uploadBaseInfo": self._upload_base_info(),
            "agentInfo": {"agentName": "Agent"},
        }
        # 业务码已在 _post_json 里校验过，这里只取数据
        data = self._post_json("/rest/autowork/v2/conversation", payload, timeout)
        conv_id = (data.get("data") or {}).get("id")
        if not conv_id:
            raise UpstreamError(
                riskguard.RiskSignal(
                    RiskLevel.WARN, "conv_no_id", "会话响应缺少 id"
                )
            )
        return conv_id

    def create_task(self, conv_id, timeout=60):
        """第二步：让服务端分配 taskId。"""
        payload = {
            "agentInfo": {
                "isProjectAgent": False,
                "agentName": "Agent",
                "canInvokeAgents": False,
            },
            "conversationId": conv_id,
            "username": self.license,
            "ide": "zulucli",
            "ideVersion": self.fp.ide_version,
            "pluginVersion": self.fp.plugin_version,
        }
        data = self._post_json("/rest/autowork/v2/task", payload, timeout)
        task_id = (data.get("data") or {}).get("taskId")
        if not task_id:
            raise UpstreamError(
                riskguard.RiskSignal(
                    RiskLevel.WARN, "task_no_id", "任务响应缺少 taskId"
                )
            )
        return task_id

    def open_stream(self, conv_id, task_id, model_id, query, timeout=CONNECT_TIMEOUT):
        """第三步：发起推理，返回已打开的连接供调用方流式读取。

        返回 response。调用方负责关闭。
        这个设计让 SSE 能边收边发，而不是先全收完再返回。
        """
        payload = {
            "username": self.license,
            "ide": "zulucli",
            "ideVersion": self.fp.ide_version,
            "pluginVersion": self.fp.plugin_version,
            "query": query,
            "conversationId": conv_id,
            "taskId": task_id,
            "modelKey": model_id,
            "modelProvider": "anthropic",
            "agentInfo": {"agentName": "Agent", "isProjectAgent": False},
            "uploadBaseInfo": self._upload_base_info(),
            "contexts": [],
            "subAgents": [],
            "isFirstQuery": True,
            "isUserQuery": True,
            "needCompression": False,
            # 首轮也必须带非空 toolUseResults，字段名是 id（不是 toolId）。
            "toolUseResults": [
                {
                    "id": "init",
                    "name": "noop",
                    "success": True,
                    "params": {},
                    "message": "ok",
                }
            ],
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = self._headers()
        headers["Accept"] = "text/event-stream"
        req = urllib.request.Request(
            self.base + "/rest/autowork/v2/execute",
            data=body,
            headers=headers,
            method="POST",
        )
        try:
            resp = urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            signal = classify_http_error(exc.code, detail)
            if exc.headers.get("Retry-After", "").isdigit():
                signal.retry_after = float(exc.headers["Retry-After"])
            raise UpstreamError(self._signal(signal), exc.code)
        except (urllib.error.URLError, OSError, IncompleteRead) as exc:
            raise UpstreamError(
                riskguard.RiskSignal(
                    RiskLevel.WARN, "network", f"网络错误: {exc}"
                )
            )
        if "application/json" in resp.headers.get("Content-Type", ""):
            try:
                self._decode_json(resp.read().decode("utf-8", "replace"))
            finally:
                resp.close()
            raise UpstreamError(riskguard.RiskSignal(
                RiskLevel.WARN, "unexpected_response", "推理接口未返回 SSE 流"
            ))
        return resp


def parse_frame(obj):
    """解析单个 SSE 帧，返回 (kind, payload)。

    kind: answer | reasoning | usage | exception | heartbeat | other
    """
    content = obj.get("content") or {}
    ctype = content.get("type")
    detail = content.get("detail") or {}

    if ctype == "ANSWER":
        if detail.get("delta"):
            return "answer", detail["delta"]
        if detail.get("reasoningDelta"):
            return "reasoning", detail["reasoningDelta"]
        return "heartbeat", None
    if ctype == "EXCEPTION":
        return "exception", detail.get("exceptionMsg") or "未知错误"
    if ctype == "TOKEN_USAGE" and isinstance(detail.get("usage"), dict):
        return "usage", detail["usage"]
    return "other", None


def remaining_timeout(deadline, limit):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise UpstreamError(riskguard.RiskSignal(
            RiskLevel.WARN, "request_timeout", "上游调用超过总超时时间"
        ))
    return min(limit, remaining)


def iter_sse_events(resp, deadline):
    """按 SSE 事件边界读取；socket 等待也受空闲与总时限约束。"""
    # urllib 的 HTTPResponse 使用 BufferedReader / SocketIO 保存连接。
    sock = getattr(getattr(resp.fp, "raw", None), "_sock", None)
    buffer = b""
    data_lines = []
    last_data = time.monotonic()
    while True:
        total_left = remaining_timeout(deadline, REQUEST_TIMEOUT)
        idle_left = SSE_IDLE_TIMEOUT - (time.monotonic() - last_data)
        if idle_left <= 0:
            raise UpstreamError(riskguard.RiskSignal(
                RiskLevel.WARN, "sse_idle_timeout", f"SSE {SSE_IDLE_TIMEOUT:g}s 无数据"
            ))
        if sock is not None:
            sock.settimeout(min(total_left, idle_left))
        try:
            block = resp.read1(65536)
        except (TimeoutError, socket.timeout):
            code = "request_timeout" if time.monotonic() >= deadline else "sse_idle_timeout"
            raise UpstreamError(riskguard.RiskSignal(
                RiskLevel.WARN, code, "上游调用超过总超时时间" if code == "request_timeout" else f"SSE {SSE_IDLE_TIMEOUT:g}s 无数据"
            ))
        except (OSError, IncompleteRead):
            raise UpstreamError(riskguard.RiskSignal(
                RiskLevel.WARN, "sse_truncated", "上游连接异常中断，回复未完成"
            ))
        if not block:
            return
        last_data = time.monotonic()
        buffer += block
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            line = line.rstrip(b"\r")
            if not line:
                if data_lines:
                    yield b"\n".join(data_lines).decode("utf-8", "replace")
                    data_lines.clear()
            elif line.startswith(b"data:"):
                value = line[5:]
                data_lines.append(value[1:] if value.startswith(b" ") else value)


# ==================== 模型名解析 ====================


class ModelResolver:
    """把用户传的模型名（ID 或显示名）解析成精确 modelId。"""

    def __init__(self, client_getter):
        self._client_getter = client_getter
        self._lock = threading.Lock()
        self._cache = None
        self._cache_at = 0.0
        self._ttl = 600.0  # 模型目录缓存 10 分钟

    def _catalog(self):
        with self._lock:
            now = time.time()
            if self._cache and (now - self._cache_at) < self._ttl:
                return self._cache
            client = self._client_getter()
            items = client.list_models() if client else list(DEFAULT_MODELS)
            self._cache = items
            self._cache_at = now
            return items

    def resolve(self, name):
        if not name:
            return None, None
        items = self._catalog()
        for mid, disp in items:
            if name == mid:
                return mid, disp
        low = name.strip().lower()
        for mid, disp in items:
            if disp.lower() == low:
                return mid, disp

        def norm(s):
            return "".join(ch for ch in s.lower() if ch.isalnum())

        target = norm(name)
        for mid, disp in items:
            if norm(disp) == target:
                return mid, disp
        return None, None

    def all(self):
        return self._catalog()

    def invalidate(self):
        with self._lock:
            self._cache = None


# ==================== 调度器 ====================


class Scheduler:
    """把限流、熔断、凭证池组合成对外的统一入口。

    一次调用最多尝试 len(pool) 个凭证。失败时：
      * 危险信号（鉴权/额度/风控）→ 冷却该凭证，换下一个
      * 普通信号（网络/5xx）→ 计数但不冷却，换下一个
    全部失败则抛 UpstreamError。
    """

    def __init__(self, pool, bucket, breaker, usage, fp):
        self.pool = pool
        self.bucket = bucket
        self.breaker = breaker
        self.usage = usage
        self.fp = fp
        self._client_cache = {}

    def client_for(self, cred_id):
        c = self._client_cache.get(cred_id)
        if c is None:
            cred = self.pool.creds[cred_id]
            c = ComateClient(
                cred["license"],
                cred.get("username"),
                self.fp,
                cred.get("device"),
            )
            self._client_cache[cred_id] = c
        return c

    def _wait_turn(self, timeout=10.0):
        """等待令牌。拿不到就抛。"""
        if not self.bucket.consume(1.0, timeout=timeout):
            raise UpstreamError(
                riskguard.RiskSignal(
                    RiskLevel.WARN,
                    "local_rate_limited",
                    f"本地限流：等待 {timeout}s 仍无可用令牌",
                    max(1.0, 1.0 / self.bucket.rate),
                )
            )

    def run_chain(self, query, model_id, on_frame=None):
        """执行完整链路。

        on_frame(obj, kind, payload) 会被每个 SSE 帧调用；返回 False 则中止读取。
        返回 (conv_id, task_id, frames, error_signal)。

        这是流式与非流式共用的底层实现。
        """
        last_error = None
        tried = set()
        deadline = time.monotonic() + REQUEST_TIMEOUT

        # 没配凭证时给出明确指引，而不是抛一个看不懂的上游错误
        if not any(c.get("license") for c in self.pool.creds.values()):
            raise UpstreamError(
                riskguard.RiskSignal(
                    RiskLevel.DANGER,
                    "no_credential",
                    "尚未配置凭证。请编辑同目录下 cred.json 填入 license，"
                    "然后重启服务",
                )
            )

        for _attempt in range(len(self.pool.healths)):
            remaining_timeout(deadline, REQUEST_TIMEOUT)
            if not self.breaker.allow():
                snap = self.breaker.snapshot()
                raise UpstreamError(
                    riskguard.RiskSignal(
                        RiskLevel.WARN,
                        "circuit_open",
                        f"熔断中，剩余 {snap['open_seconds_remaining']}s",
                        max(1, snap["open_seconds_remaining"]),
                    )
                )

            cred_id = None
            attempted = False
            had_output = False
            had_answer = False
            frames = []
            resp = None
            conv_id = task_id = None
            try:
                cred_id, _cred = self.pool.acquire(exclude=tried)
                if not cred_id:
                    if last_error:
                        raise last_error
                    raise UpstreamError(riskguard.RiskSignal(
                        RiskLevel.DANGER, "no_credential", "所有凭证均在冷却或已失效"
                    ))
                tried.add(cred_id)
                self._wait_turn(timeout=remaining_timeout(deadline, 10.0))
                client = self.client_for(cred_id)
                attempted = True
                conv_id = client.create_conversation(query, timeout=remaining_timeout(deadline, 60))
                task_id = client.create_task(conv_id, timeout=remaining_timeout(deadline, 60))
                resp = client.open_stream(conv_id, task_id, model_id, query,
                                          timeout=remaining_timeout(deadline, CONNECT_TIMEOUT))

                completed = False
                answer_end = False
                for chunk in iter_sse_events(resp, deadline):
                    if chunk == "[DONE]":
                        completed = True
                        break
                    try:
                        obj = json.loads(chunk)
                    except json.JSONDecodeError:
                        raise UpstreamError(riskguard.RiskSignal(
                            RiskLevel.WARN, "bad_sse", "上游 SSE 事件不是合法 JSON"
                        ))
                    if not isinstance(obj, dict) or not isinstance(obj.get("content"), dict):
                        # HTTP 200 的 JSON 业务错误也可能被装进 data: 事件。
                        client._decode_json(chunk)
                        raise UpstreamError(riskguard.RiskSignal(
                            RiskLevel.WARN, "bad_sse", "上游 SSE 事件缺少 content"
                        ))

                    frames.append(obj)
                    content = obj["content"]
                    detail = content.get("detail") or {}
                    kind, payload = parse_frame(obj)
                    if obj.get("cancelled") is True:
                        raise UpstreamError(riskguard.RiskSignal(
                            RiskLevel.WARN, "upstream_cancelled", "上游取消了本次推理"
                        ))
                    if kind == "exception":
                        raise UpstreamError(client._signal(classify_biz_error(payload)))
                    events = [(kind, payload)]
                    if kind == "answer" and detail.get("reasoningDelta"):
                        events.insert(0, ("reasoning", detail["reasoningDelta"]))
                    for kind, payload in events:
                        if kind in ("answer", "reasoning"):
                            had_output = True
                        if kind == "answer":
                            had_answer = True
                        if on_frame and on_frame(obj, kind, payload) is False:
                            return conv_id, task_id, frames, None
                    answer_end = answer_end or detail.get("end") is True
                    # detail.end 之后仍可能有 TOKEN_USAGE；content.end 才结束整个流。
                    if content.get("end") is True:
                        completed = True
                        break

                if not completed and not answer_end:
                    raise UpstreamError(riskguard.RiskSignal(
                        RiskLevel.WARN, "sse_truncated", "上游连接提前结束，回复未完成"
                    ))
                if not had_answer:
                    raise UpstreamError(riskguard.RiskSignal(
                        RiskLevel.WARN, "empty_response", "上游没有返回回复内容"
                    ))

                self.pool.mark_success(cred_id)
                self.breaker.record_success()
                self.usage.record()
                return conv_id, task_id, frames, None

            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                # 用户停止或断开连接时，不重放请求，也不惩罚凭证。
                raise
            except UpstreamError as exc:
                if not attempted:
                    raise
                last_error = exc
                state = self.pool.mark_failure(cred_id, exc.signal)
                self.breaker.record_failure()
                if state == "dead":
                    print(
                        "[warn] 一个凭证连续鉴权/额度失败过多，已停用",
                        file=sys.stderr,
                    )
                if had_output:
                    return conv_id, task_id, frames, exc.signal
            except Exception as exc:
                message = f"调用异常: {exc}"
                if cred_id:
                    message = message.replace(self.pool.creds[cred_id]["license"], "[REDACTED]")
                last_error = UpstreamError(
                    riskguard.RiskSignal(
                        RiskLevel.WARN, "internal", message
                    )
                )
                self.pool.mark_failure(cred_id, last_error.signal)
                self.breaker.record_failure()
                if had_output:
                    return conv_id, task_id, frames, last_error.signal
            finally:
                if resp is not None:
                    try:
                        resp.close()
                    except Exception:
                        pass
                self.breaker.release()

        if last_error:
            raise last_error
        raise UpstreamError(
            riskguard.RiskSignal(RiskLevel.WARN, "exhausted", "所有凭证均调用失败")
        )


# ==================== OpenAI 兼容层 ====================


def to_openai_models(items):
    created = int(time.time())
    return {
        "object": "list",
        "data": [
            {"id": mid, "object": "model", "created": created, "owned_by": "comate"}
            for mid, _disp in items
        ],
    }


def make_completion_id():
    return "chatcmpl-" + uuid.uuid4().hex


def merge_usage(total, payload):
    """Comate 的 inputTokens 不含 cachedInputTokens，二者共同组成 prompt。"""
    if total is None:
        total = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
                 "prompt_tokens_details": {"cached_tokens": 0}}
    cached = int(payload.get("cachedInputTokens") or 0)
    prompt = int(payload.get("inputTokens") or 0) + cached
    completion = int(payload.get("outputTokens") or 0)
    total["prompt_tokens"] += prompt
    total["completion_tokens"] += completion
    total["total_tokens"] += prompt + completion
    total["prompt_tokens_details"]["cached_tokens"] += cached
    return total


def upstream_status(signal):
    if signal.code in ("local_rate_limited", "http_429"):
        return 429
    if signal.code in ("no_credential", "circuit_open"):
        return 503
    if signal.code in ("sse_idle_timeout", "request_timeout", "http_504"):
        return 504
    return 502


def make_chunk(cid, model, delta, finish_reason=None, created=None):
    return {
        "id": cid,
        "object": "chat.completion.chunk",
        "created": int(time.time()) if created is None else created,
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }


# ==================== HTTP 服务 ====================


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "comate2api/1.0"

    # 由 serve() 注入
    scheduler = None
    pool = None
    resolver = None
    usage = None
    breaker = None
    bucket = None
    fp = None
    api_tokens = ()

    # ---------- 工具 ----------

    def handle_one_request(self):
        """接管请求读取，吞掉客户端提前断开的噪音。

        SSE 场景下客户端随时可能关闭连接（用户按停止、
        切页面），默认实现会打印一大段 ConnectionResetError
        堆栈，掩盖真正的错误。
        """
        try:
            super().handle_one_request()
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            self.close_connection = True

    def handle(self):
        try:
            super().handle()
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            pass

    def log_message(self, fmt, *args):
        pass

    def _send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors_headers()
        if self.close_connection:
            self.send_header("Connection", "close")
        retry_after = getattr(self, "_retry_after", None)
        if retry_after:
            self.send_header("Retry-After", str(max(1, math.ceil(retry_after))))
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, status, message, err_type="invalid_request_error", code=None):
        self.close_connection = True
        self._send_json(
            {"error": {"message": message, "type": err_type, "code": status if code is None else code}},
            status=status,
        )

    def _send_upstream_error(self, signal):
        self._retry_after = signal.retry_after
        return self._send_error_json(upstream_status(signal), signal.message, "upstream_error", signal.code)

    def _cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, X-API-Key")
        self.send_header("Access-Control-Expose-Headers", "Retry-After")

    def _read_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        if length < 0:
            return None
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw.decode("utf-8"))
            return body if isinstance(body, dict) else None
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None

    def _start_sse(self):
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        # 没有 Content-Length 的 HTTP/1.1 SSE 必须在结束时关闭连接。
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self._cors_headers()
        self.end_headers()

    def _write_sse(self, payload):
        self.wfile.write(
            ("data: " + json.dumps(payload, ensure_ascii=False) + "\n\n").encode("utf-8")
        )
        self.wfile.flush()

    def _check_auth(self):
        """API 鉴权。只在配置了 COMATE_API_TOKEN 时启用。

        公网部署必须开，否则任何人都能消耗你的额度。
        """
        if not self.api_tokens:
            return True
        auth = self.headers.get("Authorization", "")
        token = ""
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
        if not token:
            token = self.headers.get("x-api-key", "").strip()
        return token in self.api_tokens

    def _is_admin(self):
        """管理接口鉴权。默认要求 API token。"""
        if not self.api_tokens:
            # 没配 token 时仅允许本机访问管理接口
            return self.client_address[0] in ("127.0.0.1", "::1")
        return self._check_auth()

    @staticmethod
    def _flatten_messages(messages):
        parts = []
        for m in messages:
            if not isinstance(m, dict):
                raise ValueError("messages 的每一项必须是对象")
            role = m.get("role") or "user"
            if role not in ("system", "developer", "user", "assistant"):
                raise ValueError("当前仅支持 system/developer/user/assistant 文本消息")
            if m.get("tool_calls") or m.get("function_call"):
                raise ValueError("当前尚未支持工具调用消息")
            content = m.get("content")
            if isinstance(content, list):
                if any(not isinstance(c, dict) or c.get("type") != "text" or not isinstance(c.get("text"), str)
                       for c in content):
                    raise ValueError("当前仅支持文本内容块，尚未支持图片、音频或文件")
                content = "\n".join(c["text"] for c in content)
            if not isinstance(content, str):
                raise ValueError("message.content 必须是字符串或文本内容块数组")
            if not content.strip():
                continue
            parts.append(f"{role}: {content.strip()}")
        return "\n\n".join(parts)

    # ---------- 路由 ----------

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors_headers()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?", 1)[0].rstrip("/")
        if path == "/health":
            return self._send_json({"status": "ok", "service": "comate2api"})
        if not self._check_auth():
            return self._send_error_json(401, "缺少或无效的 API token", "auth_error")

        if path == "":
            return self._send_json({"status": "ok", "service": "comate2api"})

        if path == "/v1/models":
            try:
                return self._send_json(to_openai_models(self.resolver.all()))
            except Exception as exc:
                return self._send_error_json(500, f"模型列表获取失败: {exc}", "server_error")

        if path == "/admin/pool":
            if not self._is_admin():
                return self._send_error_json(403, "管理接口需要 API token", "auth_error")
            return self._send_json(
                {
                    "credentials": self.pool.snapshot(),
                    "healthy": self.pool.healthy_count(),
                    "circuit": self.breaker.snapshot(),
                    "usage": self.usage.snapshot(),
                    "rate_limit": {
                        "rps": self.bucket.rate,
                        "capacity": self.bucket.capacity,
                        "available": round(self.bucket.tokens, 2),
                    },
                }
            )

        if path == "/admin/revive":
            if not self._is_admin():
                return self._send_error_json(403, "管理接口需要 API token", "auth_error")
            self.pool.force_revive()
            self.breaker.record_success()
            self.resolver.invalidate()
            return self._send_json({"status": "ok", "revived": True})

        return self._send_error_json(404, f"未知路径: {self.path}")

    def do_POST(self):
        if not self._check_auth():
            return self._send_error_json(401, "缺少或无效的 API token", "auth_error")

        path = self.path.split("?", 1)[0].rstrip("/")
        if path == "/admin/revive":
            if not self._is_admin():
                return self._send_error_json(403, "管理接口需要 API token", "auth_error")
            self.pool.force_revive()
            self.breaker.record_success()
            self.resolver.invalidate()
            self.close_connection = True
            return self._send_json({"status": "ok", "revived": True})
        if path != "/v1/chat/completions":
            return self._send_error_json(404, f"未知路径: {self.path}")

        body = self._read_body()
        if body is None:
            return self._send_error_json(400, "请求体必须是合法 JSON 对象")

        requested = body.get("model")
        if not isinstance(requested, str) or not requested.strip():
            return self._send_error_json(400, "model 必须是非空字符串")
        stream = body.get("stream", False)
        if not isinstance(stream, bool):
            return self._send_error_json(400, "stream 必须是布尔值")
        options = body.get("stream_options") or {}
        if not isinstance(options, dict) or not isinstance(options.get("include_usage", False), bool):
            return self._send_error_json(400, "stream_options.include_usage 必须是布尔值")
        if (body.get("tools") or body.get("functions")
                or body.get("tool_choice") not in (None, "none")
                or body.get("function_call") not in (None, "none")):
            return self._send_error_json(400, "当前尚未支持 tools/tool_choice，请使用纯文本对话")
        if body.get("n", 1) != 1:
            return self._send_error_json(400, "当前仅支持 n=1")
        response_format = body.get("response_format")
        if response_format is not None and response_format != {"type": "text"}:
            return self._send_error_json(400, "当前尚未支持结构化 response_format")

        messages = body.get("messages")
        if not isinstance(messages, list) or not messages:
            return self._send_error_json(400, "messages 不能为空")

        try:
            query = self._flatten_messages(messages)
        except ValueError as exc:
            return self._send_error_json(400, str(exc))
        if not query.strip():
            return self._send_error_json(400, "messages 里没有可用的文本内容")

        model_id, display = self.resolver.resolve(requested)
        if not model_id:
            available = ", ".join(mid for mid, _ in self.resolver.all()[:5])
            return self._send_error_json(
                404, f"模型 '{requested}' 不存在。可用示例: {available}"
            )

        if stream:
            return self._handle_stream(query, model_id, requested, options.get("include_usage", False))
        return self._handle_nonstream(query, model_id, requested or model_id)

    def _handle_nonstream(self, query, model_id, model_label):
        collected = []
        usage = None

        def on_frame(obj, kind, payload):
            nonlocal usage
            if kind in ("answer", "reasoning"):
                collected.append((kind, payload))
            elif kind == "usage":
                usage = merge_usage(usage, payload)
            return True

        try:
            _c, _t, _frames, error = self.scheduler.run_chain(
                query, model_id, on_frame
            )
        except UpstreamError as exc:
            return self._send_upstream_error(exc.signal)

        answer = "".join(p for k, p in collected if k == "answer")
        reasoning = "".join(p for k, p in collected if k == "reasoning")

        if error:
            return self._send_upstream_error(error)

        msg = {"role": "assistant", "content": answer}
        if reasoning:
            msg["reasoning_content"] = reasoning

        return self._send_json(
            {
                "id": make_completion_id(),
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model_label,
                "choices": [{"index": 0, "message": msg, "finish_reason": "stop"}],
                "usage": usage,
            }
        )

    def _handle_stream(self, query, model_id, model_label, include_usage=False):
        # 先建立上游再发响应头，这样建连失败还能返回正常 JSON 错误
        cid = make_completion_id()
        created = int(time.time())
        started = False
        usage = None

        def emit(delta, finish_reason=None):
            chunk = make_chunk(cid, model_label, delta, finish_reason, created=created)
            if include_usage:
                chunk["usage"] = None
            self._write_sse(chunk)

        def on_frame(obj, kind, payload):
            nonlocal started, usage
            if kind == "usage":
                usage = merge_usage(usage, payload)
                return True
            if not started and kind in ("answer", "reasoning", "heartbeat"):
                # 首帧到达才正式开 SSE
                self._start_sse()
                started = True
                emit({"role": "assistant", "content": ""})
            if kind == "answer":
                emit({"content": payload})
            elif kind == "reasoning":
                emit({"reasoning_content": payload})
            return True

        try:
            _c, _t, _frames, error = self.scheduler.run_chain(
                query, model_id, on_frame
            )
        except UpstreamError as exc:
            error = exc.signal

        if error:
            if not started:
                return self._send_upstream_error(error)
            self._write_sse({"error": {"message": error.message, "type": "upstream_error", "code": error.code}})
        else:
            emit({}, finish_reason="stop")
            if include_usage:
                chunk = make_chunk(cid, model_label, {}, created=created)
                chunk["choices"] = []
                chunk["usage"] = usage
                self._write_sse(chunk)
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


def _primary_cred_id(pool):
    """取一个稳定的凭证 ID 用于控制面请求（模型列表）。

    控制面请求不应该走轮询，否则每次拿到的凭证不同，
    会让模型目录缓存变得不可预测。
    """
    with pool._lock:
        for cid in pool.creds:
            return cid
    return None


class QuietThreadingHTTPServer(ThreadingHTTPServer):
    """静默版 HTTP 服务。

    默认实现遇到客户端断开会打印完整堆栈，
    对 SSE 这类长连接服务是纯噪音。
    """

    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(
            exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)
        ):
            return
        super().handle_error(request, client_address)

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            pass
        except Exception:
            self.handle_error(request, client_address)


def serve(host, port, credentials, api_tokens, rps, cooldown, verbose=True):
    fp = ClientFingerprint()

    # 允许无凭证启动：服务会正常监听，只是调用模型时返回明确错误。
    # 这样使用者能先看 /v1/models 和文档，配好凭证再重启。
    if not credentials:
        credentials = [{"license": "", "username": ""}]

    pool = CredentialPool(credentials, cooldown_seconds=cooldown)
    bucket = TokenBucket(rate=rps, capacity=max(1, int(rps)))
    breaker = CircuitBreaker()
    usage = UsageTracker()

    scheduler = Scheduler(pool, bucket, breaker, usage, fp)
    resolver = ModelResolver(lambda: scheduler.client_for(_primary_cred_id(pool)))

    Handler.scheduler = scheduler
    Handler.pool = pool
    Handler.resolver = resolver
    Handler.usage = usage
    Handler.breaker = breaker
    Handler.bucket = bucket
    Handler.fp = fp
    Handler.api_tokens = tuple(api_tokens)

    httpd = QuietThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True

    print(f"comate2api 已启动: http://{host}:{port}")
    print(f"  凭证数  : {len(credentials)}")
    print(f"  限流    : {rps} req/s (突发上限 {bucket.capacity})")
    print(f"  冷却时长: {cooldown}s (指数退避，上限 3600s)")
    print(f"  API鉴权 : {'已启用' if api_tokens else '未启用（仅本机可访问管理接口）'}")
    print()
    print("接口:")
    print(f"  GET  http://{host}:{port}/v1/models")
    print(f"  POST http://{host}:{port}/v1/chat/completions")
    print(f"  GET  http://{host}:{port}/admin/pool     凭证池与风控状态")
    print(f"  GET  http://{host}:{port}/admin/revive   手动复活所有凭证")
    print()

    # 启动与健康检查不依赖上游网络，模型目录由 resolver 首次访问时加载。
    print("模型目录将在首次访问 /v1/models 时加载（失败时使用内置快照）")

    print()
    print("按 Ctrl+C 停止")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        httpd.server_close()


def main():
    ap = argparse.ArgumentParser(description="把文心快码登录态封装成 OpenAI 兼容 API")
    ap.add_argument("--host", default="127.0.0.1", help="监听地址，默认仅本机")
    ap.add_argument("--port", type=int, default=8181, help="监听端口")
    ap.add_argument(
        "--license",
        default=os.environ.get("COMATE_LICENSE", ""),
        help="单凭证；多凭证用逗号分隔或用环境变量 COMATE_LICENSES",
    )
    ap.add_argument(
        "--username",
        default=os.environ.get("COMATE_USERNAME", ""),
        help="仅用于日志标识，可选",
    )
    ap.add_argument(
        "--api-token",
        action="append",
        default=None,
        help="客户端访问本服务的 token，可重复指定；也可用 COMATE_API_TOKEN",
    )
    ap.add_argument(
        "--rps",
        type=float,
        default=float(os.environ.get("COMATE_RPS", "2")),
        help="每秒请求上限，默认 2",
    )
    ap.add_argument(
        "--cooldown",
        type=int,
        default=int(os.environ.get("COMATE_COOLDOWN", "300")),
        help="凭证冷却基础秒数，默认 300（指数退避）",
    )
    args = ap.parse_args()

    creds = load_credentials_from_env()
    if args.license:
        for part in args.license.replace(";", ",").split(","):
            part = part.strip()
            if part and part not in [c["license"] for c in creds]:
                creds.append({"license": part, "username": args.username})

    if not creds:
        print("错误: 缺少凭证，请设置 COMATE_LICENSE 或 COMATE_LICENSES", file=sys.stderr)
        print()
        print("用法示例:", file=sys.stderr)
        print("  set COMATE_LICENSE=你的license", file=sys.stderr)
        print("  python comate2api.py --port 8181", file=sys.stderr)
        return 2

    tokens = args.api_token or []
    env_tok = os.environ.get("COMATE_API_TOKEN", "")
    if env_tok:
        for t in env_tok.replace(";", ",").split(","):
            t = t.strip()
            if t and t not in tokens:
                tokens.append(t)

    for c in creds:
        c["license"] = c["license"].strip()
        if not c.get("username"):
            c["username"] = args.username

    serve(args.host, args.port, creds, tokens, args.rps, args.cooldown)
    return 0


if __name__ == "__main__":
    sys.exit(main())
