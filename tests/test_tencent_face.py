"""腾讯云照片判定纯逻辑单测：动物/卡通阈值 + 静默活体"无人脸"的双条件规则。"""
import os
import sys

_WEB_BACKEND = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web", "backend")
if _WEB_BACKEND not in sys.path:
    sys.path.insert(0, _WEB_BACKEND)

import tencent_face  # noqa: E402


def _labels(*items):
    return [{"n": n, "c": c, "c2": c2} for n, c, c2 in items]


def test_cat_photo_blocked_when_no_face():
    res = tencent_face.judge(_labels(("宠物", 54, "宠物"), ("猫", 39, "宠物")), True)
    assert res["blocked"] is True


def test_cat_photo_pass_when_human_face_detected():
    res = tencent_face.judge(_labels(("宠物", 87, "宠物"), ("猫", 83, "宠物")), False)
    assert res["blocked"] is False


def test_human_photo_without_animal_label_not_blocked():
    res = tencent_face.judge(_labels(("人", 97, "人像"), ("面部", 92, "人体部位")), True)
    assert res["blocked"] is False


def test_cartoon_name_blocked_only_when_no_face():
    assert tencent_face.judge(_labels(("动漫", 90, "绘画卡通")), True)["blocked"] is True
    assert tencent_face.judge(_labels(("动漫", 90, "绘画卡通")), False)["blocked"] is False


def test_graffiti_label_not_in_cartoon_blocklist():
    res = tencent_face.judge(_labels(("涂鸦", 79, "绘画卡通")), True)
    assert res["blocked"] is False


def test_low_confidence_animal_not_blocked():
    res = tencent_face.judge(_labels(("宠物", 20, "宠物")), True)
    assert res["blocked"] is False


def test_plant_label_not_blocked():
    res = tencent_face.judge(_labels(("树", 53, "树")), True)
    assert res["blocked"] is False
