"""FeishuClient 发送退避：永久不可达(230013)不重试且窗口内短路；瞬时错误仍重试；成功清除标记。"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from lib import feishu as feishu_mod  # noqa: E402


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def _make_client(responses):
    """responses: list of payloads returned by successive posts; records call count."""
    client = feishu_mod.FeishuClient("a", "b")
    client.get_tenant_access_token = lambda: "tok"
    calls = {"n": 0}

    def fake_post(*a, **k):
        i = min(calls["n"], len(responses) - 1)
        calls["n"] += 1
        return _FakeResp(responses[i])

    feishu_mod.requests.post = fake_post
    feishu_mod.time.sleep = lambda *a, **k: None  # 去掉重试退避等待
    return client, calls


def test_permanent_error_no_retry_and_short_circuit():
    client, calls = _make_client([{"code": 230013, "msg": "no availability"}])
    rid = "ou_permanent_1"
    # 第一次：真实请求一次，返回 False，且不做 3 次重试
    assert client.send_text_message(rid, "hi") is False
    assert calls["n"] == 1
    # 退避窗口内连续再发 4 次：全部本地短路，不再产生任何 HTTP 请求
    for _ in range(4):
        assert client.send_text_message(rid, "hi") is False
    assert calls["n"] == 1
    assert rid in client._unreachable


def test_transient_error_still_retries_three_times():
    client, calls = _make_client([{"code": 230101, "msg": "temporarily unavailable"}])
    rid = "ou_transient_1"
    assert client.send_text_message(rid, "hi") is False
    assert calls["n"] == 3  # 瞬时错误保留 3 次重试
    # 瞬时错误不进退避表：下一次发送会立即再次真实请求
    client.send_text_message(rid, "hi")
    assert calls["n"] == 6
    assert rid not in client._unreachable


def test_success_clears_unreachable():
    client, calls = _make_client([
        {"code": 230013},                       # 首次永久不可达
        {"code": 0, "data": {"message_id": "m"}},  # 退避到期后探测成功
    ])
    rid = "ou_recover_1"
    assert client.send_text_message(rid, "hi") is False
    assert calls["n"] == 1
    # 模拟退避窗口到期
    client._unreachable[rid]["next"] = 0
    assert bool(client.send_text_message(rid, "hi")) is True
    assert rid not in client._unreachable  # 成功后清除标记
    # 已清除：再发会立即真实请求（不被短路）
    client.send_text_message(rid, "hi")
    assert calls["n"] == 3
