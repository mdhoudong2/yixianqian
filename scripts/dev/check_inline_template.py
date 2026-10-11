#!/usr/bin/env python3
"""内联模板自闭合非 void 标签检查（零依赖）。

index.html 里的 Vue 模板是「浏览器内嵌模板」(in-DOM template)：浏览器 HTML
解析器不认非 void 元素的自闭合写法（如 <van-empty ... />），会把它们当开标签
处理，其后的兄弟节点被吞成子节点。后果：轻则内容插进 slot 导致渲染错乱，重则
v-else/v-else-if 找不到相邻 v-if，模板编译失败、页面白屏（vue compiler-30）。

本脚本用标准库 html.parser 模拟浏览器解析行为：
- <x ... /> 且 x 是 HTML 非 void 标签时，按开标签压栈（伪自闭合）；
- 该伪自闭合元素被弹出前，每遇到一个非空白文本或子元素，即判定为吞噬并报错；
- <svg>/<math> 外来内容里自闭合是浏览器认可的（<circle/>、<line/> 等），不报；
- HTML void 元素（input/img/br/...）自闭合合法，其本身不算「伪自闭合」，
  但作为内容被前面的伪自闭合元素吞掉时会计入「被吞内容」。

已知基线（BASELINE，生产既有、非本次改动范围）：
  两处「<van-empty ... /> + 紧跟带 v-if 的重试按钮」（2026-08/10 上线）：
  浏览器会把按钮解析进 Empty 的默认 slot，与 Vant 官方写法
  <van-empty>…<van-button>…</van-button></van-empty> 的运行时 DOM 等价，生产长期
  正常；本次任务红线要求不改既有代码，故按「标签行原文 + 被吞内容」指纹精确豁免。
  基线不会掩盖新问题：同一位置若新增被吞内容（例如后面再加 v-else/v-else-if）、
  或出现任何其他位置的吞噬，指纹不匹配，照常 fail；基线条目不再命中时会提示移除。

用法: python3 scripts/dev/check_inline_template.py [file ...]
默认检查仓库根目录的 web/frontend/index.html；有未豁免吞噬时 exit 1。
"""

import sys
from html.parser import HTMLParser
from pathlib import Path

VOID_ELEMENTS = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
})
FOREIGN_ROOTS = frozenset({"svg", "math"})

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TARGETS = (REPO_ROOT / "web" / "frontend" / "index.html",)

# 指纹 = (伪自闭合标签所在行去空白后的原文, 被吞内容描述)；行号漂移不影响匹配。
BASELINE = (
    ("<van-empty :description=\"candidatesError || '暂无可选人员'\" />", "子元素 <van-button>"),
    ("<van-empty :description=\"onetooneCandidatesError || '暂无可选人员'\" />", "子元素 <van-button>"),
)


class _Frame:
    __slots__ = ("tag", "line", "fake")

    def __init__(self, tag, line, fake):
        self.tag = tag
        self.line = line
        self.fake = fake


class InlineTemplateChecker(HTMLParser):
    def __init__(self):
        HTMLParser.__init__(self, convert_charrefs=True)
        self.stack = []
        self.findings = []  # [(line, tag, what)]

    # ---- HTMLParser 回调 ----

    def handle_starttag(self, tag, attrs):
        self._note_swallowed(tag, is_element=True)
        if tag in VOID_ELEMENTS:
            return
        self.stack.append(_Frame(tag, self.getpos()[0], fake=False))

    def handle_startendtag(self, tag, attrs):
        if self._in_foreign():
            return  # SVG/MathML：自闭合被浏览器认可，直接闭合
        self._note_swallowed(tag, is_element=True)
        if tag in VOID_ELEMENTS:
            return
        # 浏览器把 <x /> 当开标签处理，后续内容都会变成它的子节点（吞噬）
        self.stack.append(_Frame(tag, self.getpos()[0], fake=True))

    def handle_endtag(self, tag):
        if tag in VOID_ELEMENTS:
            return
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return
        # 多余闭合标签：忽略，浏览器同样容错

    def handle_data(self, data):
        text = data.strip()
        if text:
            self._note_swallowed(text, is_element=False)

    # ---- 内部 ----

    def _in_foreign(self):
        return any(f.tag in FOREIGN_ROOTS for f in self.stack)

    def _note_swallowed(self, item, is_element):
        if not self.stack:
            return
        top = self.stack[-1]
        if not top.fake:
            return
        # 每个被吞项各报一次：首个被吞项被基线豁免后，后来新增的兄弟仍会被抓到
        if is_element:
            what = "子元素 <%s>" % item
        else:
            snippet = item if len(item) <= 50 else item[:50] + "..."
            what = "文本 %r" % snippet
        self.findings.append((top.line, top.tag, what))


def check(path):
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    checker = InlineTemplateChecker()
    checker.feed(text)
    checker.close()
    findings = []
    for line, tag, what in checker.findings:
        src = lines[line - 1].strip() if 0 <= line - 1 < len(lines) else ""
        findings.append({
            "line": line,
            "tag": tag,
            "what": what,
            "fingerprint": (" ".join(src.split()), what),
        })
    return findings


def main(argv):
    targets = [Path(a) for a in argv[1:]] or list(DEFAULT_TARGETS)
    failed = False
    for path in targets:
        if not path.is_absolute():
            path = Path.cwd() / path
        if not path.exists():
            print("[ERROR] 文件不存在: %s" % path)
            failed = True
            continue
        findings = check(path)
        exempted = [f for f in findings if f["fingerprint"] in BASELINE]
        problems = [f for f in findings if f["fingerprint"] not in BASELINE]
        hit = {f["fingerprint"] for f in exempted}
        stale = [b for b in BASELINE if b not in hit]
        if problems:
            failed = True
            print("[FAIL] %s" % path)
            for f in problems:
                print("  %s:%d: <%s ... /> 是伪自闭合，吞掉了%s（浏览器会把它当子节点）"
                      % (path, f["line"], f["tag"], f["what"]))
            print("  共 %d 处未豁免；请改为显式开闭合 <tag ...></tag>" % len(problems))
        else:
            suffix = "（基线豁免 %d 处）" % len(exempted) if exempted else ""
            print("[OK] %s: 未发现需处理的非 void 自闭合吞咽%s" % (path, suffix))
        for f in exempted:
            print("  [BASE] %s:%d: <%s ... /> 吞掉了%s（生产既有，基线豁免）"
                  % (path, f["line"], f["tag"], f["what"]))
        if path.resolve() == DEFAULT_TARGETS[0].resolve():
            # 基线是针对 index.html 的，检查其他文件时不提示失效
            for entry in stale:
                print("  [WARN] 基线条目未再命中（若已显式修复请从 BASELINE 移除）: %s + %s"
                      % entry)
    if failed:
        print("")
        print("内联模板检查未通过：非 void 自闭合标签在浏览器内嵌模板里会吞掉后面的")
        print("兄弟节点，可能造成渲染错乱或 Vue 编译失败（白屏）。")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
