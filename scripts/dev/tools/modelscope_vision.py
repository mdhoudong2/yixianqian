#!/usr/bin/env python3
"""ModelScope 图片识别工具 - CodeBuddy 可调用
用法:
  python3 modelscope_vision.py <图片URL或本地路径> ["问题（可选）"]
  python3 modelscope_vision.py <图片路径>
"""
import sys
import os
import base64
import requests
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import local_config as _cfg
API_KEY = _cfg.MODELSCOPE_API_KEY

def encode_image(image_path_or_url):
    """如果是本地文件则base64编码，如果是URL则直接返回"""
    if image_path_or_url.startswith("http"):
        return image_path_or_url
    if not os.path.exists(image_path_or_url):
        return None
    with open(image_path_or_url, "rb") as f:
        ext = os.path.splitext(image_path_or_url)[1].lower().lstrip(".")
        mime = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png", "gif": "gif", "webp": "webp"}.get(ext, "jpeg")
        b64 = base64.b64encode(f.read()).decode()
        return f"data:image/{mime};base64,{b64}"

def recognize(image_path_or_url, question="请详细描述这张图片的内容"):
    img_data = encode_image(image_path_or_url)
    if not img_data:
        return f"错误: 找不到图片 {image_path_or_url}"

    # 使用 Qwen-VL 模型
    url = "https://api-inference.modelscope.cn/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": "Qwen/Qwen2.5-VL-72B-Instruct",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": img_data}},
                    {"type": "text", "text": question}
                ]
            }
        ],
        "max_tokens": 2000
    }
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=60)
        data = resp.json()
        if "choices" in data:
            return data["choices"][0]["message"]["content"]
        return json.dumps(data, ensure_ascii=False, indent=2)
    except Exception as e:
        return f"识别失败: {e}"

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python3 modelscope_vision.py <图片URL或路径> [问题]")
        sys.exit(1)
    img = sys.argv[1]
    q = sys.argv[2] if len(sys.argv) > 2 else "请详细描述这张图片的内容"
    print(recognize(img, q))
