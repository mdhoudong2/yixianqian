"""腾讯云照片合规判定（图像标签 + 静默活体）：只拦"非人类"照片。

判定规则（两个条件同时满足才拦）：
  1. 图像标签命中动物类（宠物/猫/狗/哺乳动物等，置信度 >= ANIMAL_CONF）
     或明确卡通类名称（动漫/卡通/漫画/插画/手办/玩偶/表情包，置信度 >= CARTOON_CONF）；
  2. 静默活体接口判定"照片中没有人脸"（NoFaceInPhoto 异常）。

真人+宠物/猫滤镜因活体接口能检出人脸而不拦；侧脸/低头真人无动物标签也不拦。
第三方异常（SDK 缺失、超时、鉴权失败、服务未开通等）一律放行并记日志，
避免外部服务抖动阻塞用户上传；判定结果由调用方写入 sidecar 缓存。
"""

import base64
import io
import logging

log = logging.getLogger(__name__)

ANIMAL_CONF = 30        # 动物标签置信度阈值
CARTOON_CONF = 80       # 明确卡通名称置信度阈值
API_TIMEOUT = 8         # 单次 API 超时（秒）

ANIMAL_C2 = {
    "宠物", "哺乳动物", "鸟", "禽类", "鱼", "海洋动物",
    "昆虫", "爬行动物", "两栖动物", "动物", "家畜", "家禽", "野生动物",
}
ANIMAL_NAMES = ("猫", "狗", "宠物", "哺乳动物", "兔", "仓鼠", "动物")
CARTOON_NAMES = ("动漫", "卡通", "漫画", "插画", "手办", "玩偶", "表情包")


def _label_hit(labels):
    """从标签列表取动物/卡通最高置信度，返回 (animal, animal_name, cartoon, cartoon_name)。"""
    animal, aname, cartoon, cname = 0, "", 0, ""
    for x in labels or []:
        name = x.get("n") or ""
        c2 = x.get("c2") or ""
        conf = x.get("c") or 0
        if (c2 in ANIMAL_C2 or any(k in name for k in ANIMAL_NAMES)) and conf > animal:
            animal, aname = conf, name
        if any(k in name for k in CARTOON_NAMES) and conf > cartoon:
            cartoon, cname = conf, name
    return animal, aname, cartoon, cname


def judge(labels, liveness_noface):
    """纯判定：标签命中 且 活体接口判定无人脸 -> blocked。"""
    animal, aname, cartoon, cname = _label_hit(labels)
    blocked = bool(liveness_noface) and (animal >= ANIMAL_CONF or cartoon >= CARTOON_CONF)
    return {
        "blocked": blocked,
        "reason": (aname or cname) if blocked else "",
        "animal": animal,
        "cartoon": cartoon,
        "noface": bool(liveness_noface),
    }


def _prepare(data):
    """压缩到最长边 1080 的 JPEG，降低 API 体积与时延；失败则原样返回。"""
    try:
        from PIL import Image, ImageOps
        im = Image.open(io.BytesIO(data))
        im = ImageOps.exif_transpose(im).convert("RGB")
        w, h = im.size
        if max(w, h) > 1080:
            r = 1080.0 / max(w, h)
            im = im.resize((max(1, int(w * r)), max(1, int(h * r))), Image.BILINEAR)
        out = io.BytesIO()
        im.save(out, format="JPEG", quality=85)
        return out.getvalue()
    except Exception:
        return data


def _http_profile(endpoint):
    from tencentcloud.common.profile.http_profile import HttpProfile
    hp = HttpProfile()
    hp.endpoint = endpoint
    hp.reqTimeout = API_TIMEOUT
    return hp


def _client(secret_id, secret_key, region, endpoint, module):
    from tencentcloud.common import credential
    from tencentcloud.common.profile.client_profile import ClientProfile
    if module == "iai":
        from tencentcloud.iai.v20200303 import iai_client
        client_cls = iai_client.IaiClient
    else:
        from tencentcloud.tiia.v20190529 import tiia_client
        client_cls = tiia_client.TiiaClient
    cp = ClientProfile()
    cp.httpProfile = _http_profile(endpoint)
    return client_cls(credential.Credential(secret_id, secret_key), region, cp)


def _detect_labels(payload, secret_id, secret_key, region):
    from tencentcloud.tiia.v20190529 import models
    client = _client(secret_id, secret_key, region, "tiia.tencentcloudapi.com", "tiia")
    req = models.DetectLabelRequest()
    req.ImageBase64 = base64.b64encode(payload).decode()
    resp = client.DetectLabel(req)
    return [{"n": x.Name, "c": x.Confidence, "c2": x.SecondCategory}
            for x in (resp.Labels or [])]


def _liveness_noface(payload, secret_id, secret_key, region):
    """True=活体接口判定无人脸；False=检出人脸（分数高低不论）。其他异常抛给上层放行。"""
    from tencentcloud.iai.v20200303 import models
    client = _client(secret_id, secret_key, region, "iai.tencentcloudapi.com", "iai")
    req = models.DetectLiveFaceAccurateRequest()
    req.Image = base64.b64encode(payload).decode()
    try:
        client.DetectLiveFaceAccurate(req)
        return False
    except Exception as e:
        if "NoFaceInPhoto" in str(e):
            return True
        raise


def check_photo(data, secret_id, secret_key, region="ap-guangzhou"):
    """完整检查。返回 {"blocked","reason","animal","cartoon","noface"}；
    无凭据或调用异常时返回 {"blocked": False, "skipped": ...}（放行）。"""
    if not secret_id or not secret_key:
        return {"blocked": False, "skipped": "no-credentials"}
    try:
        payload = _prepare(data)
        labels = _detect_labels(payload, secret_id, secret_key, region)
        animal, aname, cartoon, cname = _label_hit(labels)
        if animal < ANIMAL_CONF and cartoon < CARTOON_CONF:
            return {"blocked": False, "reason": "", "animal": animal,
                    "cartoon": cartoon, "noface": None}
        noface = _liveness_noface(payload, secret_id, secret_key, region)
        res = judge(labels, noface)
        log.info("腾讯照片判定 %s animal=%s cartoon=%s noface=%s blocked=%s",
                 aname or cname, animal, cartoon, noface, res["blocked"])
        return res
    except Exception as e:
        log.warning("腾讯照片判定失败，按放行处理: %s", e)
        return {"blocked": False, "skipped": str(e)[:120]}
