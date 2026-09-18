"""新用户自动报名的缴费门禁：只有「我已缴费」才报名线下活动。

缴费判断在 auto_signup_new_user 的最前面、任何网络调用之前，所以这些用例
不需要 mock 多维表格就能跑。
"""
from auto_tasks import auto_signup_new_user
from constants import (
    FIELD_WECHAT_PAYMENT,
    WECHAT_PAY_APP_ONLY,
    WECHAT_PAY_PAID,
)


def test_field_and_option_literals_match_bitable():
    """唯一参与判断的那两个字面量，必须与表单上的字段名/选项文案逐字一致。

    它们一旦被改动（比如有人"顺手"把引号换成英文、或改了措辞），缴费判断会静默失效
    ——所有人都不会被自动报名，且不报错。这个用例负责盯住。

    「只注册App」那个选项不参与比较（非「我已缴费」一律跳过），且生产与测试两个 base
    的文案本来就不一致，所以只要求它别和「我已缴费」撞车。
    """
    assert FIELD_WECHAT_PAYMENT == "微信缴费"
    assert WECHAT_PAY_PAID == "我已缴费"
    assert WECHAT_PAY_APP_ONLY and WECHAT_PAY_APP_ONLY != WECHAT_PAY_PAID


def test_not_paid_user_is_skipped():
    """只注册 App：不报名。"""
    assert auto_signup_new_user("ou_test", "张三", WECHAT_PAY_APP_ONLY) is False


def test_empty_or_unknown_pay_status_is_skipped():
    """字段为空 / 后台新增了没见过的选项 -> fail-closed，不报名。

    宁可漏报名（可人工补），也不要把人塞进线下活动名单让他自己去取消。
    """
    assert auto_signup_new_user("ou_test", "张三", "") is False
    assert auto_signup_new_user("ou_test", "张三", None) is False
    assert auto_signup_new_user("ou_test", "张三", "还没交钱") is False
    assert auto_signup_new_user("ou_test", "张三", "我已缴费 ") is False  # 尾随空格不算
