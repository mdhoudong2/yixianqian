#!/usr/bin/env python3
#!/usr/bin/env python3
import os, sys
_D = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _D)
sys.path.insert(0, os.path.dirname(_D))
from _prod_guard import guard
guard(os.path.basename(__file__))
"""模拟管理员指令触发的后端流程（安全版）。

- 不以真实飞书消息形式触发（避免真正的 WebSocket/消息收发），而是直接导入
  yixianqian_bot_ws 模块，调用与 do_p2_im_message_receive_v1 相同的后台异步执行器路径：
      _run_admin_task_async(admin_oid, 任务名, 对应 handler, keyword)
- 导入不会启动第二个 WebSocket 客户端（连接只发生在 main() 里，受 __main__ 守卫）。
- admin_oid 用 ou_fake_admin_sim，走 is_test_fake_openid 守卫，不会真的向飞书发消息，
  但能走通 后台线程执行器 → handler 执行 → send_text_message 回复 的完整链路，
  观察 Bot 是否卡顿、日志是否正常。

用法：
    ./venv/bin/python simulate_admin_cmd.py            # 只模拟「开始填志愿 A-0005 4 4」
    ./venv/bin/python simulate_admin_cmd.py start       # 同上显式指定
    ./venv/bin/python simulate_admin_cmd.py start stop  # 完整流程演示（会真实改写 A-0005 状态，慎用）
"""
import sys
import time
import importlib

import yixianqian_bot_ws as bot

ADMIN_FAKE_OID = "ou_fake_admin_sim"


def run_async(task_name, handler, keyword):
    """与 do_p2_im_message_receive_v1 相同的后台执行器触发方式。"""
    log(f"[simulate] 提交后台任务: {task_name} keyword={keyword!r}")
    ret = bot._run_admin_task_async(ADMIN_FAKE_OID, task_name, handler, keyword)
    log(f"[simulate] _run_admin_task_async 返回（应立即/非阻塞）: {ret!r}")


def main():
    steps = [a for a in sys.argv[1:]] or ["start"]
    log(f"[simulate] 模拟开始，admin={ADMIN_FAKE_OID}，步骤={steps}")

    # 关键观察点：后台任务执行器是否让主线程立即返回（非阻塞, 秒回）
    for step in steps:
        if step == "start":
            t0 = time.time()
            run_async("开始填志愿", bot.handle_admin_start_group, "A-0005 4 4")
            log(f"[simulate] 开始填志愿 dispatch 耗时 {time.time()-t0:.2f}s（应极短，说明未阻塞）")
        elif step == "stop":
            t0 = time.time()
            run_async("执行分组", bot.handle_admin_stop_group, "A-0005")
            log(f"[simulate] 执行分组 dispatch 耗时 {time.time()-t0:.2f}s")
        else:
            log(f"[simulate] 未知步骤: {step}")

    # 等后台线程跑完，把日志聚齐
    log("[simulate] 等待后台线程完成（最多 120s）...")
    bot.ADMIN_TASK_EXECUTOR.shutdown(wait=True)
    log("[simulate] 后台线程已全部结束，模拟完成。")


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


if __name__ == "__main__":
    main()
