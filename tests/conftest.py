"""测试环境引导：CI 无 bot/local_config.py（gitignore），注入桩配置后即可导入 bot/ 模块。"""
import os
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BOT_DIR = os.path.join(_ROOT, "bot")
if _BOT_DIR not in sys.path:
    sys.path.insert(0, _BOT_DIR)

if "local_config" not in sys.modules:
    _stub = types.ModuleType("local_config")
    _stub.FEISHU_APP_ID = "cli_test"
    _stub.FEISHU_APP_SECRET = "test_secret"
    _stub.ADMIN_OPEN_IDS = ["ou_test_admin"]
    _stub.BASE_TOKEN = "basetoken_test"
    sys.modules["local_config"] = _stub
