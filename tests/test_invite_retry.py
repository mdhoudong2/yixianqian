"""邀请奖励补扫的重试节流：解析不到的邀请人ID 不能每 30 秒重查一遍。

生产实测：被邀请人填「没有/不知道」这类垃圾值时，补扫每 45 秒扫一轮并刷日志
（2026-09-24 审查，2000+ 行/天）。这里锁三件事：窗口内跳过、窗口过重试、
用户之间互不影响。
"""
import time

import store
from store import invite_retry_due


def test_retry_is_throttled_and_expires(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "INVITE_RETRY_FILE", str(tmp_path / "retry.json"))
    now = time.time()
    assert invite_retry_due("ou_a", now=now) is True
    # 窗口内的重复调用不再尝试，也不刷新时间戳
    assert invite_retry_due("ou_a", now=now + 60) is False
    assert invite_retry_due("ou_a", now=now + store.INVITE_RETRY_INTERVAL - 1) is False
    # 窗口过去后放行（管理员事后把邀请人ID 改对，最多等这一个窗口）
    assert invite_retry_due("ou_a", now=now + store.INVITE_RETRY_INTERVAL + 1) is True


def test_different_users_do_not_share_the_window(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "INVITE_RETRY_FILE", str(tmp_path / "retry.json"))
    now = time.time()
    assert invite_retry_due("ou_a", now=now) is True
    assert invite_retry_due("ou_b", now=now) is True
    assert invite_retry_due("ou_a", now=now + 1) is False
    assert invite_retry_due("ou_b", now=now + 1) is False
