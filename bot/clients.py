# -*- coding: utf-8 -*-
"""bot 进程内的共享客户端单例与 API 别名（飞书消息 + 多维表格 + 字段解析）。"""
from constants import APP_ID, APP_SECRET, BASE_TOKEN
from lib.bitable_client import (
    BitableClient, get_attachment_tokens, get_datetime_value, get_date_value,
    get_field_number, get_field_text, get_multi_select_value, get_phone_value,
    get_select_value,
)
from lib.feishu import FeishuClient
from lib.util import log

feishu = FeishuClient(APP_ID, APP_SECRET, logger=log)
bitable = BitableClient(APP_ID, APP_SECRET, BASE_TOKEN, logger=log)

get_tenant_access_token = feishu.get_tenant_access_token
search_records = bitable.search_records
update_record = bitable.update_record
create_record = bitable.create_record
delete_record = bitable.delete_record
batch_create_records = bitable.batch_create_records
batch_delete_records = bitable.batch_delete_records
field_exists = bitable.field_exists


def is_test_fake_openid(open_id):
    """判断是否为测试用假 open_id（ou_fake_*）。这些不是真实飞书用户，无法收到消息，
    直接跳过以免拖慢/阻塞向全体参与者的群发通知。"""
    return isinstance(open_id, str) and open_id.startswith("ou_fake_")


def send_text_message(receive_id, text):
    # 假测试账号不发起真实发送，避免 99992351 报错刷屏并阻塞后续消息处理
    if is_test_fake_openid(receive_id):
        return False
    return feishu.send_text_message(receive_id, text)


def send_card_message(receive_id, card_content):
    """发送交互卡片消息"""
    return feishu.send_card_message(receive_id, card_content)


def send_user_card(receive_id, share_open_id):
    """发送个人名片消息"""
    return feishu.send_user_card(receive_id, share_open_id)
