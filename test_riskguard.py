#!/usr/bin/env python3
"""
风控模块自测。不依赖网络，纯粹验证状态机与算法正确性。

跑法：python test_riskguard.py
"""

import sys
import time

import riskguard
from riskguard import (
    CircuitBreaker,
    CredentialHealth,
    CredentialPool,
    RiskLevel,
    RiskSignal,
    SlidingWindowCounter,
    TokenBucket,
    classify_biz_error,
    classify_http_error,
    load_credentials_from_env,
)


passed = 0
failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        print(f"  [PASS] {name}")
        passed += 1
    else:
        print(f"  [FAIL] {name} {detail}")
        failed += 1


print("== 令牌桶 ==")
b = TokenBucket(rate=10, capacity=2)
check("初始令牌等于容量", b.tokens == 2.0, f"got {b.tokens}")
check("前两次立即通过", b.consume() and b.consume())
check("第三次非阻塞失败", b.consume(timeout=0) is False)

b2 = TokenBucket(rate=5, capacity=1)
check("满桶时立即通过", b2.consume(timeout=0) is True)
t0 = time.monotonic()
ok = b2.consume(timeout=1.0)
elapsed = time.monotonic() - t0
check("空桶等待后通过", ok is True)
check("等待时长接近补给间隔", 0.1 < elapsed < 0.6, f"elapsed={elapsed:.3f}")

b3 = TokenBucket(rate=2, capacity=1)
b3.consume(timeout=0)
t0 = time.monotonic()
ok3 = b3.consume(timeout=0.1)
el3 = time.monotonic() - t0
# rate=2/s 时补一个令牌要 0.5s，0.1s 预算必然放弃
check("超出超时预算则放弃", ok3 is False, f"ok={ok3}")
check("放弃时机不晚于预算太多", el3 < 0.3, f"elapsed={el3:.3f}")

print()
print("== 滑动窗口 ==")
w = SlidingWindowCounter(window_seconds=1)
for _ in range(5):
    w.add()
check("窗口内计数正确", w.count() == 5, f"got {w.count()}")
time.sleep(1.1)
check("窗口过期后归零", w.count() == 0, f"got {w.count()}")

print()
print("== 凭证健康度 ==")
h = CredentialHealth("c1", cooldown_seconds=1, max_failures=3)
check("初始可用", h.available() is True)
check("初始状态 healthy", h.state == "healthy")

h.record_failure(
    RiskSignal(RiskLevel.WARN, "http_500", "上游异常"), fatal=False
)
check("普通失败不冷却", h.state == "healthy", f"got {h.state}")

h.record_failure(
    RiskSignal(RiskLevel.DANGER, "http_401", "鉴权失败"), fatal=True
)
check("危险失败进入冷却", h.state == "cooling", f"got {h.state}")
h2c = CredentialHealth("c-cool", cooldown_seconds=1, max_failures=3)
h2c.record_failure(RiskSignal(RiskLevel.DANGER, "http_401", "x"), fatal=True)
check("冷却中不可用", h2c.available() is False)
check("冷却中状态为 cooling", h2c.state == "cooling", f"got {h2c.state}")
time.sleep(1.6)
avail = h2c.available()
check("冷却到期后恢复可用", avail is True, f"got {avail}")
check("恢复后状态归位", h2c.state == "healthy", f"got {h2c.state}")

h2 = CredentialHealth("c2", cooldown_seconds=1, max_failures=2)
h2.record_failure(RiskSignal(RiskLevel.DANGER, "http_401", "x"), fatal=True)
h2.record_failure(RiskSignal(RiskLevel.DANGER, "http_401", "x"), fatal=True)
check("连续失败达到阈值后死亡", h2.state == "dead", f"got {h2.state}")
check("死亡状态永久不可用", h2.available() is False)

print()
print("== 凭证池轮询 ==")
pool = CredentialPool(
    [
        {"license": "lic-a", "username": "u"},
        {"license": "lic-b", "username": "u"},
    ],
    cooldown_seconds=1,
)
ids = []
for _ in range(4):
    cid, _cred = pool.acquire()
    ids.append(cid)
check("两个凭证都被取到过", len(set(ids)) == 2, f"got {set(ids)}")
check("健康计数为 2", pool.healthy_count() == 2)

pool.mark_failure("lic-a", RiskSignal(RiskLevel.DANGER, "http_403", "拒绝"))
check("单个凭证冷却后健康数减一", pool.healthy_count() == 1, f"got {pool.healthy_count()}")
cid, _ = pool.acquire()
check("冷却后只取到另一个", cid == "lic-b", f"got {cid}")

pool.mark_failure("lic-b", RiskSignal(RiskLevel.DANGER, "http_403", "拒绝"))
check("全部冷却后取不到", pool.acquire()[0] is None)
pool.force_revive()
check("手动复活后全部可用", pool.healthy_count() == 2)

print()
print("== 熔断器 ==")
cb = CircuitBreaker(failure_threshold=3, open_seconds=1)
check("初始放行", cb.allow() is True)
for _ in range(3):
    cb.record_failure()
check("达到阈值后跳闸", cb.state == "open", f"got {cb.state}")
check("跳闸后拒绝请求", cb.allow() is False)
time.sleep(1.1)
check("超时后进入半开", cb.allow() is True)
check("半开状态记录正确", cb.state == "half_open", f"got {cb.state}")
cb.record_success()
check("探测成功后关闭", cb.state == "closed", f"got {cb.state}")
check("关闭后恢复放行", cb.allow() is True)

print()
print("== 错误分类 ==")
sig = classify_http_error(401, '{"message":"token expired"}')
check("401 判为危险", sig.level == RiskLevel.DANGER, f"got {sig.level}")
check("401 消息含详情", "token expired" in sig.message, f"got {sig.message}")

sig = classify_http_error(503, "")
check("503 判为警告", sig.level == RiskLevel.WARN, f"got {sig.level}")

sig = classify_http_error(429, "")
check("429 判为危险", sig.level == RiskLevel.DANGER)

sig = classify_biz_error("zulu.service.model.unavailable")
check("模型不可用判为警告", sig.level == RiskLevel.WARN, f"got {sig.level}")

sig = classify_biz_error("ResourceUsageOverlimitError")
check("额度超限判为危险", sig.level == RiskLevel.DANGER, f"got {sig.level}")

print()
print("== 环境变量解析 ==")
import os

os.environ["COMATE_LICENSE"] = "lic1,lic2, lic3 ;lic1"
creds = load_credentials_from_env()
check("解析出 3 个唯一凭证", len(creds) == 3, f"got {len(creds)}")

print()
print("=" * 40)
print(f"通过 {passed} 项，失败 {failed} 项")
sys.exit(0 if failed == 0 else 1)