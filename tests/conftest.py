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
