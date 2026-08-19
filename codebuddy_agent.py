#!/usr/bin/env python3
"""
一线牵助手 - CodeBuddy Agent 飞书机器人后端
替代Dify，使用CodeBuddy CLI处理消息
"""
import os
import sys
import json
import time
import threading
import subprocess
import logging
import requests
import lark_oapi as lark
from lark_oapi.api.im.v1 import *

# ============ 配置 ============
import local_config as _cfg
APP_ID = _cfg.ASSISTANT_APP_ID
APP_SECRET = _cfg.ASSISTANT_APP_SECRET
CODEBUDDY_PATH = "/usr/bin/codebuddy"
WORK_DIR = "/opt/yixianqian-h5"
TOOLS_DIR = "/opt/yixianqian/tools"
CONFIG_PATH = "/opt/yixianqian/codebuddy_config.json"
ADMIN_OPEN_IDS = _cfg.ADMIN_OPEN_IDS
MAX_RESULT_LENGTH = 3000
CODEBUDDY_TIMEOUT = 300

# ============ 日志 ============
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('/tmp/codebuddy_agent.log')
    ]
)
logger = logging.getLogger(__name__)

# ============ 消息去重 ============
_processed_msgs = set()
_MAX_PROCESSED = 1000

# ============ 飞书API ============
_token_cache = {"token": "", "expires": 0}

def get_tenant_access_token():
    if _token_cache["token"] and time.time() < _token_cache["expires"] - 60:
        return _token_cache["token"]
    try:
        resp = requests.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": APP_ID, "app_secret": APP_SECRET},
            timeout=10
        )
        data = resp.json()
        if data.get("code") == 0:
            _token_cache["token"] = data["tenant_access_token"]
            _token_cache["expires"] = time.time() + data.get("expire", 7200)
            return _token_cache["token"]
    except Exception as e:
        logger.error(f"获取token失败: {e}")
    return ""

def send_text_message(receive_id, text):
    token = get_tenant_access_token()
    if not token:
        logger.error("无token，无法发送消息")
        return False
    try:
        resp = requests.post(
            "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={
                "receive_id": receive_id,
                "msg_type": "text",
                "content": json.dumps({"text": text}, ensure_ascii=False)
            },
            timeout=10
        )
        data = resp.json()
        if data.get("code") != 0:
            logger.error(f"发送消息失败: {data}")
        return data.get("code") == 0
    except Exception as e:
        logger.error(f"发送消息异常: {e}")
        return False

def is_admin(open_id):
    return open_id in ADMIN_OPEN_IDS

# ============ 图片下载 ============
def download_image(message_id, file_key):
    """下载飞书消息中的图片"""
    token = get_tenant_access_token()
    if not token:
        return None
    try:
        resp = requests.get(
            f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/resources/{file_key}?type=image",
            headers={"Authorization": f"Bearer {token}"},
            timeout=30
        )
        if resp.status_code == 200:
            img_path = f"/tmp/cb_img_{message_id}_{int(time.time())}.jpg"
            with open(img_path, "wb") as f:
                f.write(resp.content)
            return img_path
    except Exception as e:
        logger.error(f"下载图片失败: {e}")
    return None

# ============ CodeBuddy执行 ============
def run_codebuddy(task, image_path=None):
    """执行CodeBuddy命令，返回结果"""
    prompt = task
    if image_path:
        prompt = f"[用户发送了一张图片，路径: {image_path}]\n{task}"

    try:
        result = subprocess.run(
            [CODEBUDDY_PATH, "--print", "--dangerously-skip-permissions", prompt],
            cwd=WORK_DIR,
            capture_output=True,
            text=True,
            timeout=CODEBUDDY_TIMEOUT,
        )
        output = (result.stdout or "").strip()
        if result.returncode != 0:
            err = (result.stderr or "").strip()
            if err:
                return f"执行出错：\n{err[:1000]}"
            return f"执行失败（返回码 {result.returncode}）"
        return output if output else "执行完成，无输出"
    except subprocess.TimeoutExpired:
        return "执行超时（超过5分钟），请拆分任务重试"
    except Exception as e:
        return f"执行异常：{e}"

def process_message_async(sender_id, text, image_path=None):
    """后台线程处理消息"""
    def _run():
        logger.info(f"CodeBuddy开始执行: {text[:100]}")
        result = run_codebuddy(text, image_path)

        # 清理临时图片
        if image_path and os.path.exists(image_path):
            try:
                os.remove(image_path)
            except:
                pass

        # 分段发送结果
        if len(result) <= MAX_RESULT_LENGTH:
            send_text_message(sender_id, result)
        else:
            chunks = [result[i:i+MAX_RESULT_LENGTH] for i in range(0, len(result), MAX_RESULT_LENGTH)]
            for idx, chunk in enumerate(chunks, 1):
                prefix = f"【结果 {idx}/{len(chunks)}】\n" if len(chunks) > 1 else ""
                send_text_message(sender_id, prefix + chunk)
                time.sleep(0.5)

        logger.info(f"CodeBuddy执行完成，结果长度: {len(result)}")

    t = threading.Thread(target=_run, daemon=True, name="codebuddy-agent")
    t.start()

# ============ 消息处理 ============
def handle_text_message(sender_id, text):
    """处理文本消息"""
    if not text:
        send_text_message(sender_id, "请发送文字消息描述你的需求")
        return

    # 立即回复
    send_text_message(sender_id, f"🤖 正在处理：{text[:50]}{'...' if len(text) > 50 else ''}\n请稍候")

    # 后台执行
    process_message_async(sender_id, text)

def handle_image_message(sender_id, message_id, content_dict):
    """处理图片消息"""
    image_key = content_dict.get("image_key", "")
    if not image_key:
        send_text_message(sender_id, "无法获取图片，请重新发送")
        return

    send_text_message(sender_id, "🤖 正在识别图片并处理，请稍候")

    # 下载图片
    img_path = download_image(message_id, image_key)
    if not img_path:
        send_text_message(sender_id, "图片下载失败，请重新发送")
        return

    # 默认让CodeBuddy分析图片
    task = "请分析这张图片的内容并描述"
    process_message_async(sender_id, task, image_path=img_path)

# ============ 飞书事件回调 ============
def do_p2_im_message_receive_v1(data: P2ImMessageReceiveV1) -> None:
    try:
        msg = data.event.message
        sender = data.event.sender

        message_id = msg.message_id
        chat_type = msg.chat_type
        sender_type = sender.sender_type
        message_type = msg.message_type
        content = msg.content or "{}"

        # 消息去重
        if message_id in _processed_msgs:
            return
        _processed_msgs.add(message_id)
        if len(_processed_msgs) > _MAX_PROCESSED:
            _processed_msgs.clear()

        # 只处理私聊
        if chat_type != "p2p":
            return
        if sender_type != "user":
            return

        sender_id = sender.sender_id.open_id
        logger.info(f"收到消息: sender={sender_id}, type={message_type}")

        if message_type == "text":
            content_dict = json.loads(content)
            text = content_dict.get("text", "").strip()
            handle_text_message(sender_id, text)
        elif message_type == "image":
            content_dict = json.loads(content)
            handle_image_message(sender_id, message_id, content_dict)
        else:
            send_text_message(sender_id, "目前支持文字和图片消息，请发送文字描述你的需求")

    except Exception as e:
        logger.error(f"处理消息异常: {e}", exc_info=True)

# ============ 启动 ============
def main():
    logger.info("=" * 50)
    logger.info("一线牵助手 CodeBuddy Agent 启动中...")
    logger.info(f"APP_ID: {APP_ID}")
    logger.info(f"工作目录: {WORK_DIR}")
    logger.info(f"管理员: {ADMIN_OPEN_IDS}")

    # 验证CodeBuddy可用
    try:
        result = subprocess.run(
            [CODEBUDDY_PATH, "--version"],
            capture_output=True, text=True, timeout=10
        )
        logger.info(f"CodeBuddy版本: {result.stdout.strip()}")
    except Exception as e:
        logger.error(f"CodeBuddy不可用: {e}")
        sys.exit(1)

    # 注册事件处理器
    event_handler = lark.EventDispatcherHandler.builder("", "") \
        .register_p2_im_message_receive_v1(do_p2_im_message_receive_v1) \
        .build()

    # 创建WebSocket客户端
    client = lark.ws.Client(
        APP_ID,
        APP_SECRET,
        event_handler=event_handler,
        log_level=lark.LogLevel.INFO
    )

    logger.info("WebSocket连接中...")
    client.start()

if __name__ == "__main__":
    main()
