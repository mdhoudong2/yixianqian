"""飞书多维表格（Bitable）API 封装：DAO + 字段值解析（bot 与 H5 后端共用）。

原 bot 与 web/backend/bitable.py 各有一份 search/update/create/delete 及
get_field_text 等字段解析函数，此处统一实现：

- 过滤条件兼容两种形态：
    - bot 传入完整 filter（含 conjunction: and/or）
    - web 传入条件列表（自动包装为 conjunction=and）
- 字段解析以 web 版（列表全部拼接）为准，bot 场景均为单值字段，行为一致。
"""
import threading
import time

import requests

API_BASE = "https://open.feishu.cn/open-apis"

# 模块级连接池：复用 TCP/TLS 连接（跨海握手一次 ~1s，复用后每次调用省掉）
_http = requests.Session()
_adapter = requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=16)
_http.mount("https://", _adapter)

# 全局请求节流（令牌桶，20 req/s）：bot 多轮询线程 + H5 快照/写请求共用，
# 防止并发洪峰触发飞书限流 1255002 / 连接被重置（RemoteDisconnected）。
_rate_lock = threading.Lock()
_rate_next = [0.0]
_RATE_INTERVAL = 0.05  # 20 req/s

def _throttle():
    with _rate_lock:
        now = time.time()
        due = _rate_next[0]
        if now < due:
            time.sleep(due - now)
            now = time.time()
        _rate_next[0] = now + _RATE_INTERVAL


class BitableClient:
    def __init__(self, app_id, app_secret, base_token, logger=None, timeout=10):
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
            _throttle()
            try:
                resp = _http.post(
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

    def search_records(self, table_id, filter_conditions=None, page_size=100, field_names=None, timeout=30):
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
            _throttle()
            if page_token:
                body["page_token"] = page_token
            else:
                body.pop("page_token", None)
            result = None
            for attempt in range(2):
                try:
                    resp = _http.post(url, headers=self._headers(), json=body, timeout=timeout)
                    result = resp.json()
                except Exception as e:
                    self.log(f"搜索记录异常(table={table_id},第{attempt + 1}次): {e}")
                    if attempt < 1:
                        time.sleep(2 ** attempt)
                        continue
                    return None
                if result.get("code") != 0:
                    self.log(f"搜索记录失败(table={table_id}): {result}")
                    # token 失效：清缓存重取后重试
                    if attempt < 1 and result.get("code") in (99991661, 99991663, 99991672, 99991668):
                        self._token_cache = {"token": None, "expire_time": 0}
                        time.sleep(2 ** attempt)
                        continue
                    # 飞书内部瞬时错误也重试一次，避免失败清空快照
                    if attempt < 1 and result.get("code") in (1255002, 1254291, 2200):
                        time.sleep(1)
                        continue
                    return None
                break
            if result is None:
                return None
            d = result.get("data", {})
            all_items.extend(d.get("items", []))
            if not d.get("has_more"):
                break
            time.sleep(0.15)
            page_token = d.get("page_token")
            if not page_token:
                break
        return all_items

    def get_record(self, table_id, record_id):
        url = f"{API_BASE}/bitable/v1/apps/{self.base_token}/tables/{table_id}/records/{record_id}"
        try:
            resp = _http.get(url, headers=self._headers(), timeout=self.timeout)
            result = resp.json()
            if result.get("code") == 0:
                return result.get("data", {}).get("record")
        except Exception as e:
            self.log(f"获取记录异常(table={table_id}): {e}")
        return None

    def create_record(self, table_id, fields):
        url = f"{API_BASE}/bitable/v1/apps/{self.base_token}/tables/{table_id}/records"
        try:
            resp = _http.post(url, headers=self._headers(), json={"fields": fields},
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
            resp = _http.put(url, headers=self._headers(), json={"fields": fields},
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
            resp = _http.delete(url, headers=self._headers(), timeout=self.timeout)
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
            _throttle()
            try:
                resp = _http.post(url, headers=self._headers(), json={"records": chunk},
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
            _throttle()
            try:
                resp = _http.post(url, headers=self._headers(), json={"records": chunk},
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
            resp = _http.get(url, headers=self._headers(), params={"page_size": 100},
                                timeout=self.timeout)
            result = resp.json()
            if result.get("code") == 0:
                items = result.get("data", {}).get("items", [])
                exists = any(f.get("field_name") == field_name for f in items)
        except Exception:
            pass
        self._field_cache[key] = (exists, now + 300)
        return exists

    def upload_attachment(self, file_bytes, file_name, content_type="application/octet-stream"):
        """上传附件到多维表格，返回 file_token（附件字段写入 [{"file_token": token}]）。"""
        url = f"{API_BASE}/drive/v1/medias/upload_all"
        token = self.get_token()
        if not token:
            return None
        # multipart 上传不能带 Content-Type: application/json，交由 requests 自动生成 boundary
        headers = {"Authorization": f"Bearer {token}"}
        try:
            resp = _http.post(
                url,
                headers=headers,
                data={
                    "file_name": file_name,
                    "parent_type": "bitable_file",
                    "parent_node": self.base_token,
                    "size": str(len(file_bytes)),
                },
                files={"file": (file_name, file_bytes, content_type)},
                timeout=30,
            )
            result = resp.json()
            if result.get("code") == 0:
                return result.get("data", {}).get("file_token")
            self.log(f"上传附件失败: {result}")
        except Exception as e:
            self.log(f"上传附件异常: {e}")
        return None


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
        if abs(ts) > 10_000_000_000:  # 毫秒（含 1970 年前的负时间戳，如 -1210752000000）
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

def validate_id_card(idc):
    """校验中国居民身份证 15/18位：长度+日期+地址前缀+18位校验位。

    返回 (ok: bool, msg: str)，msg 为空表示合法，否则为原因；15位无校验位仅验日期/前缀。
    轻量本地校验，无法验真伪/人证一致（需公安付费接口）。
    """
    import datetime as _dt
    idc = (idc or "").strip().upper().replace(" ", "")
    if not idc:
        return False, "空"
    _weights = [7,9,10,5,8,4,2,1,6,3,7,9,10,5,8,4,2]
    _check_map = ['1','0','X','9','8','7','6','5','4','3','2']
    _valid_prefixes = set([f"{i:02d}" for i in list(range(11,16))+list(range(21,24))+list(range(31,38))+list(range(41,47))+list(range(50,55))+list(range(61,66))+[71,81,82,91]])
    if len(idc) == 15:
        if not idc.isdigit():
            return False, "15位含非数字"
        y = "19" + idc[6:8]
        m = idc[8:10]
        d = idc[10:12]
        try:
            _dt.date(int(y), int(m), int(d))
        except:
            return False, f"15位日期非法 {y}-{m}-{d}"
        if idc[:2] not in _valid_prefixes:
            return False, f"15位地址前缀非法 {idc[:2]}"
        return True, ""
    if len(idc) == 18:
        if not idc[:17].isdigit():
            return False, "前17位含非数字"
        if idc[17] not in "0123456789X":
            return False, "末位非法"
        y, m, d = idc[6:10], idc[10:12], idc[12:14]
        try:
            bd = _dt.date(int(y), int(m), int(d))
            if bd > _dt.date.today():
                return False, f"生日未来 {y}-{m}-{d}"
            age = _dt.date.today().year - int(y) - ((_dt.date.today().month, _dt.date.today().day) < (int(m), int(d)))
            if age < 0 or age > 120:
                return False, f"年龄异常 {age}"
        except:
            return False, f"日期非法 {y}-{m}-{d}"
        if idc[:2] not in _valid_prefixes:
            return False, f"地址前缀非法 {idc[:2]}"
        s = sum(int(idc[i]) * _weights[i] for i in range(17))
        r = s % 11
        expect = _check_map[r]
        if idc[17] != expect:
            return False, f"校验位错 期望 {expect} 实 {idc[17]}"
        return True, ""
    return False, f"长度非法 {len(idc)}"


def get_id_valid(fields, id_key="身份证号"):
    """从字段 dict 判断身份证是否格式合法，返回 bool"""
    ok, _ = validate_id_card(get_field_text(fields, id_key))
    return ok

