"""飞书多维表格（Bitable）API 封装：DAO + 字段值解析（bot 与 H5 后端共用）。

原 bot 与 web/backend/bitable.py 各有一份 search/update/create/delete 及
get_field_text 等字段解析函数，此处统一实现：

- 过滤条件兼容两种形态：
    - bot 传入完整 filter（含 conjunction: and/or）
    - web 传入条件列表（自动包装为 conjunction=and）
- 字段解析以 web 版（列表全部拼接）为准，bot 场景均为单值字段，行为一致。
"""
import time

import requests

API_BASE = "https://open.feishu.cn/open-apis"


class BitableClient:
    def __init__(self, app_id, app_secret, base_token, logger=None, timeout=15):
        self.app_id = app_id
        self.app_secret = app_secret
        self.base_token = base_token
        self.timeout = timeout
        self._logger = logger or (lambda msg: None)
        self._token_cache = {"token": None, "expire_time": 0}
        self._field_cache = {}

    def log(self, msg):
        try:
            self._logger(msg)
        except Exception:
            pass

    # ---------- token ----------

    def get_token(self):
        now = time.time()
        if self._token_cache["token"] and self._token_cache["expire_time"] > now + 60:
            return self._token_cache["token"]
        url = API_BASE + "/auth/v3/tenant_access_token/internal"
        for attempt in range(3):
            try:
                resp = requests.post(
                    url, json={"app_id": self.app_id, "app_secret": self.app_secret}, timeout=15)
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

    def _headers(self):
        token = self.get_token()
        return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    @staticmethod
    def _normalize_filter(filter_conditions):
        if not filter_conditions:
            return None
        if isinstance(filter_conditions, dict) and "conjunction" in filter_conditions:
            return filter_conditions
        return {"conjunction": "and", "conditions": filter_conditions}

    # ---------- 记录 CRUD ----------

    def search_records(self, table_id, filter_conditions=None, page_size=100, field_names=None):
        url = f"{API_BASE}/bitable/v1/apps/{self.base_token}/tables/{table_id}/records/search"
        all_items = []
        page_token = None
        body = {"page_size": page_size}
        filt = self._normalize_filter(filter_conditions)
        if filt:
            body["filter"] = filt
        if field_names:
            body["field_names"] = field_names
        while True:
            # page_token 属于 body 参数（放 query param 会拿不到翻页数据）
            if page_token:
                body["page_token"] = page_token
            else:
                body.pop("page_token", None)
            result = None
            for attempt in range(3):
                try:
                    resp = requests.post(url, headers=self._headers(), json=body, timeout=self.timeout)
                    result = resp.json()
                except Exception as e:
                    self.log(f"搜索记录异常(table={table_id},第{attempt + 1}次): {e}")
                    if attempt < 2:
                        time.sleep(2 ** attempt)
                        continue
                    return all_items
                if result.get("code") != 0:
                    self.log(f"搜索记录失败(table={table_id}): {result}")
                    # token 失效：清缓存重取后重试
                    if attempt < 2 and result.get("code") in (99991661, 99991663, 99991672, 99991668):
                        self._token_cache = {"token": None, "expire_time": 0}
                        time.sleep(2 ** attempt)
                        continue
                    return all_items
                break
            d = result.get("data", {})
            all_items.extend(d.get("items", []))
            if not d.get("has_more"):
                break
            page_token = d.get("page_token")
            if not page_token:
                break
        return all_items

    def get_record(self, table_id, record_id):
        url = f"{API_BASE}/bitable/v1/apps/{self.base_token}/tables/{table_id}/records/{record_id}"
        try:
            resp = requests.get(url, headers=self._headers(), timeout=self.timeout)
            result = resp.json()
            if result.get("code") == 0:
                return result.get("data", {}).get("record")
        except Exception as e:
            self.log(f"获取记录异常(table={table_id}): {e}")
        return None

    def create_record(self, table_id, fields):
        url = f"{API_BASE}/bitable/v1/apps/{self.base_token}/tables/{table_id}/records"
        try:
            resp = requests.post(url, headers=self._headers(), json={"fields": fields},
                                 timeout=self.timeout)
            result = resp.json()
            if result.get("code") == 0:
                return result.get("data", {}).get("record")
            self.log(f"创建记录失败(table={table_id}): {result}")
        except Exception as e:
            self.log(f"创建记录异常(table={table_id}): {e}")
        return None

    def update_record(self, table_id, record_id, fields):
        url = f"{API_BASE}/bitable/v1/apps/{self.base_token}/tables/{table_id}/records/{record_id}"
        try:
            resp = requests.put(url, headers=self._headers(), json={"fields": fields},
                                timeout=self.timeout)
            result = resp.json()
            if result.get("code") == 0:
                return result.get("data", {}).get("record")
            self.log(f"更新记录失败(table={table_id}): {result}")
        except Exception as e:
            self.log(f"更新记录异常(table={table_id}): {e}")
        return None

    def delete_record(self, table_id, record_id):
        url = f"{API_BASE}/bitable/v1/apps/{self.base_token}/tables/{table_id}/records/{record_id}"
        try:
            resp = requests.delete(url, headers=self._headers(), timeout=self.timeout)
            result = resp.json()
            if result.get("code") == 0:
                return True
            self.log(f"删除记录失败(table={table_id}): {result}")
        except Exception as e:
            self.log(f"删除记录异常(table={table_id}): {e}")
        return False

    def batch_create_records(self, table_id, records_fields, batch_size=200):
        """批量创建记录。records_fields: [{"fields": {...}}, ...] 或 [fields_dict, ...]。"""
        if not records_fields:
            return 0
        records = []
        for rf in records_fields:
            if isinstance(rf, dict) and "fields" in rf:
                records.append(rf)
            else:
                records.append({"fields": rf})
        url = f"{API_BASE}/bitable/v1/apps/{self.base_token}/tables/{table_id}/records/batch_create"
        created = 0
        for i in range(0, len(records), batch_size):
            chunk = records[i:i + batch_size]
            try:
                resp = requests.post(url, headers=self._headers(), json={"records": chunk},
                                     timeout=30)
                result = resp.json()
                if result.get("code") == 0:
                    created += len(result.get("data", {}).get("records", []) or chunk)
                else:
                    self.log(f"批量创建记录失败(table={table_id},批{i // batch_size + 1}): "
                             f"code={result.get('code')} msg={result.get('msg')}")
            except Exception as e:
                self.log(f"批量创建记录异常(table={table_id},批{i // batch_size + 1}): {e}")
        return created

    def batch_delete_records(self, table_id, record_ids, batch_size=500):
        ids = [rid for rid in record_ids if rid]
        if not ids:
            return 0
        url = f"{API_BASE}/bitable/v1/apps/{self.base_token}/tables/{table_id}/records/batch_delete"
        deleted = 0
        for i in range(0, len(ids), batch_size):
            chunk = ids[i:i + batch_size]
            try:
                resp = requests.post(url, headers=self._headers(), json={"records": chunk},
                                     timeout=30)
                result = resp.json()
                if result.get("code") == 0:
                    deleted += len(chunk)
                else:
                    self.log(f"批量删除记录失败(table={table_id},批{i // batch_size + 1}): "
                             f"code={result.get('code')} msg={result.get('msg')}")
            except Exception as e:
                self.log(f"批量删除记录异常(table={table_id},批{i // batch_size + 1}): {e}")
        return deleted

    def field_exists(self, table_id, field_name):
        """判断表里是否存在某字段（带缓存）。schema 变更前的防御。"""
        now = time.time()
        key = (table_id, field_name)
        cached = self._field_cache.get(key)
        if cached and cached[1] > now:
            return cached[0]
        exists = True  # 查询失败时假定存在，避免误伤正常写入
        url = f"{API_BASE}/bitable/v1/apps/{self.base_token}/tables/{table_id}/fields"
        try:
            resp = requests.get(url, headers=self._headers(), params={"page_size": 100},
                                timeout=self.timeout)
            result = resp.json()
            if result.get("code") == 0:
                items = result.get("data", {}).get("items", [])
                exists = any(f.get("field_name") == field_name for f in items)
        except Exception:
            pass
        self._field_cache[key] = (exists, now + 300)
        return exists


# ========== 字段值解析（纯函数，供双方直接 import） ==========

def _unwrap_formula(val):
    """公式字段值 {'type': N, 'value': [...]} -> 提取 value 列表"""
    if isinstance(val, dict) and "value" in val and isinstance(val["value"], list):
        return val["value"]
    return val


def _text_of(item):
    """从单元格元素提取文本（dict 或 str）"""
    if isinstance(item, dict):
        return str(item.get("text") or item.get("name") or "")
    return str(item)


def get_field_text(fields, key, default=""):
    """从字段值中提取文本（兼容文本/公式/单选字段）"""
    val = _unwrap_formula(fields.get(key))
    if val is None:
        return default
    if isinstance(val, list):
        return "".join(_text_of(item) for item in val)
    if isinstance(val, dict):
        return _text_of(val)
    return str(val)


def get_field_number(fields, key, default=0):
    """从字段值中提取数字（兼容数字/公式字段）；整数值返回 int，避免爱心被写成浮点"""
    val = _unwrap_formula(fields.get(key))
    if val is None:
        return default
    if isinstance(val, list):
        if not val:
            return default
        val = val[0]
        if isinstance(val, dict):
            val = val.get("value") if val.get("value") is not None else (val.get("text") or val.get("name"))
    if isinstance(val, dict):
        val = val.get("value") if val.get("value") is not None else (val.get("text") or val.get("name"))
    try:
        f = float(val)
        return int(f) if f.is_integer() else f
    except (ValueError, TypeError):
        return default


def get_select_value(fields, key, default=""):
    """获取单选字段值（兼容单选/公式字段）"""
    val = _unwrap_formula(fields.get(key))
    if val is None:
        return default
    if isinstance(val, list):
        if not val:
            return default
        return _text_of(val[0])
    if isinstance(val, dict):
        return _text_of(val)
    return str(val)


def get_multi_select_value(fields, key):
    """获取多选字段值，返回选项名列表"""
    val = _unwrap_formula(fields.get(key))
    if val is None:
        return []
    if isinstance(val, list):
        return [t for t in (_text_of(item) for item in val) if t]
    if isinstance(val, (str, int, float)) and val not in ("", None):
        return [str(val)]
    return []


def get_attachment_tokens(fields, key):
    """获取附件字段的 file_token 列表"""
    val = fields.get(key)
    if not val or not isinstance(val, list):
        return []
    tokens = []
    for item in val:
        if isinstance(item, dict):
            token = item.get("file_token") or item.get("token")
            if token:
                tokens.append(token)
    return tokens


def get_date_value(fields, key, default=""):
    """获取日期字段值（时间戳毫秒），返回 %Y-%m-%d"""
    return get_datetime_value(fields, key, default)


def get_datetime_value(fields, key, default=""):
    """获取 DateTime 字段值，返回 %Y-%m-%d。

    兼容 Feishu Bitable 日期的多种返回形态：
    - 列表 [{ "timestamp": 秒 }, ...]（新 API）
    - dict {"value": 毫秒}
    - 直接毫秒整数 len==13；秒整数 len==10
    - 公式包装 {"type":..,"value":[...]}
    """
    val = fields.get(key)
    if val is None:
        return default
    val = _unwrap_formula(val)
    ts = None
    try:
        if isinstance(val, list):
            if not val:
                return default
            first = val[0]
            if isinstance(first, dict):
                ts = first.get("timestamp") or first.get("value") or first.get("time")
            else:
                ts = first
        elif isinstance(val, dict):
            ts = val.get("value") or val.get("timestamp")
        else:
            ts = val
        if ts is None:
            return default
        ts = int(ts)
        if ts > 10_000_000_000:  # 毫秒
            ts = ts / 1000
        return time.strftime("%Y-%m-%d", time.localtime(ts))
    except (ValueError, TypeError, OverflowError):
        return str(val)


def get_phone_value(fields, key, default=""):
    """获取电话字段值（电话字段 type=13 返回 [{"number": ...}]，需提取 number）"""
    val = fields.get(key)
    if val is None:
        return default
    if isinstance(val, str):
        return val
    if isinstance(val, dict):
        return val.get("number") or val.get("text") or str(val)
    if isinstance(val, list):
        if not val:
            return default
        first = val[0]
        if isinstance(first, dict):
            return first.get("number") or first.get("text") or str(first)
        return str(first)
    return str(val)
