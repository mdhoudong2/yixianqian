"""轻量工具：统一的日志输出（bot 与 H5 后端共用）。"""
import time


def log(msg):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}")
