"""主菜单卡片（卡片1）构建与发送。"""
from clients import send_card_message


def generate_h5_url(open_id):
    """生成 H5 入口链接。身份由飞书「网页免登」确定，URL 不再携带登录 token（防止链接被转发冒用身份）"""
    return "https://app.nantou.love/"



def build_main_menu_card(h5_url=None):
    """构建主菜单卡片（卡片1：一线牵 App + 邀请好友 + 帮助）"""
    app_button = {
        "tag": "button",
        "text": {"tag": "plain_text", "content": "一线牵 App"},
        "type": "primary",
    }
    if h5_url:
        app_button["url"] = h5_url
    else:
        app_button["value"] = {"action": "menu_h5"}

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "欢迎使用一线牵\U0001f495"},
            "template": "red"
        },
        "elements": [
            {
                "tag": "action",
                "actions": [
                    app_button,
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "邀请好友"},
                        "type": "default",
                        "value": {"action": "menu_invite"}
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "帮助"},
                        "type": "default",
                        "value": {"action": "menu_help"}
                    }
                ]
            }
        ]
    }




def send_main_menu_card(open_id):
    """发送卡片1（主菜单卡片），「一线牵 App」按钮直接跳转H5"""
    return send_card_message(open_id, build_main_menu_card(generate_h5_url(open_id)))




WELCOME_TEXT = (
    "欢迎欢迎\U0001f44f！\n\n"
    "给机器人发送下列指令：\n\n"
    "【一线牵】\n"
    "获取一线牵App链接（牵线、消息、活动、我的）；\n\n"
    "【邀请】\n"
    "获取专属邀请链接，邀请好友得爱心；\n\n"
    "【注册】\n"
    "获取注册表单链接；\n\n"
    "【状态】\n"
    "查看注册审核进度；\n\n"
    "【帮助】\n"
    "查看所有指令；\n\n"
    "祝你早日找到另一半！\U0001f495"
)
