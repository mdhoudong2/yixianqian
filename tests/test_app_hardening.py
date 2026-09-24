"""H5 后端资源防护：卡片顺序缓存有上限、删图后缓存副本一起清。

2026-09-24 审查：`_card_order_cache` 的 key 含客户端任意筛选参数组合且从不淘汰，
单账号可造无限条目把单 worker 内存撑爆；删照片只清人脸 sidecar，磁盘图片缓存
仍被白名单的「已缓存放行」放行，旧 URL 在缓存存活期内继续可读。
"""
import os
import sys

_WEB_BACKEND = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web", "backend"
)
if _WEB_BACKEND not in sys.path:
    sys.path.insert(0, _WEB_BACKEND)

import app  # noqa: E402


def test_card_order_cache_is_bounded():
    app._card_order_cache.clear()
    try:
        for i in range(app._SESSION_ORDER_MAX + 50):
            app._card_order_cache[(f"ou_{i}", "f")] = {"order": [], "created": i}
        with app._card_order_lock:
            app._trim_card_order_cache_locked()
        assert len(app._card_order_cache) <= app._SESSION_ORDER_MAX
        # 保留的是较新的条目（旧的一半被淘汰）
        assert ("ou_0", "f") not in app._card_order_cache
    finally:
        app._card_order_cache.clear()


def test_purge_image_cache_removes_token_files(monkeypatch, tmp_path):
    monkeypatch.setattr(app, "IMAGE_CACHE_DIR", str(tmp_path))
    (tmp_path / "tokA.jpg").write_bytes(b"a")
    (tmp_path / "tokA.png").write_bytes(b"b")
    (tmp_path / "tokB.jpg").write_bytes(b"c")
    app._purge_image_cache("tokA")
    assert not (tmp_path / "tokA.jpg").exists()
    assert not (tmp_path / "tokA.png").exists()
    assert (tmp_path / "tokB.jpg").exists()
