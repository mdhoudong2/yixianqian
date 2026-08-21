#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""批量添加用户表缺失的字段"""

import requests
import json
import time

import local_config as _cfg
APP_ID = _cfg.APP_ID
APP_SECRET = _cfg.APP_SECRET
BASE_TOKEN = _cfg.BASE_TOKEN
TABLE_ID = "tblsecbZZv0thaPe"

def get_token():
    url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    resp = requests.post(url, json={"app_id": APP_ID, "app_secret": APP_SECRET})
    return resp.json()["tenant_access_token"]

def create_field(token, field_name, field_type, property=None):
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{BASE_TOKEN}/tables/{TABLE_ID}/fields"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    data = {
        "field_name": field_name,
        "type": field_type
    }
    if property:
        data["property"] = property
    
    resp = requests.post(url, headers=headers, json=data)
    result = resp.json()
    if result.get("code") == 0:
        print(f"✅ 已添加字段: {field_name} (type={field_type})")
        return True
    else:
        print(f"❌ 添加字段失败: {field_name} - {result.get('msg')}")
        return False

def main():
    token = get_token()
    print(f"Token: {token[:10]}...")
    
    # 需要添加的字段列表
    # type: 1=文本, 2=数字, 3=单选, 5=日期, 13=电话号码, 17=附件, 22=地理位置
    
    fields_to_add = [
        # 文本字段 (type=1)
        {"name": "姓名", "type": 1},
        {"name": "圣名", "type": 1},
        {"name": "经常去的教堂", "type": 1},
        {"name": "身份证号", "type": 1},
        {"name": "家庭成员情况", "type": 1},
        {"name": "从事行业", "type": 1},
        {"name": "职位", "type": 1},
        {"name": "房产状况", "type": 1},
        {"name": "是否拥有驾照和汽车", "type": 1},
        {"name": "性格", "type": 1},
        {"name": "关于自己", "type": 1},
        {"name": "择偶标准", "type": 1},
        {"name": "你是怎么知道这个App的？", "type": 1},
        
        # 数字字段 (type=2)
        {"name": "身高（cm）", "type": 2, "property": {"formatter": "0"}},
        
        # 电话号码字段 (type=13)
        {"name": "手机号", "type": 13},
        
        # 日期字段 (type=5)
        {"name": "生日", "type": 5, "property": {"date_formatter": "yyyy/MM/dd", "auto_fill": False}},
        
        # 地理位置字段 (type=22)
        {"name": "教堂所在城市", "type": 22},
        {"name": "籍贯", "type": 22},
        {"name": "现居/工作城市", "type": 22},
        
        # 单选字段 (type=3) - 需要先创建，后续再添加选项
        {"name": "你结过婚吗？", "type": 3},
        {"name": "年收入", "type": 3},
        {"name": "婚后是否与父母同住", "type": 3},
        {"name": "您替子女注册吗？", "type": 3},
        
        # 附件字段 (type=17)
        {"name": "个人照片", "type": 17},
    ]
    
    success_count = 0
    fail_count = 0
    
    for field in fields_to_add:
        prop = field.get("property")
        if create_field(token, field["name"], field["type"], prop):
            success_count += 1
        else:
            fail_count += 1
        time.sleep(0.5)  # 避免请求过快
    
    print(f"\n完成！成功: {success_count}, 失败: {fail_count}")

if __name__ == "__main__":
    main()
