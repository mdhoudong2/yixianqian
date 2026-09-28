"""测试环境引导：CI 无 bot/local_config.py（gitignore），注入桩配置后即可导入 bot/ 模块。"""
import os
import sys
import types

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BOT_DIR = os.path.join(_ROOT, "bot")
for _p in (_ROOT, _BOT_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

if "local_config" not in sys.modules:
    _stub = types.ModuleType("local_config")
    _stub.FEISHU_APP_ID = "cli_test"
    _stub.FEISHU_APP_SECRET = "test_secret"
    _stub.ADMIN_OPEN_IDS = ["ou_test_admin"]
    _stub.BASE_TOKEN = "basetoken_test"
    sys.modules["local_config"] = _stub


@pytest.fixture
def points_db(tmp_path):
    """一个全新的麦穗库（临时文件，跑完即弃）。

    两处「必须清」的地方，漏了都会跨用例串味：
    - `close_all()`：连接是线程本地的，不关会留着上个库的句柄；
    - `points_config.invalidate()`：配置缓存是模块级的、按时间而非按库失效，
      上个用例改过的分值会在这个用例里冒充「库里的覆盖值」。
    """
    from lib import points_config
    from lib import points_db as mod

    mod.set_db_path(str(tmp_path / "yixianqian_points.db"))
    mod.migrate()
    points_config.invalidate()
    yield mod
    mod.close_all()
    points_config.invalidate()


@pytest.fixture
def clock(points_db, monkeypatch):
    """可控时钟：把所有「现在」钉住，测试自己往前拨。

    只有 `points_db.now_dt` 这一个缝——`now_str()` 和 `points_invite` 里算确认期
    到期时刻的地方都从它派生。要验「7 天确认期的边界」这类事，靠 sleep 是不行的
    （跑 7 天），靠手写一个未来的 confirm_due_at 又绕过了被测代码。
    """
    from datetime import timedelta

    from lib import quota

    state = {"t": quota.now().replace(microsecond=0)}

    def advance(days=0, hours=0, seconds=0):
        state["t"] += timedelta(days=days, hours=hours, seconds=seconds)
        return state["t"]

    monkeypatch.setattr(points_db, "now_dt", lambda: state["t"])
    advance.__doc__ = "把时钟往前拨，返回拨完的时刻。"
    return advance
