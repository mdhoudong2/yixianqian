# -*- coding: utf-8 -*-
"""飞书开放平台客户端：tenant token 缓存与消息发送（bot 与 H5 后端共用）。

原 bot 与 web/backend 各写了一份 send_text_message / send_card_message /
send_user_card，此处统一实现，消除重复。
"""
import json
import time

import requests

API_BASE = "https://open.feishu.cn/open-apis"


class FeishuClient:
    def __init__(self, app_id, app_secret, logger=None, timeout=15):
        self.app_id = app_id
        self.app_secret = app_secret
        self.timeout = timeout
        self._logger = logger or (lambda msg: None)
        self._token_cache = {"token": None, "expire_time": 0}

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

    def _send(self, receive_id, msg_type, content_obj):
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
                if result.get("code") == 0:
                    return result.get("data", {}).get("message_id", True)
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
