#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一线牵测试数据准备脚本"""
import requests
import json
import time
import random

import local_config as _cfg
APP_ID = _cfg.APP_ID
APP_SECRET = _cfg.APP_SECRET
BASE_TOKEN = _cfg.BASE_TOKEN
USER_TABLE_ID = 'tblsecbZZv0thaPe'
SIGNUP_TABLE_ID = 'tblNVJCnohVaWf8t'

ACTIVITY_ID = 'A-0005'

_token_cache = {'token': None, 'expire_time': 0}

def get_token():
    now = time.time()
    if _token_cache['token'] and _token_cache['expire_time'] > now + 60:
        return _token_cache['token']
    url = 'https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal'
    resp = requests.post(url, json={'app_id': APP_ID, 'app_secret': APP_SECRET}, timeout=10)
    result = resp.json()
    if result.get('code') == 0:
        _token_cache['token'] = result['tenant_access_token']
        _token_cache['expire_time'] = now + result.get('expire', 7200)
        return result['tenant_access_token']
    return None

def create_user(fields):
    token = get_token()
    if not token:
        return None
    url = f'https://open.feishu.cn/open-apis/bitable/v1/apps/{BASE_TOKEN}/tables/{USER_TABLE_ID}/records'
    headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
    data = {'fields': fields}
    try:
        resp = requests.post(url, headers=headers, json=data, timeout=15)
        result = resp.json()
        if result.get('code') == 0:
            return result.get('data', {}).get('record', {}).get('record_id')
        else:
            print(f'  创建用户失败: {result.get("msg", "")}')
            return None
    except Exception as e:
        print(f'  创建用户异常: {e}')
        return None

def create_signup(open_id, nickname):
    token = get_token()
    if not token:
        return None
    url = f'https://open.feishu.cn/open-apis/bitable/v1/apps/{BASE_TOKEN}/tables/{SIGNUP_TABLE_ID}/records'
    headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
    now_ms = int(time.time() * 1000)
    fields = {
        '活动ID': ACTIVITY_ID,
        '报名人open_id': open_id,
        '报名人昵称': nickname,
        '状态': '已报名',
        '报名时间': now_ms,
    }
    data = {'fields': fields}
    try:
        resp = requests.post(url, headers=headers, json=data, timeout=15)
        result = resp.json()
        if result.get('code') == 0:
            return result.get('data', {}).get('record', {}).get('record_id')
        else:
            print(f'  报名失败: {result.get("msg", "")}')
            return None
    except Exception as e:
        print(f'  报名异常: {e}')
        return None

def main():
    print('='*60)
    print('开始创建测试数据')
    print('='*60)

    # 已有的用户（U-0012男、U-0013女）
    existing_users = [
        {'uid': 'U-0012', 'gender': '男', 'oid': 'ou_xxx8', 'nickname': '左易灭杀的昵称'},
        {'uid': 'U-0013', 'gender': '女', 'oid': 'ou_xxx9', 'nickname': '塑料姐妹花的昵称'},
    ]

    # 生成198个新用户（99男 + 99女）
    new_users = []
    next_num = 14  # 从U-0014开始

    # 99个男用户
    for i in range(99):
        uid = f'U-{next_num:04d}'
        next_num += 1
        new_users.append({
            'uid': uid,
            'gender': '男',
            'oid': f'ou_test_male_{i+1:03d}',
            'nickname': f'测试男{i+1:03d}昵称',
            'name': f'测试男{i+1:03d}',
        })

    # 99个女用户
    for i in range(99):
        uid = f'U-{next_num:04d}'
        next_num += 1
        new_users.append({
            'uid': uid,
            'gender': '女',
            'oid': f'ou_test_female_{i+1:03d}',
            'nickname': f'测试女{i+1:03d}昵称',
            'name': f'测试女{i+1:03d}',
        })

    print(f'已有用户: {len(existing_users)}个 (U-0012男, U-0013女)')
    print(f'新创建用户: {len(new_users)}个 (99男+99女)')
    print(f'总参与人数: {len(existing_users) + len(new_users)}人')

    # 创建新用户
    print('\n--- 创建新用户 ---')
    created_count = 0
    for u in new_users:
        now_ms = int(time.time() * 1000)
        fields = {
            '用户ID': u['uid'],
            '昵称': u['nickname'],
            '姓名': u['name'],
            '性别': u['gender'],
            '飞书用户ID': u['oid'],
            '账号状态': '活跃',
            '注册时间': now_ms,
            '爱心剩余': '30',
            '年龄': random.randint(22, 35),
            '学历': random.choice(['本科', '硕士', '博士']),
            '现居/工作城市': '深圳',
            '经常去的教堂': '深圳南头堂',
            '职位': random.choice(['工程师', '教师', '医生', '设计师', '公务员', '金融分析师']),
            '身高（cm）': str(random.randint(160, 185)),
            '你结过婚吗？': '没结过婚',
            '您替子女注册吗？': '不是，我为自己报名',
        }
        rid = create_user(fields)
        if rid:
            created_count += 1
            u['record_id'] = rid
        if created_count > 0 and created_count % 20 == 0:
            print(f'  已创建 {created_count}/{len(new_users)} 个用户')
        time.sleep(0.05)

    print(f'成功创建用户: {created_count}/{len(new_users)}')

    # 让200人报名A-0005
    print('\n--- 报名A-0005活动 ---')
    all_participants = existing_users + new_users
    signup_count = 0
    for u in all_participants:
        rid = create_signup(u['oid'], u['nickname'])
        if rid:
            signup_count += 1
        if signup_count > 0 and signup_count % 20 == 0:
            print(f'  已报名 {signup_count}/{len(all_participants)} 人')
        time.sleep(0.05)

    print(f'成功报名: {signup_count}/{len(all_participants)}')

    # 统计
    males = sum(1 for u in all_participants if u['gender'] == '男')
    females = sum(1 for u in all_participants if u['gender'] == '女')
    print(f'\n=== 汇总 ===')
    print(f'总人数: {len(all_participants)}')
    print(f'男: {males}, 女: {females}')
    print('测试数据准备完成！')

if __name__ == '__main__':
    main()
