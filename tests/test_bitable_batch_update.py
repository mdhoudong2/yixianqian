"""lib/bitable_client.py batch_update_records 单测（不联网）。

真版本只在活服务 E2E 用过（A-0011 批量签到 17 人），此前 tests/ 只有 FakeBitable
的假版本。这里用假 Session.post 钉死请求形状、分批边界，以及「部分失败如实少算、
不得报成功」的语义——调用方（web/backend/app.py 批量签到）按 `len(pending) - updated`
报 failed，多算会谎报成功。
"""
import requests

from lib import bitable_client as bc

BASE = "basetoken_test"
TABLE = "tblFakeSignup"
URL = f"{bc.API_BASE}/bitable/v1/apps/{BASE}/tables/{TABLE}/records/batch_update"


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def _client(monkeypatch):
    client = bc.BitableClient("app", "secret", BASE)
    monkeypatch.setattr(client, "get_token", lambda: "tok_test")
    monkeypatch.setattr(bc, "_throttle", lambda: None)  # 测试不睡令牌桶
    return client


def _install_post(monkeypatch, replies):
    """replies: 依次弹出的响应体 dict 或要抛的 Exception；返回 calls 记录。"""
    calls = []

    def fake_post(url, **kwargs):
        calls.append({"url": url, **kwargs})
        r = replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return _Resp(r)

    monkeypatch.setattr(bc._http, "post", fake_post)
    return calls


def _recs(n):
    return [{"record_id": f"rec{i}", "fields": {"现场签到": "按时"}} for i in range(n)]


def _ok(n_records):
    return {"code": 0, "data": {"records": [{"record_id": f"r{i}"} for i in range(n_records)]}}


def test_request_shape(monkeypatch):
    calls = _install_post(monkeypatch, [_ok(2)])
    updated = _client(monkeypatch).batch_update_records(TABLE, _recs(2))

    assert updated == 2
    assert len(calls) == 1
    call = calls[0]
    assert call["url"] == URL  # base/table/路径拼接
    assert call["json"] == {"records": _recs(2)}  # payload 结构：records 原样带
    assert call["headers"]["Authorization"] == "Bearer tok_test"
    assert call["timeout"] == 30


def test_batches_501_into_two(monkeypatch):
    calls = _install_post(monkeypatch, [_ok(500), _ok(1)])
    updated = _client(monkeypatch).batch_update_records(TABLE, _recs(501))

    assert updated == 501
    assert len(calls) == 2
    assert [len(c["json"]["records"]) for c in calls] == [500, 1]
    first_ids = {r["record_id"] for r in calls[0]["json"]["records"]}
    second_ids = {r["record_id"] for r in calls[1]["json"]["records"]}
    assert not first_ids & second_ids  # 第二批不重发第一批


def test_partial_success_undercounts(monkeypatch):
    """飞书回 code=0 但 records 少于本批：只认实际返回条数，绝不满打满算。"""
    calls = _install_post(monkeypatch, [{"code": 0, "data": {"records": [{"record_id": "rec0"}]}}])
    updated = _client(monkeypatch).batch_update_records(TABLE, _recs(2))

    assert len(calls) == 1
    assert updated == 1


def test_failed_batch_does_not_report_success(monkeypatch):
    """第二批 code≠0：整批不计入；调用方按 len(pending)-updated 得到 failed>0。"""
    calls = _install_post(monkeypatch, [_ok(2), {"code": 1254000, "msg": "invalid request"}])
    updated = _client(monkeypatch).batch_update_records(TABLE, _recs(3), batch_size=2)

    assert updated == 2
    assert len(calls) == 2
    assert [len(c["json"]["records"]) for c in calls] == [2, 1]


def test_exception_undercounts_and_does_not_raise(monkeypatch):
    calls = _install_post(monkeypatch, [requests.exceptions.ConnectionError("boom"), _ok(1)])
    updated = _client(monkeypatch).batch_update_records(TABLE, _recs(2), batch_size=1)

    assert updated == 1
    assert len(calls) == 2


def test_missing_data_records_falls_back_to_chunk_size(monkeypatch):
    """code=0 但响应没有 data.records（现网实测形态）：按本批条数计。"""
    calls = _install_post(monkeypatch, [{"code": 0}])
    updated = _client(monkeypatch).batch_update_records(TABLE, _recs(2))

    assert updated == 2
    assert len(calls) == 1


def test_incomplete_rows_filtered_and_empty_short_circuit(monkeypatch):
    calls = _install_post(monkeypatch, [_ok(1)])
    client = _client(monkeypatch)
    records = [None, {"fields": {"x": 1}}, {"record_id": "r1"},
               {"record_id": "r2", "fields": {}}, {"record_id": "r3", "fields": None},
               {"record_id": "rec_ok", "fields": {"x": 1}}]
    assert client.batch_update_records(TABLE, records) == 1
    assert len(calls) == 1
    assert calls[0]["json"]["records"] == [{"record_id": "rec_ok", "fields": {"x": 1}}]

    # 全残行 / 空输入：不发请求直接回 0
    assert client.batch_update_records(TABLE, [None, {"record_id": "r"}]) == 0
    assert client.batch_update_records(TABLE, []) == 0
    assert len(calls) == 1
