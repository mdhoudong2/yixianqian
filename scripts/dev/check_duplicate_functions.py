#!/usr/bin/env python3
"""内联 JS 函数重名检查（零依赖）。

index.html 的内联脚本是一个巨大的 setup() 作用域：同名 `function` 声明后者静默
覆盖前者——调用点照跑，只是跑去了另一个实现。实例（2026-10-13 修）：分组管理台
新增的 `loadGroupResult(r)` 覆盖了用户端的 `loadGroupResult(activityId, round)`，
用户点「查看分组结果」被管理台版本的 `if (!gaDetailId.value || !r) return;` 空跑
拦截，页面永远停在「未找到你的分组信息」，且无任何报错。

本脚本按「同一 <script> 内、同一缩进层级」近似作用域：
同名（async）function 在同一缩进出现 ≥2 次即报错（无基线豁免——重名必须改名）。

用法: python3 scripts/dev/check_duplicate_functions.py [file ...]
默认检查仓库根 web/frontend/index.html；发现重名时 exit 1。
"""

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TARGETS = (REPO_ROOT / "web" / "frontend" / "index.html",)

SCRIPT_RE = re.compile(r"<script([^>]*)>(.*?)</script>", re.S)
FUNC_RE = re.compile(r"(?m)^(\s*)(?:async\s+)?function\s+(\w+)\s*\(")


def inline_scripts(html):
    for attrs, body in SCRIPT_RE.findall(html):
        if "src=" not in attrs:
            yield body


def check_file(path):
    """返回 [(path, 函数名, 缩进, [脚本内行号...]), ...]。"""
    html = Path(path).read_text(encoding="utf-8")
    findings = []
    for script in inline_scripts(html):
        seen = {}
        for m in FUNC_RE.finditer(script):
            indent = len(m.group(1).expandtabs(8))
            name = m.group(2)
            line = script[:m.start()].count("\n") + 1
            seen.setdefault((indent, name), []).append(line)
        for (indent, name), lines in sorted(seen.items()):
            if len(lines) > 1:
                findings.append((str(path), name, indent, lines))
    return findings


def main():
    targets = sys.argv[1:] or [str(t) for t in DEFAULT_TARGETS]
    findings = []
    for t in targets:
        findings.extend(check_file(t))
    if findings:
        for path, name, indent, lines in findings:
            print(f"[FAIL] {path}: 同名函数 `{name}` 在同一缩进（{indent}）出现 {len(lines)} 次"
                  f"（脚本内行 {lines}）——后者会静默覆盖前者，请改名区分")
        return 1
    print(f"[OK] 未发现同作用域重名函数（{len(targets)} 个文件）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
