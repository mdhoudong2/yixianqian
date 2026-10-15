#!/usr/bin/env python3
"""setup return 曝光检查（零依赖）。

index.html 是单个大 setup()：模板里直接绑定的标识符（v-model、@事件、v-if 等）
必须出现在 setup 末尾 `return { ... }` 的对象里，模板才能看到。漏放不会运行报错，
只让交互静默失效——实例（2026-10-15 修）：一对一执行确认弹层新增的 gaOtoRunShow
等 7 个绑定未加入 return，点「执行一对一」后弹层永远不渲染（用户只看到转圈），
后端 API 完全正常、API 级 e2e 也抓不到，只有真实点击 DOM 才会暴露。

判定：
- 模板区 = 最后一个 `<script` 之前的内容；
- 已曝光集合 = 最后一个 `<script` 之后到 `}).use(vant)` 之间、最后一个
  `return {` 之后的文本里出现的全部标识符；
- 模板直接绑定 = 从以下属性值中提取的标识符（根标识符），逐一要求曝光：
    * v-model / v-model:xxx 的值；
    * @事件(.修饰)="NAME(...)" / @事件="NAME" 的处理函数名；
    * v-if / v-else-if / v-show 的布尔表达式——整个表达式的根标识符；单一标识符
      是其特例。之所以解析复合表达式：本次事故的 gaOtoRunK / gaOtoRunPreview 正是
      出现在 `v-if="gaOtoRunK && gaOtoRunPreview && ..."` 复合条件里；
    * :属性="IDENT"（整个值恰为单一标识符）；
    * v-for="x in IDENT" / "(x, i) in IDENT" / "x, i in IDENT" 的源标识符。

放行/排除逻辑（避免噪音误报，均有既定理由）：
- v-for 局部别名（x in X、(a,b) in X、a, b in X 三种写法）：作用域仅限循环元素，
  由模板引擎提供，不需要 setup 曝光；
- 成员访问的属性名（`a.b` 里的 `b`）：只检查根标识符 `a`；
- JS 字面量/关键字（true/false/null/undefined/$event 等）：不是 setup 绑定；
- 字符串字面量内容、HTML 注释、HTML 实体（&amp; 等）：非代码。

检查范围保守：只覆盖上述五类直接绑定属性，不校验 {{ }} 插值与行内语句表达式。

用法: python3 scripts/dev/check_template_bindings.py [file ...]
默认检查仓库根 web/frontend/index.html；有未曝光绑定时 exit 1。
"""

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TARGETS = (REPO_ROOT / "web" / "frontend" / "index.html",)

SCRIPT_TAG = "<script"
MOUNT_CALL = "}).use(vant)"
RETURN_OPEN = "return {"

IDENT = r"[A-Za-z_$][\w$]*"
IDENT_RE = re.compile(IDENT)
# 根标识符：前面不能是单词字符或 `.`（`a.b` 的 `b` 是成员访问，跳过）
ROOT_RE = re.compile(r"(?<![\w$.])" + IDENT)
STRING_RE = re.compile(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"")
ENTITY_RE = re.compile(r"&[a-zA-Z0-9#]+;")
COMMENT_RE = re.compile(r"<!--.*?-->", re.S)

V_MODEL_RE = re.compile(r'\bv-model(?::[A-Za-z-]+)?(?:\.[A-Za-z-]+)*\s*=\s*"([^"]*)"')
EVENT_RE = re.compile(r'@[A-Za-z:.-]+\s*=\s*"([^"]*)"')
COND_RE = re.compile(r'\bv-(?:if|else-if|show)\s*=\s*"([^"]*)"')
BIND_PROP_RE = re.compile(r'(?<![\w:.-]):[A-Za-z][\w-]*\s*=\s*"([^"]*)"')
V_FOR_RE = re.compile(r'\bv-for\s*=\s*"([^"]*)"')
HANDLER_RE = re.compile(r"^(" + IDENT + r")\s*(?:\(|$)")

# JS 字面量/关键字/模板内建名：不是 setup 绑定的标识符
KEYWORDS = frozenset({
    "true", "false", "null", "undefined", "NaN", "Infinity", "$event",
    "this", "typeof", "void", "new", "in", "instanceof", "delete",
    "return", "if", "else", "for", "while", "do", "switch", "case",
    "break", "continue", "throw", "try", "catch", "finally",
    "function", "of", "await", "async", "yield", "class", "const", "let", "var",
})

KIND_MODEL = "v-model"
KIND_EVENT = "@事件"
KIND_COND = "v-if/v-show"
KIND_PROP = ":prop"
KIND_VFOR = "v-for"


def root_idents(expr):
    """提取表达式里的根标识符（先剥字符串字面量与 HTML 实体）。"""
    expr = STRING_RE.sub(" ", expr)
    expr = ENTITY_RE.sub(" ", expr)
    return set(ROOT_RE.findall(expr))


def collect_template_bindings(template):
    """返回 (bindings, aliases)。

    bindings: {名字: {绑定种类, ...}}——模板里的直接绑定候选；
    aliases: 所有 v-for 局部别名（三种写法都收集）。
    """
    tpl = COMMENT_RE.sub(" ", template)
    bindings = {}
    aliases = set()

    def add(name, kind):
        if IDENT_RE.fullmatch(name):
            bindings.setdefault(name, set()).add(kind)

    for m in V_FOR_RE.finditer(tpl):
        value = m.group(1)
        lhs = re.match(r"^\s*\(?\s*([^()]*?)\s*\)?\s+in\s+", value)
        src = re.match(r"^\s*\(?\s*[^()]*?\s*\)?\s+in\s+(.*)$", value)
        if lhs:
            for alias in lhs.group(1).split(","):
                alias = alias.strip()
                if IDENT_RE.fullmatch(alias):
                    aliases.add(alias)
        if src:
            for name in root_idents(src.group(1)):
                add(name, KIND_VFOR)

    for m in V_MODEL_RE.finditer(tpl):
        for name in root_idents(m.group(1)):
            add(name, KIND_MODEL)

    for m in EVENT_RE.finditer(tpl):
        match = HANDLER_RE.match(m.group(1).strip())
        if match:
            add(match.group(1), KIND_EVENT)

    for m in COND_RE.finditer(tpl):
        for name in root_idents(m.group(1)):
            add(name, KIND_COND)

    for m in BIND_PROP_RE.finditer(tpl):
        value = m.group(1).strip()
        if IDENT_RE.fullmatch(value):
            add(value, KIND_PROP)

    return bindings, aliases


def check_file(path):
    """检查单个文件，返回 (missing, checked)。

    missing: {名字: {绑定种类, ...}}——模板直接绑定但不在已曝光集合中；
    checked: 参与曝光检查的模板绑定名字总数。
    文件结构不符合预期时抛 ValueError。
    """
    text = path.read_text(encoding="utf-8")
    script_idx = text.rfind(SCRIPT_TAG)
    if script_idx < 0:
        raise ValueError("未找到 <script，无法定位模板区")
    tail = text[script_idx:]
    mount_idx = tail.rfind(MOUNT_CALL)
    if mount_idx < 0:
        raise ValueError("未找到 %s，无法定位 setup 返回区" % MOUNT_CALL)
    region = tail[:mount_idx]
    ret_idx = region.rfind(RETURN_OPEN)
    if ret_idx < 0:
        raise ValueError("未找到 return {，无法定位已曝光集合")

    exposed = set(re.findall(r"\b[A-Za-z_$][\w$]*\b", region[ret_idx:]))
    bindings, aliases = collect_template_bindings(text[:script_idx])

    missing = {}
    checked = 0
    for name, kinds in bindings.items():
        if name in aliases or name in KEYWORDS:
            continue
        checked += 1
        if name not in exposed:
            missing[name] = kinds
    return missing, checked


def main(argv):
    targets = [Path(a) for a in argv[1:]] or list(DEFAULT_TARGETS)
    checked_total = 0
    problems = []
    for path in targets:
        if not path.is_absolute():
            path = Path.cwd() / path
        if not path.exists():
            problems.append((path, None, "文件不存在"))
            continue
        try:
            missing, checked = check_file(path)
        except ValueError as exc:
            problems.append((path, None, str(exc)))
            continue
        checked_total += checked
        if missing:
            problems.append((path, missing, None))

    if problems:
        for path, missing, error in problems:
            if error:
                print("[ERROR] %s: %s" % (path, error))
                continue
            print("[FAIL] %s: 有 %d 个模板直接绑定未在 setup return 曝光："
                  % (path, len(missing)))
            for name in sorted(missing):
                print("  - %s（%s）" % (name, "、".join(sorted(missing[name]))))
            print("  这些绑定模板看不到、交互会静默失效，请加入 setup 末尾 return { ... }。")
        print("")
        print("setup return 曝光检查未通过：模板直接绑定的标识符必须出现在 setup 最后")
        print("return 的对象里，否则绑定静默失效（不报错），只有真实点击才会暴露。")
        return 1

    print("[OK] 模板绑定均已在 setup return 曝光（%d 个）" % checked_total)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
