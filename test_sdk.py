#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""测试飞书SDK多维表格API"""
import lark_oapi as lark
from lark_oapi.api.bitable.v1 import *

import local_config as _cfg
APP_ID = _cfg.APP_ID
APP_SECRET = _cfg.APP_SECRET
BASE_TOKEN = _cfg.BASE_TOKEN
USER_TABLE_ID = "tblsecbZZv0thaPe"

print("=== 测试1: 创建客户端 ===")
client = lark.Client.builder() \
    .app_id(APP_ID) \
    .app_secret(APP_SECRET) \
    .log_level(lark.LogLevel.DEBUG) \
    .build()
print("客户端创建成功")

print("\n=== 测试2: 搜索记录 ===")
try:
    request = SearchAppTableRecordRequest.builder() \
        .app_token(BASE_TOKEN) \
        .table_id(USER_TABLE_ID) \
        .request_body(SearchAppTableRecordRequestBody.builder()
            .filter(FilterInfo.builder()
                .conjunction("and")
                .conditions([Condition.builder()
                    .field_name("昵称")
                    .operator("is")
                    .value(["测试用户"])
                    .build()])
                .build())
            .build()) \
        .build()
    response = client.bitable.v1.app_table_record.search(request)
    print(f"success: {response.success()}")
    print(f"code: {response.code}")
    print(f"msg: {response.msg}")
    if response.data and response.data.items:
        for item in response.data.items:
            print(f"  record_id: {item.record_id}")
            print(f"  fields: {item.fields}")
except Exception as e:
    print(f"异常: {type(e).__name__}: {e}")
    import traceback
    traceback.print_exc()

print("\n=== 测试3: 更新记录 ===")
try:
    request = UpdateAppTableRecordRequest.builder() \
        .app_token(BASE_TOKEN) \
        .table_id(USER_TABLE_ID) \
        .record_id("recvrZRECSpMNO") \
        .request_body(UpdateAppTableRecordRequestBody.builder()
            .fields({"飞书用户ID": "sdk_test_123"})
            .build()) \
        .build()
    response = client.bitable.v1.app_table_record.update(request)
    print(f"success: {response.success()}")
    print(f"code: {response.code}")
    print(f"msg: {response.msg}")
except Exception as e:
    print(f"异常: {type(e).__name__}: {e}")
    import traceback
    traceback.print_exc()
