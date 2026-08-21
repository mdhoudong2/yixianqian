# -*- coding: utf-8 -*-
"""开发脚本生产保护：默认禁止在生产环境执行，需显式设置 YX_DEV_ALLOW=1。"""
import os
import sys


def guard(script_name=""):
    if os.environ.get("YIXIANQIAN_ENV", "prod") == "prod" and os.environ.get("YX_DEV_ALLOW") != "1":
        name = script_name or "此脚本"
        print(f"⛔ {name} 是开发/数据修复脚本，默认禁止在生产执行。")
        print("   如确需在生产运行，请显式设置环境变量：YX_DEV_ALLOW=1")
        sys.exit(1)
