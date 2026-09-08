"""飞书开放平台客户端：tenant token 缓存与消息发送（bot 与 H5 后端共用）。

原 bot 与 web/backend 各写了一份 send_text_message / send_card_message /
send_user_card，此处统一实现，消除重复。
"""
import json
import threading
import time

import requests

API_BASE = "https://open.feishu.cn/open-apis"

# 「永久性不可达、重试无意义」的发送错误码：
# 230013 = Bot has NO availability to this user（用户未添加/已移除机器人，或跨租户对其不可见）
PERMANENT_SEND_CODES = {230013}
# 命中永久不可达后的本地退避（秒）：首次 30 分钟，连续失败翻倍，封顶 2 小时；
# 退避窗口内对该用户的发送直接短路（不发请求、不刷错误日志），到期放一次真实探测，成功即恢复。
_UNREACH_BACKOFF = [1800, 3600, 7200]


class FeishuClient:
    def __init__(self, app_id, app_secret, logger=None, timeout=15):
        self.app_id = app_id
        self.app_secret = app_secret
        self.timeout = timeout
        self._logger = logger or (lambda msg: None)
        self._token_cache = {"token": None, "expire_time": 0}
        # receive_id -> {"next": 下次允许真实发送的时间戳, "fails": 连续不可达次数}
        self._unreachable = {}
        self._unreach_lock = threading.Lock()

    def log(self, msg):
        try:
            self._logger(msg)
        except Exception:
            pass

    def get_tenant_access_token(self):
        now = time.time()
        if self._token_cache["token"] and self._token_cache["expire_time"] > now + 60:
            return self._token_cache["token"]
        url = API_BASE + "/auth/v3/tenant_access_token/internal"
        data = {"app_id": self.app_id, "app_secret": self.app_secret}
        for attempt in range(3):
            try:
                resp = requests.post(url, json=data, timeout=15)
                result = resp.json()
                if result.get("code") == 0:
                    self._token_cache["token"] = result["tenant_access_token"]
                    self._token_cache["expire_time"] = now + result.get("expire", 7200)
                    return self._token_cache["token"]
                self.log(f"获取token失败(第{attempt + 1}次): {result}")
            except Exception as e:
                self.log(f"获取token异常(第{attempt + 1}次): {e}")
            if attempt < 2:
                time.sleep(2 ** attempt)
        return None

    def _mark_unreachable(self, receive_id, code):
        """登记一个永久不可达用户：按连续失败次数递增退避，避免对其无限重试刷屏。"""
        with self._unreach_lock:
            old = self._unreachable.get(receive_id)
            fails = (old["fails"] if old else 0) + 1
            delay = _UNREACH_BACKOFF[min(fails - 1, len(_UNREACH_BACKOFF) - 1)]
            self._unreachable[receive_id] = {"next": time.time() + delay, "fails": fails}
        self.log(f"用户暂不可达(code={code})，{delay // 60}分钟内不再重试 …{str(receive_id)[-6:]}（连续第{fails}次）")

    def _send(self, receive_id, msg_type, content_obj):
        # 退避窗口内的永久不可达用户：直接短路，不发请求、不刷失败日志（到期再真实探测一次）
        now = time.time()
        with self._unreach_lock:
            info = self._unreachable.get(receive_id)
            if info and now < info["next"]:
                return False
        token = self.get_tenant_access_token()
        if not token:
            return False
        url = API_BASE + "/im/v1/messages?receive_id_type=open_id"
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        data = {"receive_id": receive_id, "msg_type": msg_type,
                "content": json.dumps(content_obj, ensure_ascii=False)}
        for attempt in range(3):
            try:
                resp = requests.post(url, headers=headers, json=data, timeout=self.timeout)
                result = resp.json()
                code = result.get("code")
                if code == 0:
                    # 发送成功说明已恢复可达，清除退避标记
                    with self._unreach_lock:
                        self._unreachable.pop(receive_id, None)
                    return result.get("data", {}).get("message_id", True)
                if code in PERMANENT_SEND_CODES:
                    # 永久不可达：重试无意义，登记退避后立即返回（不再×3、不再每30秒刷）
                    self._mark_unreachable(receive_id, code)
                    return False
                self.log(f"发送{msg_type}消息失败(第{attempt + 1}次): {result}")
            except Exception as e:
                self.log(f"发送{msg_type}消息异常(第{attempt + 1}次): {e}")
            if attempt < 2:
                time.sleep(2 ** attempt)
        return False

    def send_text_message(self, receive_id, text):
        return self._send(receive_id, "text", {"text": text})

    def send_card_message(self, receive_id, card_content):
        return self._send(receive_id, "interactive", card_content)

    def send_user_card(self, receive_id, share_open_id):
        return self._send(receive_id, "share_user", {"user_id": share_open_id})

    def download_media(self, file_token):
        """下载云空间/多维表格附件（drive medias），成功返回字节，失败返回 None。"""
        token = self.get_tenant_access_token()
        if not token or not file_token:
            return None
        url = f"{API_BASE}/drive/v1/medias/{file_token}/download"
        for attempt in range(3):
            try:
                resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=self.timeout)
                if resp.status_code == 200 and resp.content:
                    return resp.content
                self.log(f"下载附件失败(第{attempt + 1}次): HTTP {resp.status_code}")
            except Exception as e:
                self.log(f"下载附件异常(第{attempt + 1}次): {e}")
            if attempt < 2:
                time.sleep(2 ** attempt)
        return None

    def upload_message_image(self, image_bytes, filename="poster.jpg"):
        """上传消息图片换取 img_key（互动卡片 image 元素需要 img_key，不支持外链 URL）。
        成功返回 img_key 字符串，失败返回 None。"""
        token = self.get_tenant_access_token()
        if not token or not image_bytes:
            return None
        url = f"{API_BASE}/im/v1/images"
        headers = {"Authorization": f"Bearer {token}"}
        form = {"image_type": (None, "message")}
        files = {"image": (filename, image_bytes, "image/jpeg")}
        for attempt in range(3):
            try:
                resp = requests.post(url, headers=headers, data=form, files=files, timeout=self.timeout)
                result = resp.json()
                if result.get("code") == 0:
                    return result.get("data", {}).get("image_key")
                self.log(f"上传消息图片失败(第{attempt + 1}次): {result}")
            except Exception as e:
                self.log(f"上传消息图片异常(第{attempt + 1}次): {e}")
            if attempt < 2:
                time.sleep(2 ** attempt)
        return None
