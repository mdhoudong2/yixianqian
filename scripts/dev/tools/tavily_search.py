#!/usr/bin/env python3
"""Tavily 联网搜索工具 - CodeBuddy 可调用
用法: python3 tavily_search.py "搜索关键词" [max_results]
"""
import sys
import json
import requests
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import local_config as _cfg

def search(query, max_results=5):
    api_key = _cfg.TAVILY_API_KEY
    resp = requests.post(
        "https://api.tavily.com/search",
        json={
            "api_key": api_key,
            "query": query,
            "max_results": max_results,
            "search_depth": "basic",
            "include_answer": True
        },
        timeout=30
    )
    data = resp.json()
    result = []
    if data.get("answer"):
        result.append(f"摘要: {data['answer']}\n")
    for item in data.get("results", []):
        result.append(f"标题: {item.get('title', '')}")
        result.append(f"链接: {item.get('url', '')}")
        result.append(f"内容: {item.get('content', '')[:500]}")
        result.append("")
    return "\n".join(result) if result else json.dumps(data, ensure_ascii=False, indent=2)

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python3 tavily_search.py '搜索关键词' [结果数量]")
        sys.exit(1)
    q = sys.argv[1]
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    print(search(q, n))
