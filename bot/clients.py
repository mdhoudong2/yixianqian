"""bot 进程内的共享客户端单例与 API 别名（飞书消息 + 多维表格 + 字段解析）。"""
from constants import (
    APP_ID,
    APP_SECRET,
    BASE_TOKEN,
    FIELD_LIKE_CREATED_AT,
    FIELD_LIKE_MONTH,
    FIELD_LIKE_STATUS,
    FIELD_LIKE_TYPE,
)
from store import load_p2p_chats

from lib import quota
from lib.bitable_client import (  # noqa: F401 — re-export 供各模块 from clients import * 使用
    BitableClient,
    get_attachment_tokens,
    get_date_value,
    get_datetime_value,
    get_field_number,
    get_field_text,
    get_multi_select_value,
    get_phone_value,
    get_select_value,
)
from lib.feishu import FeishuClient
from lib.util import log

feishu = FeishuClient(APP_ID, APP_SECRET, logger=log)
bitable = BitableClient(APP_ID, APP_SECRET, BASE_TOKEN, logger=log)

get_tenant_access_token = feishu.get_tenant_access_token

def search_records(*args, **kwargs):
    """失败返回 None 时对外转为 []，保持旧调用方兼容。"""
    result = bitable.search_records(*args, **kwargs)
    return result if result is not None else []

update_record = bitable.update_record
create_record = bitable.create_record
delete_record = bitable.delete_record
batch_create_records = bitable.batch_create_records
batch_delete_records = bitable.batch_delete_records
field_exists = bitable.field_exists
field_is_number = bitable.field_is_number


def like_month(fields):
    """喜欢的归属月份：优先显式字段（受理时刻钉死），回退创建时间的 %Y-%m。

    绝不能只用创建时间——那记的是 spool 落库那一刻，可能比用户点击晚几秒到
    几分钟，23:59 点的喜欢会算进下个月、白耗上个月的额度。
    """
    m = get_field_text(fields, FIELD_LIKE_MONTH)
    if m:
        return m
    created = get_datetime_value(fields, FIELD_LIKE_CREATED_AT)
    return created[:7] if created else ""


def like_is_active(fields):
    """这条喜欢现在还算数吗？口径唯一来源 lib.quota.is_like_active。

    全站读喜欢状态的地方都该走这里，不要各写各的 `!= "被驳回"`：
    否定式会把「以后新增的任何状态」都当成有效，静默多算额度、多算配对。
    """
    return quota.is_like_active(
        get_select_value(fields, FIELD_LIKE_STATUS),
        get_field_text(fields, FIELD_LIKE_TYPE) or quota.LIKE_TYPE_ANON,
        like_month(fields))


def is_test_fake_openid(open_id):
    """判断是否为测试用假 open_id（ou_fake_*）。这些不是真实飞书用户，无法收到消息，
    直接跳过以免拖慢/阻塞向全体参与者的群发通知。"""
    return isinstance(open_id, str) and open_id.startswith("ou_fake_")


def _cached_p2p_chat_id(receive_id, receive_id_type):
    """主动推送走open_id通道、且本地缓存了该用户p2p单聊chat_id时返回chat_id，否则None。
    用于规避飞书对部分单聊open_id直发的230101；入站回复已直接用事件里的chat_id，不走这里。"""
    if receive_id_type == "open_id" and isinstance(receive_id, str) and receive_id.startswith("ou_"):
        return load_p2p_chats().get(receive_id)
    return None


def send_text_message(receive_id, text, receive_id_type="open_id"):
    # 假测试账号不发起真实发送，避免 99992351 报错刷屏并阻塞后续消息处理
    if is_test_fake_openid(receive_id):
        return False
    chat_id = _cached_p2p_chat_id(receive_id, receive_id_type)
    if chat_id:
        if feishu.send_text_message(chat_id, text, receive_id_type="chat_id"):
            return True
        log(f"主动推送chat_id失败，回退open_id: {receive_id}")
    return feishu.send_text_message(receive_id, text, receive_id_type)


def send_card_message(receive_id, card_content, receive_id_type="open_id"):
    """发送交互卡片消息"""
    if is_test_fake_openid(receive_id):
        return False
    chat_id = _cached_p2p_chat_id(receive_id, receive_id_type)
    if chat_id:
        if feishu.send_card_message(chat_id, card_content, receive_id_type="chat_id"):
            return True
        log(f"主动推送chat_id失败，回退open_id: {receive_id}")
    return feishu.send_card_message(receive_id, card_content, receive_id_type)


def send_user_card(receive_id, share_open_id, receive_id_type="open_id"):
    """发送个人名片消息"""
    if is_test_fake_openid(receive_id):
        return False
    chat_id = _cached_p2p_chat_id(receive_id, receive_id_type)
    if chat_id:
        if feishu.send_user_card(chat_id, share_open_id, receive_id_type="chat_id"):
            return True
        log(f"主动推送chat_id失败，回退open_id: {receive_id}")
    return feishu.send_user_card(receive_id, share_open_id, receive_id_type)
