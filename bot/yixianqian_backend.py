#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
一线牵相亲平台 - 后端API服务（简化版）
功能：
1. 公开数据接口（用户列表、用户详情、活动列表）
2. 用户操作接口（通过header X-User-Nickname 识别身份）
3. 点爱心、报名活动、举报等
"""

import time
import requests
from typing import Optional, List
from fastapi import FastAPI, HTTPException, Header, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ==================== 配置 ====================
import local_config as _cfg
APP_ID = _cfg.APP_ID
APP_SECRET = _cfg.APP_SECRET
BASE_TOKEN = _cfg.BASE_TOKEN

# 表ID
USER_TABLE_ID = "tblsecbZZv0thaPe"
LIKE_TABLE_ID = "tblaciMZHRQH7QBA"
ACTIVITY_TABLE_ID = "tblHLltReY8xHTfu"
SIGNUP_TABLE_ID = "tbls1f5yFBLZt6Af"
MATCH_TABLE_ID = "tbl8eu9Y85tQZCu7"
REPORT_TABLE_ID = "tblDj4PMHitAmo4T"

# ==================== 数据模型 ====================
class LikeRequest(BaseModel):
    target_nickname: str
    message: str = ""

class CancelLikeRequest(BaseModel):
    target_nickname: str

class SignupRequest(BaseModel):
    activity_name: str

class CancelSignupRequest(BaseModel):
    activity_name: str

class ReportRequest(BaseModel):
    reported_nickname: str
    reason: str

# ==================== Token缓存 ====================
_token_cache = {"token": None, "expire_time": 0}

def get_tenant_access_token():
    now = time.time()
    if _token_cache["token"] and _token_cache["expire_time"] > now + 60:
        return _token_cache["token"]
    url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    data = {"app_id": APP_ID, "app_secret": APP_SECRET}
    resp = requests.post(url, json=data, timeout=10)
    result = resp.json()
    if result.get("code") == 0:
        _token_cache["token"] = result["tenant_access_token"]
        _token_cache["expire_time"] = now + result.get("expire", 7200)
        return _token_cache["token"]
    return None

# ==================== 多维表格工具函数 ====================
def search_records(table_id, filter_conditions=None, page_size=100):
    token = get_tenant_access_token()
    if not token:
        return []
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{BASE_TOKEN}/tables/{table_id}/records/search"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    data = {"page_size": page_size}
    if filter_conditions:
        data["filter"] = filter_conditions
    try:
        resp = requests.post(url, headers=headers, json=data, timeout=10)
        result = resp.json()
        if result.get("code") == 0:
            return result.get("data", {}).get("items", [])
    except:
        pass
    return []

def get_record(table_id, record_id):
    token = get_tenant_access_token()
    if not token:
        return None
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{BASE_TOKEN}/tables/{table_id}/records/{record_id}"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        result = resp.json()
        if result.get("code") == 0:
            return result.get("data", {}).get("record", {})
    except:
        pass
    return None

def create_record(table_id, fields):
    token = get_tenant_access_token()
    if not token:
        return None
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{BASE_TOKEN}/tables/{table_id}/records"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    data = {"fields": fields}
    try:
        resp = requests.post(url, headers=headers, json=data, timeout=10)
        result = resp.json()
        if result.get("code") == 0:
            return result.get("data", {}).get("record", {})
    except:
        pass
    return None

def update_record(table_id, record_id, fields):
    token = get_tenant_access_token()
    if not token:
        return False
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{BASE_TOKEN}/tables/{table_id}/records/{record_id}"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    data = {"fields": fields}
    try:
        resp = requests.put(url, headers=headers, json=data, timeout=10)
        result = resp.json()
        return result.get("code") == 0
    except:
        pass
    return False

def get_field_text(fields, field_name):
    val = fields.get(field_name, "")
    if isinstance(val, list) and val:
        return val[0].get("text", "")
    return val if isinstance(val, str) else ""

def get_field_select(fields, field_name):
    val = fields.get(field_name, "")
    if isinstance(val, dict):
        return val.get("text", "")
    return val if isinstance(val, str) else ""

def format_user_record(item):
    fields = item.get("fields", {})
    return {
        "record_id": item.get("record_id"),
        "nickname": get_field_text(fields, "昵称"),
        "gender": get_field_select(fields, "性别"),
        "age": fields.get("年龄", 0),
        "education": get_field_select(fields, "学历"),
        "occupation": get_field_text(fields, "职业"),
        "hobbies": fields.get("兴趣爱好", []),
        "intro": get_field_text(fields, "自我介绍"),
        "heart_remain": fields.get("爱心剩余", 30),
        "account_status": get_field_select(fields, "账号状态"),
        "photos": fields.get("照片", []),
    }

def format_activity_record(item):
    fields = item.get("fields", {})
    return {
        "record_id": item.get("record_id"),
        "name": get_field_text(fields, "活动名称"),
        "time": fields.get("活动时间", ""),
        "location": get_field_text(fields, "活动地点"),
        "description": get_field_text(fields, "活动描述"),
        "poster": fields.get("活动海报", []),
        "capacity": fields.get("报名人数上限", 30),
        "current_count": fields.get("当前报名人数", 0),
        "status": get_field_select(fields, "活动状态"),
    }

# ==================== 身份验证 ====================
def get_current_user(request: Request, user: Optional[str] = None):
    """从查询参数获取当前用户昵称"""
    user_nickname = user or request.query_params.get("user")
    if not user_nickname:
        raise HTTPException(status_code=401, detail="未识别用户身份，请通过机器人专属链接进入")
    users = search_records(USER_TABLE_ID, {
        "conjunction": "and",
        "conditions": [{"field_name": "昵称", "operator": "is", "value": [user_nickname]}]
    })
    if not users:
        raise HTTPException(status_code=401, detail="用户不存在，请先注册")
    return {
        "nickname": user_nickname,
        "record_id": users[0].get("record_id"),
        "gender": get_field_select(users[0].get("fields", {}), "性别")
    }

# ==================== FastAPI应用 ====================
app = FastAPI(title="一线牵相亲平台API", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ==================== 公开接口 ====================
@app.get("/api/users")
async def get_users(
    gender: str = None,
    min_age: int = None,
    max_age: int = None,
    education: str = None,
    keyword: str = None,
    page: int = 1,
    page_size: int = 20
):
    """获取活跃用户列表（公开）"""
    conditions = [
        {"field_name": "账号状态", "operator": "is", "value": ["活跃"]}
    ]
    if gender:
        conditions.append({"field_name": "性别", "operator": "is", "value": [gender]})
    if education:
        conditions.append({"field_name": "学历", "operator": "is", "value": [education]})
    if min_age is not None:
        conditions.append({"field_name": "年龄", "operator": "isGreater", "value": [str(min_age - 1)]})
    if max_age is not None:
        conditions.append({"field_name": "年龄", "operator": "isLess", "value": [str(max_age + 1)]})

    records = search_records(USER_TABLE_ID, {"conjunction": "and", "conditions": conditions})

    if keyword:
        records = [r for r in records if keyword in get_field_text(r.get("fields", {}), "昵称") or
                   keyword in get_field_text(r.get("fields", {}), "自我介绍")]

    start = (page - 1) * page_size
    end = start + page_size
    page_records = records[start:end]

    return {
        "total": len(records),
        "page": page,
        "page_size": page_size,
        "list": [format_user_record(r) for r in page_records]
    }

@app.get("/api/users/{nickname}")
async def get_user_detail(nickname: str):
    """获取用户详情（公开）"""
    users = search_records(USER_TABLE_ID, {
        "conjunction": "and",
        "conditions": [{"field_name": "昵称", "operator": "is", "value": [nickname]}]
    })
    if not users:
        raise HTTPException(status_code=404, detail="用户不存在")
    return format_user_record(users[0])

@app.get("/api/activities")
async def get_activities(status: str = None, page: int = 1, page_size: int = 20):
    """获取活动列表（公开）"""
    conditions = []
    if status:
        conditions.append({"field_name": "活动状态", "operator": "is", "value": [status]})

    records = search_records(ACTIVITY_TABLE_ID,
                             {"conjunction": "and", "conditions": conditions} if conditions else None)

    start = (page - 1) * page_size
    end = start + page_size
    page_records = records[start:end]

    return {
        "total": len(records),
        "list": [format_activity_record(r) for r in page_records]
    }

@app.get("/api/activities/{activity_name}")
async def get_activity_detail(activity_name: str):
    """获取活动详情（公开）"""
    records = search_records(ACTIVITY_TABLE_ID, {
        "conjunction": "and",
        "conditions": [{"field_name": "活动名称", "operator": "is", "value": [activity_name]}]
    })
    if not records:
        raise HTTPException(status_code=404, detail="活动不存在")
    activity = format_activity_record(records[0])
    signups = search_records(SIGNUP_TABLE_ID, {
        "conjunction": "and",
        "conditions": [
            {"field_name": "活动名称", "operator": "is", "value": [activity_name]},
            {"field_name": "报名状态", "operator": "is", "value": ["已报名"]}
        ]
    })
    activity["signup_count"] = len(signups)
    return activity

# ==================== 需要身份的接口 ====================
@app.get("/api/me/profile")
async def get_my_profile(user: dict = Depends(get_current_user)):
    record = get_record(USER_TABLE_ID, user["record_id"])
    if not record:
        raise HTTPException(status_code=404, detail="用户不存在")
    return format_user_record(record)

@app.get("/api/likes/i-like")
async def get_my_likes(user: dict = Depends(get_current_user)):
    records = search_records(LIKE_TABLE_ID, {
        "conjunction": "and",
        "conditions": [
            {"field_name": "发起用户昵称", "operator": "is", "value": [user["nickname"]]},
            {"field_name": "状态", "operator": "isNot", "value": ["已取消"]}
        ]
    })
    result = []
    for r in records:
        f = r.get("fields", {})
        result.append({
            "target_nickname": get_field_text(f, "目标用户昵称"),
            "message": get_field_text(f, "附言"),
            "status": get_field_select(f, "状态"),
        })
    return result

@app.get("/api/likes/mutual")
async def get_mutual_likes(user: dict = Depends(get_current_user)):
    records = search_records(LIKE_TABLE_ID, {
        "conjunction": "and",
        "conditions": [
            {"field_name": "发起用户昵称", "operator": "is", "value": [user["nickname"]]},
            {"field_name": "状态", "operator": "is", "value": ["相互喜欢"]}
        ]
    })
    result = []
    for r in records:
        f = r.get("fields", {})
        target_nickname = get_field_text(f, "目标用户昵称")
        target_users = search_records(USER_TABLE_ID, {
            "conjunction": "and",
            "conditions": [{"field_name": "昵称", "operator": "is", "value": [target_nickname]}]
        })
        target_info = format_user_record(target_users[0]) if target_users else {}
        result.append({
            "target_nickname": target_nickname,
            "message": get_field_text(f, "附言"),
            "target_info": target_info
        })
    return result

@app.post("/api/likes")
async def like_user(req: LikeRequest, user: dict = Depends(get_current_user)):
    my_record = get_record(USER_TABLE_ID, user["record_id"])
    if not my_record:
        raise HTTPException(status_code=404, detail="用户不存在")
    hearts = my_record.get("fields", {}).get("爱心剩余", 30)
    if hearts <= 0:
        raise HTTPException(status_code=400, detail="爱心数量不足")

    existing = search_records(LIKE_TABLE_ID, {
        "conjunction": "and",
        "conditions": [
            {"field_name": "发起用户昵称", "operator": "is", "value": [user["nickname"]]},
            {"field_name": "目标用户昵称", "operator": "is", "value": [req.target_nickname]},
            {"field_name": "状态", "operator": "isNot", "value": ["已取消"]}
        ]
    })
    if existing:
        raise HTTPException(status_code=400, detail="你已经喜欢过TA了")

    record = create_record(LIKE_TABLE_ID, {
        "发起用户昵称": user["nickname"],
        "目标用户昵称": req.target_nickname,
        "附言": req.message,
        "状态": "单向喜欢",
        "爱心已扣减": False
    })
    if not record:
        raise HTTPException(status_code=500, detail="操作失败")
    return {"success": True, "message": "喜欢成功，对方会收到匿名通知"}

@app.post("/api/likes/cancel")
async def cancel_like(req: CancelLikeRequest, user: dict = Depends(get_current_user)):
    records = search_records(LIKE_TABLE_ID, {
        "conjunction": "and",
        "conditions": [
            {"field_name": "发起用户昵称", "operator": "is", "value": [user["nickname"]]},
            {"field_name": "目标用户昵称", "operator": "is", "value": [req.target_nickname]},
            {"field_name": "状态", "operator": "isNot", "value": ["已取消"]}
        ]
    })
    if not records:
        raise HTTPException(status_code=404, detail="未找到喜欢记录")
    success = update_record(LIKE_TABLE_ID, records[0].get("record_id"), {"状态": "已取消"})
    if not success:
        raise HTTPException(status_code=500, detail="取消失败")
    return {"success": True}

@app.post("/api/activities/signup")
async def signup_activity(req: SignupRequest, user: dict = Depends(get_current_user)):
    activities = search_records(ACTIVITY_TABLE_ID, {
        "conjunction": "and",
        "conditions": [{"field_name": "活动名称", "operator": "is", "value": [req.activity_name]}]
    })
    if not activities:
        raise HTTPException(status_code=404, detail="活动不存在")
    status = get_field_select(activities[0].get("fields", {}), "活动状态")
    if status != "报名中":
        raise HTTPException(status_code=400, detail=f"活动当前状态：{status}，无法报名")

    existing = search_records(SIGNUP_TABLE_ID, {
        "conjunction": "and",
        "conditions": [
            {"field_name": "活动名称", "operator": "is", "value": [req.activity_name]},
            {"field_name": "用户昵称", "operator": "is", "value": [user["nickname"]]},
            {"field_name": "报名状态", "operator": "is", "value": ["已报名"]}
        ]
    })
    if existing:
        raise HTTPException(status_code=400, detail="你已经报名了该活动")

    record = create_record(SIGNUP_TABLE_ID, {
        "活动名称": req.activity_name,
        "用户昵称": user["nickname"],
        "报名状态": "已报名",
        "已通知喜欢者": False
    })
    if not record:
        raise HTTPException(status_code=500, detail="报名失败")
    return {"success": True, "message": "报名成功"}

@app.post("/api/activities/cancel-signup")
async def cancel_signup(req: CancelSignupRequest, user: dict = Depends(get_current_user)):
    records = search_records(SIGNUP_TABLE_ID, {
        "conjunction": "and",
        "conditions": [
            {"field_name": "活动名称", "operator": "is", "value": [req.activity_name]},
            {"field_name": "用户昵称", "operator": "is", "value": [user["nickname"]]},
            {"field_name": "报名状态", "operator": "is", "value": ["已报名"]}
        ]
    })
    if not records:
        raise HTTPException(status_code=404, detail="未找到报名记录")
    success = update_record(SIGNUP_TABLE_ID, records[0].get("record_id"), {"报名状态": "已取消"})
    if not success:
        raise HTTPException(status_code=500, detail="取消失败")
    return {"success": True}

@app.get("/api/activities/my/signups")
async def get_my_signups(user: dict = Depends(get_current_user)):
    records = search_records(SIGNUP_TABLE_ID, {
        "conjunction": "and",
        "conditions": [
            {"field_name": "用户昵称", "operator": "is", "value": [user["nickname"]]},
            {"field_name": "报名状态", "operator": "is", "value": ["已报名"]}
        ]
    })
    result = []
    for r in records:
        f = r.get("fields", {})
        activity_name = get_field_text(f, "活动名称")
        activities = search_records(ACTIVITY_TABLE_ID, {
            "conjunction": "and",
            "conditions": [{"field_name": "活动名称", "operator": "is", "value": [activity_name]}]
        })
        activity_info = format_activity_record(activities[0]) if activities else {}
        result.append({"activity_name": activity_name, "activity_info": activity_info})
    return result

@app.get("/api/match/recommendations")
async def get_match_recommendations(user: dict = Depends(get_current_user)):
    records = search_records(MATCH_TABLE_ID, {
        "conjunction": "and",
        "conditions": [
            {"field_name": "推荐给用户", "operator": "is", "value": [user["nickname"]]},
            {"field_name": "推荐状态", "operator": "is", "value": ["待查看"]}
        ]
    })
    result = []
    for r in records:
        f = r.get("fields", {})
        target_nickname = get_field_text(f, "被推荐用户")
        target_users = search_records(USER_TABLE_ID, {
            "conjunction": "and",
            "conditions": [{"field_name": "昵称", "operator": "is", "value": [target_nickname]}]
        })
        target_info = format_user_record(target_users[0]) if target_users else {}
        result.append({
            "target_nickname": target_nickname,
            "reason": get_field_text(f, "推荐理由"),
            "target_info": target_info
        })
    return result

@app.post("/api/reports")
async def report_user(req: ReportRequest, user: dict = Depends(get_current_user)):
    record = create_record(REPORT_TABLE_ID, {
        "举报人昵称": user["nickname"],
        "被举报人昵称": req.reported_nickname,
        "举报原因": req.reason,
        "处理状态": "待处理"
    })
    if not record:
        raise HTTPException(status_code=500, detail="举报失败")
    return {"success": True, "message": "举报已提交，管理员会尽快处理"}

@app.post("/api/users/exit-market")
async def exit_market(user: dict = Depends(get_current_user)):
    success = update_record(USER_TABLE_ID, user["record_id"], {"账号状态": "已退出"})
    if not success:
        raise HTTPException(status_code=500, detail="操作失败")
    return {"success": True, "message": "已退出相亲市场"}

@app.post("/api/users/return-market")
async def return_market(user: dict = Depends(get_current_user)):
    success = update_record(USER_TABLE_ID, user["record_id"], {"账号状态": "活跃"})
    if not success:
        raise HTTPException(status_code=500, detail="操作失败")
    return {"success": True, "message": "已回归相亲市场"}

@app.get("/api/debug/headers")
async def debug_headers(request: Request):
    return dict(request.headers)

@app.get("/api/health")
async def health_check():
    return {"status": "ok", "timestamp": time.time()}

if __name__ == "__main__":
    import uvicorn
    print("=" * 60)
    print("一线牵相亲平台 - 后端API服务（简化版）")
    print(f"API文档: http://localhost:8000/docs")
    print("身份验证: header X-User-Nickname")
    print("=" * 60)
    uvicorn.run(app, host="0.0.0.0", port=8000)
