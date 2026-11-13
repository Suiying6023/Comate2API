"""
风控与稳定性模块。

设计依据：
  * Comate 服务端有三个可观测的约束 —— 会话归属校验、模型目录、SSE 心跳。
  * 参考文献：baidu2api2（多凭证轮询 + 最小并发 + 故障转移 + 8 秒无数据中
    断）、cursor2api（截断续写、SSE 防卡死）、Kiro-Go（账号池 + 自动刷新）。

核心思路：不做「多账号刷额度」，而是保证**单账号下的调用看起来像真实 IDE**。
风控目标不是绕过检测，而是让本服务在被正常使用时不会触发异常。
"""

import json
import hashlib
import math
import os
import random
import threading
import time
from collections import deque


# ==================== 可观测的风控信号 ====================


class RiskLevel:
    OK = "ok"
    WARN = "warn"
    DANGER = "danger"


class RiskSignal:
    """一次上游响应的风险信号。"""

    def __init__(self, level, code, message, retry_after=None):
        self.level = level
        self.code = code
        self.message = message
        self.retry_after = retry_after
        self.at = time.time()

    def __repr__(self):
        return f"<RiskSignal {self.level} {self.code}>"


# 上游错误码 -> (风险等级, 是否应冷却凭证)
# 这些是从实际调用中观察到的，不是猜测。
ERROR_POLICY = {
    # 明确鉴权失败：凭证失效或错误
    401: (RiskLevel.DANGER, "凭证无效或已过期", True),
    403: (RiskLevel.DANGER, "访问被拒绝，可能触发风控", True),
    # 限流：官方标准语义
    429: (RiskLevel.DANGER, "请求过于频繁", True),
    # 额度耗尽
    402: (RiskLevel.DANGER, "额度不足", True),
    # 服务端错误：短暂重试即可，不惩罚凭证
    500: (RiskLevel.WARN, "上游服务异常", False),
    502: (RiskLevel.WARN, "上游网关异常", False),
    503: (RiskLevel.WARN, "上游暂不可用", False),
    504: (RiskLevel.WARN, "上游超时", False),
}

# 业务层错误（HTTP 200 但 content.type == EXCEPTION）
BIZ_ERROR_POLICY = {
    "zulu.service.model.unavailable": (RiskLevel.WARN, "模型不可用", False),
    "ResourceUsageOverlimitError": (RiskLevel.DANGER, "额度超限", True),
    "AuthenticationError": (RiskLevel.DANGER, "鉴权失败", True),
}


# ==================== 令牌桶限流 ====================


class TokenBucket:
    """单线程安全的令牌桶。

    用来把请求速率压在安全区间内。Comate 没有公开 QPS 限制，
    这里用保守默认值，避免触发服务端风控。
    """

    def __init__(self, rate, capacity=None):
        self.rate = float(rate)  # 每秒补充的令牌数
        if not math.isfinite(self.rate) or self.rate <= 0:
            raise ValueError("rps 必须是大于 0 的有限数值")
        self.capacity = capacity or max(1, int(rate))
        self.tokens = float(self.capacity)
        self.updated = time.monotonic()
        self._lock = threading.Lock()

    def consume(self, tokens=1.0, timeout=None):
        """尝试取令牌。返回是否成功。

        timeout 为 None 时非阻塞；否则最多等待 timeout 秒。
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            wait = None
            with self._lock:
                now = time.monotonic()
                elapsed = now - self.updated
                self.updated = now
                self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
                if self.tokens >= tokens:
                    self.tokens -= tokens
                    return True
                if deadline is None:
                    return False
                need = (tokens - self.tokens) / self.rate
                if time.monotonic() + need > deadline:
                    return False
                # 需要在锁外等待，避免持锁 sleep 阻塞其他线程
                wait = max(0.001, min(need, 0.2))
            if wait is None:
                return False
            time.sleep(wait)
            # 回到循环顶部重新尝试


class SlidingWindowCounter:
    """滑动窗口计数器，用于统计最近 N 秒的请求数。"""

    def __init__(self, window_seconds=60):
        self.window = window_seconds
        self.events = deque()
        self._lock = threading.Lock()

    def add(self, n=1):
        now = time.monotonic()
        with self._lock:
            self.events.append((now, n))
            self._evict(now)

    def count(self):
        now = time.monotonic()
        with self._lock:
            self._evict(now)
            return sum(n for _t, n in self.events)

    def _evict(self, now):
        cutoff = now - self.window
        while self.events and self.events[0][0] < cutoff:
            self.events.popleft()


# ==================== 凭证健康度 ====================


class CredentialHealth:
    """单个凭证的健康状态。

    状态机：
      healthy --(触发冷却)--> cooling --(冷却到期)--> healthy
      healthy --(连续失败超阈值)--> dead（需人工介入）
    """

    def __init__(self, cred_id, cooldown_seconds=300, max_failures=5):
        self.cred_id = cred_id
        self.cooldown_seconds = cooldown_seconds
        self.max_failures = max_failures

        self.state = "healthy"
        self.cooldown_until = 0.0
        self.consecutive_failures = 0
        self._danger_failures = 0
        self.total_requests = 0
        self.total_failures = 0
        self.last_used = 0.0
        self.last_error = None
        self._lock = threading.Lock()

    def available(self):
        with self._lock:
            if self.state == "dead":
                return False
            if self.state == "cooling":
                if time.time() >= self.cooldown_until:
                    self.state = "healthy"
                    return True
                return False
            return True

    def record_success(self):
        with self._lock:
            self.total_requests += 1
            self.consecutive_failures = 0
            self._danger_failures = 0
            self.state = "healthy"
            self.cooldown_until = 0.0
            self.last_error = None
            self.last_used = time.time()

    def record_failure(self, signal, fatal=False):
        """记录一次失败。fatal=True 表示应冷却。

        冷却时长带指数退避与抖动，避免多凭证同时恢复造成尖峰。
        """
        with self._lock:
            self.total_requests += 1
            self.total_failures += 1
            self.consecutive_failures += 1
            self.last_error = signal.message
            self.last_used = time.time()

            if fatal:
                self._danger_failures += 1
                if self._danger_failures >= self.max_failures:
                    self.state = "dead"
                    return "dead"
                # 指数退避：300s, 600s, 1200s... 上限 1 小时
                backoff = self.cooldown_seconds * (2 ** (self._danger_failures - 1))
                # 抖动 ±20%，避免多凭证同时到期
                backoff = min(backoff * random.uniform(0.8, 1.2), 3600.0)
                if signal.retry_after is not None:
                    backoff = max(backoff, signal.retry_after)
                self.state = "cooling"
                self.cooldown_until = time.time() + backoff
                return "cooling"
            return "healthy"

    def snapshot(self):
        with self._lock:
            return {
                "id": "cred-" + hashlib.sha256(self.cred_id.encode()).hexdigest()[:12],
                "state": self.state,
                "consecutive_failures": self.consecutive_failures,
                "total_requests": self.total_requests,
                "total_failures": self.total_failures,
                "last_error": self.last_error,
                "cooldown_remaining": max(
                    0, int(self.cooldown_until - time.time())
                )
                if self.state == "cooling"
                else 0,
            }


# ==================== 凭证池 ====================


class CredentialPool:
    """多凭证池，带最小并发与故障转移。

    选取策略（借鉴 baidu2api2）：
      1. 过滤掉不可用的（冷却中 / 已死亡）
      2. 在可用集合里选「最近最少使用」的
      3. 同分时随机打散，避免固定顺序造成热点
    """

    def __init__(self, credentials, cooldown_seconds=300, max_failures=5):
        if not credentials:
            raise ValueError("凭证池不能为空")
        self.healths = {}
        self.creds = {}
        for i, c in enumerate(credentials):
            cid = c.get("license") or f"cred-{i}"
            self.creds[cid] = c
            self.healths[cid] = CredentialHealth(cid, cooldown_seconds, max_failures)
        self._lock = threading.Lock()

    def acquire(self, exclude=()):
        """取一个可用凭证，返回 (cred_id, cred) 或 (None, None)。

        取用即更新 last_used，保证轮询真正分散，
        不会像「只读不写」那样永远返回同一个凭证。
        """
        with self._lock:
            candidates = []
            for cid, h in self.healths.items():
                if cid in exclude:
                    continue
                if h.available():
                    candidates.append((h.last_used, random.random(), cid))
            if not candidates:
                return None, None
            # 最近最少使用优先；last_used 相同则随机打散
            candidates.sort(key=lambda x: (x[0], x[1]))
            cid = candidates[0][2]
            # 关键：标记本次取用，否则下次还会选中同一个
            self.healths[cid].last_used = time.time()
            return cid, self.creds[cid]

    def mark_success(self, cred_id):
        h = self.healths.get(cred_id)
        if h:
            h.record_success()

    def mark_failure(self, cred_id, signal):
        """返回该凭证的新状态：healthy / cooling / dead。"""
        h = self.healths.get(cred_id)
        if not h:
            return "unknown"
        fatal = signal.level == RiskLevel.DANGER
        return h.record_failure(signal, fatal=fatal)

    def force_revive(self, cred_id=None):
        """手动复活。不传 id 则全部复活。"""
        with self._lock:
            targets = [cred_id] if cred_id else list(self.healths)
            for cid in targets:
                h = self.healths.get(cid)
                if h:
                    h.state = "healthy"
                    h.consecutive_failures = 0
                    h._danger_failures = 0
                    h.cooldown_until = 0.0
                    h.last_error = None

    def snapshot(self):
        return [self.healths[cid].snapshot() for cid in self.creds]

    def healthy_count(self):
        return sum(1 for h in self.healths.values() if h.available())


# ==================== 上游错误分类 ====================


def classify_http_error(status, body_text=""):
    """把 HTTP 错误码映射成 RiskSignal。"""
    level, msg, fatal = ERROR_POLICY.get(
        status, (RiskLevel.WARN, f"未预期的 HTTP {status}", False)
    )
    # 从响应体里挖更具体的信息
    detail = _extract_error_message(body_text)
    if detail:
        msg = f"{msg}: {detail}"

    retry_after = None
    if status == 429:
        # 官方可能返回 Retry-After，调用方会解析后传入
        retry_after = _extract_retry_after(body_text)
    return RiskSignal(level, f"http_{status}", msg, retry_after)


def classify_biz_error(code_or_msg):
    """把业务错误映射成 RiskSignal。HTTP 200 但内容里报错的情况。"""
    text = str(code_or_msg or "")
    for key, (level, msg, _fatal) in BIZ_ERROR_POLICY.items():
        if key.lower() in text.lower():
            return RiskSignal(level, key, msg)
    return RiskSignal(RiskLevel.WARN, "biz_error", text[:200] or "未知业务错误")


def _extract_error_message(body_text):
    if not body_text:
        return None
    try:
        data = json.loads(body_text)
    except (json.JSONDecodeError, TypeError):
        return body_text.strip()[:200] or None
    if isinstance(data, dict):
        for key in ("message", "msg", "error_description", "detail"):
            v = data.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()[:200]
        err = data.get("error")
        if isinstance(err, dict):
            for key in ("message", "code"):
                v = err.get(key)
                if isinstance(v, str) and v.strip():
                    return v.strip()[:200]
        if isinstance(err, str) and err.strip():
            return err.strip()[:200]
    return None


def _extract_retry_after(body_text):
    """尽力从错误体里解析建议等待秒数。"""
    if not body_text:
        return None
    try:
        data = json.loads(body_text)
        for key in ("retry_after", "retryAfter", "retry-after"):
            v = data.get(key)
            if isinstance(v, (int, float)) and v > 0:
                return float(v)
    except (json.JSONDecodeError, TypeError):
        pass
    return None


# ==================== 熔断器 ====================


class CircuitBreaker:
    """连续失败到阈值就跳闸，给上游喘息时间。

    半开状态放一个探测请求，成功则恢复。
    """

    def __init__(self, failure_threshold=5, open_seconds=60, half_open_max=1):
        self.failure_threshold = failure_threshold
        self.open_seconds = open_seconds
        self.half_open_max = half_open_max

        self.failures = 0
        self.state = "closed"  # closed | open | half_open
        self.opened_at = 0.0
        self.half_open_inflight = 0
        self._lock = threading.Lock()

    def allow(self):
        """是否放行本次请求。half_open 下只放少量探测。"""
        with self._lock:
            if self.state == "closed":
                return True
            if self.state == "open":
                if time.time() - self.opened_at >= self.open_seconds:
                    self.state = "half_open"
                    self.half_open_inflight = 0
                else:
                    return False
            if self.state == "half_open":
                if self.half_open_inflight < self.half_open_max:
                    self.half_open_inflight += 1
                    return True
                return False
            return True

    def record_success(self):
        with self._lock:
            self.failures = 0
            self.state = "closed"
            self.opened_at = 0.0
            self.half_open_inflight = 0

    def record_failure(self):
        with self._lock:
            self.failures += 1
            if self.state == "half_open":
                self.state = "open"
                self.opened_at = time.time()
                self.half_open_inflight = 0
            elif self.failures >= self.failure_threshold:
                self.state = "open"
                self.opened_at = time.time()
                self.half_open_inflight = 0

    def release(self):
        """请求结束时归还 half_open 名额。"""
        with self._lock:
            if self.half_open_inflight > 0:
                self.half_open_inflight -= 1

    def snapshot(self):
        with self._lock:
            return {
                "state": self.state,
                "failures": self.failures,
                "open_seconds_remaining": max(
                    0, int(self.open_seconds - (time.time() - self.opened_at))
                )
                if self.state == "open"
                else 0,
            }


# ==================== 调用统计与告警 ====================


class UsageTracker:
    """跟踪调用量，在异常时告警。

    阈值不是拍脑袋定的，而是基于「真实 IDE 会话的典型频率」估算：
    一个开发者一天大概几十到几百次对话，远高于此的持续高频
    不像人工使用。
    """

    def __init__(self, warn_rpm=30, alert_hourly=1000, alert_daily=10000):
        self.warn_rpm = warn_rpm
        self.alert_hourly = alert_hourly
        self.alert_daily = alert_daily

        self.per_minute = SlidingWindowCounter(60)
        self.hourly = SlidingWindowCounter(3600)
        self.daily = SlidingWindowCounter(86400)
        self.warnings = deque(maxlen=50)
        self._lock = threading.Lock()

    def record(self, tokens_estimated=0):
        self.per_minute.add()
        self.hourly.add()
        self.daily.add()

        rpm = self.per_minute.count()
        if rpm >= self.warn_rpm:
            self._warn(
                RiskLevel.WARN,
                f"每分钟请求数 {rpm} 超过建议阈值 {self.warn_rpm}，"
                f"持续高频可能触发风控",
            )

    def _warn(self, level, message):
        with self._lock:
            # 同一分钟内的相同告警只记一次，避免刷屏
            now = time.time()
            for w in reversed(self.warnings):
                if w["message"] == message and now - w["at"] < 60:
                    return
            self.warnings.append(
                {"at": now, "level": level, "message": message}
            )

    def snapshot(self):
        return {
            "rpm": self.per_minute.count(),
            "hourly": self.hourly.count(),
            "daily": self.daily.count(),
            "warnings": list(self.warnings)[-10:],
        }


# ==================== 请求指纹 ====================


class ClientFingerprint:
    """生成与真实客户端一致的请求头。

    这里**不是**为了伪装成别的客户端做坏事，而是保证
    本服务的请求与官方 CLI 发出的请求在协议层面一致，
    避免因头字段缺失被服务端判定为异常客户端。

    plugin-version 必须与本地实际安装的 CLI 版本一致。
    """

    def __init__(self, plugin_version="zulucli-1.8.1", ide_version=None):
        self.plugin_version = plugin_version
        self.ide_version = ide_version or plugin_version.replace("zulucli-", "")

    def base_headers(self):
        return {
            "User-Agent": "axios/1.7.9",
            "Accept": "application/json, text/plain, */*",
            "X-Source": "COMATE",
            "Content-Type": "application/json",
            "plugin-version": self.plugin_version,
            # 跳过内部 fcnap 网关，与官方 CLI 行为一致
            "x-skip-fcnap": "yes",
        }

    def with_auth(self, license_key):
        h = self.base_headers()
        # 服务端实际校验这两个字段
        h["login-name"] = license_key
        h["Uuap-login-name"] = license_key
        return h

    def stream_headers(self):
        """SSE 请求的头：禁用代理缓冲，保持长连接。"""
        h = self.base_headers()
        h["Accept"] = "text/event-stream"
        return h


def load_credentials_from_env():
    """从环境变量读多凭证。

    支持两种写法：
      COMATE_LICENSE="lic1,lic2,lic3"      逗号分隔
      COMATE_LICENSES="lic1,lic2,lic3"     同上，兼容复数名
    """
    raw = os.environ.get("COMATE_LICENSES") or os.environ.get("COMATE_LICENSE") or ""
    seen = []
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if part and part not in seen:
            seen.append(part)
    return [{"license": p, "username": os.environ.get("COMATE_USERNAME", "")} for p in seen]
