"""飞书单选框 operator=is 的 value 只能一个值（1254018 回归）。

v7 重构把「状态白名单」塞进 `operator:"is"` 的 value（两个选项），飞书直接判
InvalidFilter；而查询失败会被转成空表，于是活动报名通知静默不发、重复喜欢与
spool 幂等静默失效（见 2026-09-24 审查）。本测试锁两件事：

1. 源码里不许再出现 `operator:"is"` 配多值 value（AST 扫描 bot/web/lib）；
2. `_spool_process` 查重：查询失败（None）必须不建记录，不能和空表混为一谈。
"""
import ast
import os
import sys

_WEB_BACKEND = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web", "backend"
)
if _WEB_BACKEND not in sys.path:
    sys.path.insert(0, _WEB_BACKEND)

import app  # noqa: E402
from config import F_LIKE_MONTH, F_LIKE_STATUS, F_LIKE_TARGET_OPENID  # noqa: E402

from lib import quota  # noqa: E402

ME = "ou_me"
TARGET = "ou_t1"


def _iter_source_files():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    skip = {"__pycache__", "venv", ".venv", "site-packages", "node_modules"}
    for sub in ("bot", "web", "lib"):
        for dirpath, dirnames, files in os.walk(os.path.join(root, sub)):
            dirnames[:] = [d for d in dirnames if d not in skip]
            for name in files:
                if name.endswith(".py"):
                    yield os.path.join(dirpath, name)


def _multi_value_is_filters(tree):
    """返回 [(lineno, 说明)]：operator=is 却给了不止一个值的过滤条件。"""
    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        items = {}
        for k, v in zip(node.keys, node.values):
            if isinstance(k, ast.Constant) and isinstance(k.value, str):
                items[k.value] = v
        op = items.get("operator")
        if not (isinstance(op, ast.Constant) and op.value == "is"):
            continue
        val = items.get("value")
        if isinstance(val, ast.Call) and getattr(val.func, "id", None) == "list":
            bad.append((node.lineno, "operator=is 的 value 是 list(...)，可能多值"))
        elif isinstance(val, ast.List) and len(val.elts) > 1:
            bad.append((node.lineno, f"operator=is 的 value 有 {len(val.elts)} 个值"))
    return bad


def test_no_multi_value_is_filters():
    bad = []
    for path in _iter_source_files():
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename=path)
        for lineno, why in _multi_value_is_filters(tree):
            bad.append(f"{os.path.relpath(path)}:{lineno} {why}")
    assert not bad, "飞书单选框 operator=is 只能带一个值：\n" + "\n".join(bad)


def _active_like_record():
    return {"record_id": "rec_exists", "fields": {
        F_LIKE_STATUS: "单向喜欢",
        F_LIKE_MONTH: [{"text": quota.month_key(), "type": "text"}],
    }}


def _like_op():
    return {"type": "like", "temp_key": "tk1", "initiator_oid": ME,
            "target_oid": TARGET, "fields": {"x": 1}}


def test_spool_process_dedupe_filter_is_single_value(monkeypatch):
    seen = {"created": 0}

    def fake_raw(table_id, filter_conditions=None, **kw):
        seen["filter"] = filter_conditions
        return [_active_like_record()]

    monkeypatch.setattr(app.bitable, "raw_search_records", fake_raw)
    monkeypatch.setattr(app.bitable, "create_record",
                        lambda *a, **k: seen.__setitem__("created", seen["created"] + 1) or
                        {"record_id": "new"})
    assert app._spool_process(_like_op()) is True
    assert seen["created"] == 0
    for cond in seen["filter"]["conditions"]:
        assert len(cond["value"]) == 1


def test_spool_process_does_not_create_when_dedupe_query_fails(monkeypatch):
    created = []
    monkeypatch.setattr(app.bitable, "raw_search_records", lambda *a, **k: None)
    monkeypatch.setattr(app.bitable, "create_record",
                        lambda *a, **k: (created.append(1), {"record_id": "new"})[1])
    assert app._spool_process(_like_op()) is False
    assert created == []


def test_spool_process_allows_new_like_after_rejection(monkeypatch):
    created = []
    rejected = {"record_id": "rec_old", "fields": {F_LIKE_STATUS: "被驳回"}}
    monkeypatch.setattr(app.bitable, "raw_search_records", lambda *a, **k: [rejected])
    monkeypatch.setattr(app.bitable, "create_record",
                        lambda *a, **k: (created.append(1), {"record_id": "new"})[1])
    assert app._spool_process(_like_op()) is True
    assert created == [1]
