"""随机输入生成器 — 用于暴力解生成测试用例。

Day2 流程：LLM 生成题目 + 参考解后，本模块生成随机合法输入，
用参考解跑出期望输出，产出 (input_args, expected_output) 对作为测试用例。

用法：
    from code_tutor_agent.sandbox.input_generator import generate_random_inputs
    inputs = generate_random_inputs(function_sig="nums: List[int], target: int -> List[int]", count=5)
"""

from __future__ import annotations

import ast
import json
import logging
import random
import re
from typing import Any

logger = logging.getLogger(__name__)

# Parse function signature like "nums: List[int], target: int -> List[int]"
# Returns list of (name, type_str) for params, and return_type_str
_SIG_RE = re.compile(r"^(.*?)\s*->\s*(.+)$")
_PARAM_RE = re.compile(r"(\w+)\s*:\s*([^,]+)")

# ── P2: 从 constraints 中提取数值范围的 regex ──
# 匹配 "0 <= ... <= 10^4" 或 "-10^9 <= ... <= 10^9" 或 "1 <= n <= 10^5"
_RANGE_RE = re.compile(
    r"(?P<low>-?\d+(?:\^\d+)?)\s*<=\s*(?:\w+(?:\[[^\]]*\])?)\s*<=\s*(?P<high>-?\d+(?:\^\d+)?)"
)
# 也匹配 "length == 0" 或 "n > 0" 等简单约束
_SINGLE_BOUND_RE = re.compile(r"(?P<op>>=|<=|>|<|=)\s*(?P<val>-?\d+(?:\^\d+)?)")

# ── 签名字符串（str 参数）的合法字符集推断 ──
# 题面/约束里的「字符域」声明。首选「枚举式」（把合法字符逐个列出来）：
#   s 仅由括号 '()[]{}' 组成 / 只包括 '('，')'，'{'，'}'，'['，']' / 由 '0' 和 '1' 组成
# 枚举式没命中时回退关键词式声明。
_BRACKET_ORDER = "()[]{}"
_DOMAIN_DECL_RE = re.compile(
    r"仅由|只由|只包括|只包含|仅包含|只含|由\s*.{0,20}?组成"
    r"|consists of|contains? only|made up of",
    re.IGNORECASE,
)
_QUOTED_CHARS_RE = re.compile(r"'([^']{1,8})'")
# 混合字符域声明（「仅由数字和英文字母组成」）：不能当成纯数字域，
# 否则会把这类题的随机用例全部判成越界而丢弃（题目 35 最长回文子串即此误报）。
_MIXED_DOMAIN_RE = re.compile(
    r"(?:数字|digits)[^。，,.\n]{0,12}(?:和|与|、|\band\b)[^。，,.\n]{0,12}(?:字母|letters?)"
    r"|(?:字母|letters?)[^。，,.\n]{0,12}(?:和|与|、|\band\b)[^。，,.\n]{0,12}(?:数字|digits)"
)

# (触发正则, 允许字符集)；顺序敏感：更具体的圆括号要排在泛化括号之前。
_CHAR_DOMAIN_RULES: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"仅由.{0,10}圆括号|只包含.{0,10}圆括号"
                r"|characters?\s*'\('\s*(?:and|,)\s*'\)'"), "()"),
    (re.compile(r"括号.{0,6}组成|仅由括号|只由括号|只包含.{0,6}括号|只包括.{0,6}括号"
                r"|consists of brackets|contains? only brackets|bracket characters"), "()[]{}"),
    (re.compile(r"二值|binary string|只包含\s*0\s*和\s*1|仅由\s*0\s*和\s*1"), "01"),
    (re.compile(r"仅由数字|只包含数字|由数字组成|仅包含数字"
                r"|consists of digits|contains? only digits"), "0123456789"),
)


def _domain_from_enumeration(line: str) -> str | None:
    """从一行声明里取出被枚举的合法字符，非纯括号/数字域时返回 None。

    要求「本行列出的字符全部落在同一字符类里」，否则不动原行为——例如计算器类
    题面 ``s 由数字、' '、'+'、'-'、'('、')' 组成`` 混了运算符，收紧成括号集反而错。
    """
    tokens = _QUOTED_CHARS_RE.findall(line)
    if not tokens:
        return None
    chars: set[str] = set()
    for tok in tokens:
        if tok.strip() in ("和", "and", "或", "or", "、"):
            continue
        chars.update(tok)
    if not chars:
        return None
    if chars <= set(_BRACKET_ORDER):
        return "".join(c for c in _BRACKET_ORDER if c in chars)
    if chars <= set("0123456789"):
        return "".join(sorted(chars))
    return None


def _eval_power(s: str) -> int:
    """处理 10^4 这样的幂记法为整数 10000；正确处理一元负号（-10^4 = -(10^4)）。"""
    s = s.strip()
    if '^' in s:
        base, exp = s.split('^', 1)
        base = base.strip()
        neg = False
        if base.startswith('-'):
            neg = True
            base = base[1:]
        val = int(base) ** int(exp)
        return -val if neg else val
    return int(s)


def _parse_constraint_ranges(constraints: list[str] | None) -> tuple[int, int, int, int]:
    """从 constraints 中提取数值范围。

    Returns:
        (min_val, max_val, min_len, max_len) — 值范围与数组长度范围。
        解析失败时返回默认值 (-100, 100, 2, 8)。
    """
    if not constraints:
        return -100, 100, 2, 8

    min_val, max_val = -100, 100
    min_len, max_len = 2, 8
    val_low_specified = False  # 是否有约束显式限定了元素下界（如 0 <= x）

    for text in constraints:
        # 尝试匹配 "低 <= 某变量 <= 高"
        for m in _RANGE_RE.finditer(text):
            low = _eval_power(m.group("low"))
            high = _eval_power(m.group("high"))
            # 判断是数组长度约束还是元素值约束
            lhs = m.group(0)  # 整个匹配文本
            if "length" in lhs or "len" in lhs or "size" in lhs:
                if low > min_len:
                    min_len = low
                if high < max_len:
                    max_len = high
            else:
                if low <= high:
                    # 元素值约束：将取值范围收紧到「默认区间 ∩ 约束区间」。
                    # 旧逻辑只会放宽（low 更负 / high 更大时改），导致约束更严格时
                    # 仍用默认的 [-100,100]，从而生成非法值（如 0/1 题出现大整数）。
                    val_low_specified = True
                    new_min = max(min_val, low)
                    new_max = min(max_val, high)
                    if new_min <= new_max:
                        min_val, max_val = new_min, new_max
                    else:
                        # 约束区间完全在默认区间之外（罕见），以约束为准
                        min_val, max_val = low, high
                    if high > 100:
                        # 元素值范围较大，暗示可用更大数组
                        max_len = max(max_len, 8)

    # 保证合理性
    min_len = max(1, min_len)
    max_len = max(max_len, min_len + 1)
    if not val_low_specified and min_val > -1:
        # 未显式限定元素下界时，确保随机值至少覆盖到负数（默认 -100 已含负数，
        # 此处仅在被动抬高下界时才补 -1）；显式下界（如 0 <= x）以约束为准，不加负数。
        min_val = -1
    max_val = max(max_val, 1)

    logger.debug("Parsed constraint ranges: val=[%d, %d] len=[%d, %d]",
                 min_val, max_val, min_len, max_len)
    return min_val, max_val, min_len, max_len


def _normalize_type(t: str) -> str:
    """把签名单元类型归一到生成器认识的写法。

    LLM 写的 starter_code 常是 Python 3.9+ 风格（``list[int]``、``TreeNode | None``、
    前向引用 ``'ListNode'``），而 ``_generate_param_value`` 历史上只认
    ``List[...]`` / ``Optional[...]`` / 裸结构名。不认识时它会「回退成随机整数」
    （见该函数末尾的 Unknown type 分支），于是树/链表/数组题的随机用例
    输入变成 ``846`` 这类标量，判题拿到的期望退化成 ``[]``（题目 179 即如此）。
    归一化放在 ``parse_signature`` 这个唯一入口，生成/sanitize/harness 三路共享。
    """
    s = (t or "").strip()
    # 前向引用引号：``'ListNode'`` / "ListNode"
    if len(s) >= 2 and s[0] in "'\"" and s[-1] == s[0]:
        s = s[1:-1].strip()
    # PEP 604 联合：``X | None`` / ``None | X`` → ``Optional[X]``
    if "|" in s and "[" not in s:
        parts = [p.strip() for p in s.split("|") if p.strip()]
        rest = [p for p in parts if p not in ("None", "NoneType")]
        if len(rest) == 1 and len(rest) < len(parts):
            s = f"Optional[{rest[0]}]"
    # 内置泛型小写：``list[int]`` → ``List[int]``；裸 ``list`` → ``List[int]``
    s = re.sub(r"\blist\[", "List[", s)
    if s == "list":
        s = "List[int]"
    return s


def parse_signature(sig: str) -> tuple[list[tuple[str, str]], str]:
    """Parse a function signature into parameter types and return type.

    Type strings are normalized (see ``_normalize_type``) so that every
    consumer — random input generator, ``sanitize_test_case`` and the
    execution harness — sees the same canonical spelling.

    Args:
        sig: e.g. "nums: List[int], target: int -> List[int]"

    Returns:
        ( [(name, type), ...], return_type )
    """
    m = _SIG_RE.search(sig)
    if not m:
        logger.warning("Cannot parse signature: %s", sig)
        return [], ""

    params_str = m.group(1).strip()
    return_type = _normalize_type(m.group(2).strip())

    params = [(name, _normalize_type(t)) for name, t in _PARAM_RE.findall(params_str)]
    return params, return_type


_STRUCT_TYPES = ("ListNode", "TreeNode", "Node", "GraphNode")


def _unwrap_optional(type_str: str) -> str:
    t = (type_str or "").strip()
    m = re.match(r"^Optional\[(.*)\]$", t)
    return m.group(1).strip() if m else t


def _is_struct_type(type_str: str) -> bool:
    """是否为链表/树/图结构体参数（含结构体数组，如 ``List[Optional[ListNode]]``）。"""
    return any(k in (type_str or "") for k in _STRUCT_TYPES)


def _normalize_domain_text(text: str) -> str:
    """全角引号/括号统一成半角，便于字符域正则命中。"""
    table = str.maketrans("‘’“”（）", "''\"\"()")
    return (text or "").translate(table)


def infer_char_domain(*texts: str) -> str | None:
    """从 constraints + 题面推断 str 参数的合法字符集，推断不出返回 None。

    没这条推断时 ``_random_str`` 只会生成随机小写字母，括号题因此拿到
    ``"chhdxk"`` 这类域外输入，参考解恒 False，整套随机用例失去区分度
    （题目 174「有效的括号」/ 90 即如此）。
    不命中时返回 None，``_random_str`` 保持原有的随机小写字母行为。
    """
    for raw in texts:
        blob = _normalize_domain_text(raw or "")
        if not blob:
            continue
        lines = re.split(r"[\n。]", blob)
        for line in lines:
            if _MIXED_DOMAIN_RE.search(line):
                continue
            if _DOMAIN_DECL_RE.search(line):
                domain = _domain_from_enumeration(line)
                if domain:
                    return domain
        for pattern, alphabet in _CHAR_DOMAIN_RULES:
            for line in lines:
                if _MIXED_DOMAIN_RE.search(line):
                    continue
                if pattern.search(line):
                    return alphabet
    return None


def _random_list_int(min_len=2, max_len=8, min_val=-100, max_val=100) -> str:
    """Generate a random List[int] as a string like '[3, 1, 4]'."""
    n = random.randint(min_len, max_len)
    arr = [random.randint(min_val, max_val) for _ in range(n)]
    return "[" + ",".join(str(v) for v in arr) + "]"


def _random_int(min_val=-1000, max_val=1000) -> str:
    return str(random.randint(min_val, max_val))


def _random_float(min_val=-1000.0, max_val=1000.0) -> str:
    return f"{random.uniform(min_val, max_val):.2f}"


_BRACKET_PAIRS = {"(": ")", "[": "]", "{": "}"}


def _random_valid_bracket_string(alphabet: str) -> str:
    """按 LIFO 闭合规则构造一个合法括号串（字符集限定在 alphabet 内）。

    纯随机取字符时合法串占比只有百分之几（长度 8 约 1.9%），12 条随机用例
    的期望会全落在 False 上——判题失去区分度。因此一半概率构造合法串。
    """
    pairs = {k: v for k, v in _BRACKET_PAIRS.items() if k in alphabet and v in alphabet}
    if not pairs:
        return ""
    to_open = random.randint(1, 4)
    out: list[str] = []
    stack: list[str] = []
    while to_open or stack:
        if stack and (not to_open or random.random() < 0.4):
            out.append(stack.pop())
        else:
            op = random.choice(list(pairs))
            out.append(op)
            stack.append(pairs[op])
            to_open -= 1
    return "".join(out)


def _random_str(max_len=8, alphabet: str | None = None) -> str:
    chars = alphabet or "abcdefghijklmnopqrstuvwxyz"
    n = random.randint(1, max_len)
    if alphabet and set(alphabet) <= set("()[]{}") and random.random() < 0.5:
        return '"' + _random_valid_bracket_string(alphabet) + '"'
    return '"' + "".join(random.choice(chars) for _ in range(n)) + '"'


def _random_struct_array(type_str: str, min_val=-100, max_val=100) -> str:
    """为链表/树类型生成数组形式的随机输入（字符串）。

    链表（ListNode）：``[v1, v2, ...]``
    树（TreeNode/Node）：层序数组，可含 null 表示空子树。
    """
    if type_str in ("TreeNode", "Node"):
        n = random.randint(1, 7)
        vals = []
        for _ in range(n):
            vals.append("null" if random.random() < 0.2 else str(random.randint(min_val, max_val)))
        if vals[0] == "null":
            vals[0] = str(random.randint(min_val, max_val))
        return "[" + ",".join(vals) + "]"
    # 默认按链表处理（ListNode / GraphNode 等）
    n = random.randint(1, 6)
    arr = [random.randint(min_val, max_val) for _ in range(n)]
    return "[" + ",".join(str(v) for v in arr) + "]"


_TYPE_GENERATORS = {
    "int": _random_int,
    "float": _random_float,
    "str": _random_str,
    "bool": lambda: random.choice(["True", "False"]),
}


def _gen_cell(inner: str) -> str:
    """Generate one cell value for a 2D matrix given its element type.

    For ``List[List[str]]`` (grid problems) we emit '0'/'1' single-char
    strings, since grid inputs are almost always flag matrices.
    """
    if inner == "str":
        return '"' + random.choice(["0", "1"]) + '"'
    return _generate_param_value(inner)


def _generate_param_value(
    type_str: str,
    min_val: int | None = None,
    max_val: int | None = None,
    min_len: int | None = None,
    max_len: int | None = None,
    char_domain: str | None = None,
) -> str:
    """Generate a random Python literal string for a given type.

    Args:
        type_str: e.g. "int", "List[int]", "List[str]", "List[List[int]]",
            "ListNode", "Optional[ListNode]", "TreeNode"
        min_val, max_val: P2: value range for int/list elements (from constraints).
        min_len, max_len: P2: length range for lists (from constraints).
        char_domain: 题面声明的合法字符集（见 ``infer_char_domain``），仅作用于 str。

    Returns:
        String representation of a random value, e.g. "42", "[1,2,3]"
    """
    type_str = type_str.strip()

    # 处理 Optional[...] 包装（如 Optional[ListNode]）
    opt_match = re.match(r"^Optional\[(.*)\]$", type_str)
    if opt_match:
        type_str = opt_match.group(1).strip()

    # 链表 / 树结构：以数组形式生成（runner 会按类型还原成对应对象）
    if type_str in ("ListNode", "TreeNode", "Node", "GraphNode"):
        return _random_struct_array(
            type_str,
            min_val if min_val is not None else -100,
            max_val if max_val is not None else 100,
        )

    # Handle 2D List[List[...]] FIRST — more specific than the 1D pattern.
    # (A greedy 1D regex would otherwise swallow "List[List[str]]" whole,
    #  turning it into a ragged list-of-lists of random strings.)
    m2 = re.match(r"^List\[List\[(.+?)\]\]$", type_str)
    if m2:
        inner = m2.group(1).strip()
        rows = random.randint(1, 4)
        cols = random.randint(1, 5)
        matrix = [
            "[" + ",".join(_gen_cell(inner) for _ in range(cols)) + "]"
            for _ in range(rows)
        ]
        return "[" + ",".join(matrix) + "]"

    # Handle 1D List[...]
    m1 = re.match(r"^List\[(.+)\]$", type_str)
    if m1:
        inner = m1.group(1).strip()
        n = random.randint(min_len or 1, max_len or 6)
        items = [
            _generate_param_value(inner, min_val, max_val, min_len, max_len, char_domain)
            for _ in range(n)
        ]
        return "[" + ",".join(items) + "]"

    # Handle basic types
    if type_str == "int":
        return _random_int(
            min_val if min_val is not None else -1000,
            max_val if max_val is not None else 1000,
        )
    if type_str == "str" and char_domain:
        return _random_str(alphabet=char_domain)
    gen = _TYPE_GENERATORS.get(type_str)
    if gen:
        return gen()

    # Fallback: return a default
    logger.warning("Unknown type '%s', defaulting to int", type_str)
    return _random_int(
        min_val if min_val is not None else -1000,
        max_val if max_val is not None else 1000,
    )


def generate_random_inputs(
    function_sig: str,
    count: int = 3,
    constraints: list[str] | None = None,
    description: str | None = None,
    seed: int | None = 42,
    # P2: 从 constraints 解析出的数值范围，None 表示使用默认值
    _parsed_min_val: int | None = None,
    _parsed_max_val: int | None = None,
    _parsed_min_len: int | None = None,
    _parsed_max_len: int | None = None,
) -> list[list[str]]:
    """Generate random input argument lists for a function signature.

    Each returned entry is a list of string arguments suitable for
    ``RunnerResult`` (as ``input_args``).

    Args:
        function_sig: e.g. "nums: List[int], target: int -> List[int]"
        count: Number of random inputs to generate.
        constraints: Optional constraint strings；若含「有序/sorted/非递减」等
            关键词，会对 List[int] 输入做升序排序（方案 B）。
            P2: 也会从中解析数值范围，约束随机输入的取值范围。
        description: Optional problem description，同样参与「有序」判定。
        seed: Random seed for reproducibility.
        _parsed_*: Internal use — pre-parsed ranges from constraints.

    Returns:
        List of input_args lists, e.g. [["[1,2,3]", "5"], ["[4,5]", "9"]]
    """
    if seed is not None:
        random.seed(seed)

    # P2: 解析 constraints 中的数值范围
    if _parsed_min_val is None:
        _parsed_min_val, _parsed_max_val, _parsed_min_len, _parsed_max_len = \
            _parse_constraint_ranges(constraints)

    params, _ = parse_signature(function_sig)
    if not params:
        logger.warning("Empty params from sig '%s', returning empty", function_sig)
        return []

    # 综合 constraints + description 判断是否为「有序输入」类题目。
    sort_inputs = _needs_sorted_inputs(*(constraints or []), description or "")
    # 题面声明的合法字符集（括号题 / 01 串 / 数字串），推断不出为 None
    char_domain = infer_char_domain(*(constraints or []), description or "")

    logger.info(
        "▶ generate_random_inputs() — sig=%s, count=%d, sort_inputs=%s, params=%s, "
        "val=[%d,%d] len=[%d,%d] char_domain=%s",
        function_sig, count, sort_inputs, [p[0] for p in params],
        _parsed_min_val, _parsed_max_val, _parsed_min_len, _parsed_max_len,
        char_domain,
    )

    results = []
    for i in range(count):
        args = []
        prev_type = None
        prev_val = None
        for name, type_str in params:
            t = type_str.strip()
            # 位置/倒数索引类参数（如「倒数第 k 个」的 k）：若前一个参数是
            # 结构类型（链表/树），则把 k 约束在 [1, len(结构)]，避免生成非法负值。
            if t == "int" and prev_type and _is_struct_type(prev_type) and prev_val:
                try:
                    _lst = ast.literal_eval(prev_val)
                    _n = len(_lst) if isinstance(_lst, list) else 1
                except Exception:
                    _n = 5
                val = str(random.randint(1, max(1, _n)))
            # 数组后的「长度」参数（m/n/len/size/k…）：约束为该数组的真实长度，
            # 而不是随机整数。这是「合并两个有序数组」等题 m/n 乱数的根因修复。
            elif t == "int" and prev_type and _is_list_type(prev_type) and _looks_like_length(name) and prev_val:
                try:
                    _lst = ast.literal_eval(prev_val)
                    _n = len(_lst) if isinstance(_lst, list) else 1
                except Exception:
                    _n = 5
                val = str(_n)
            else:
                # P2: 传入解析后的范围。
                # 注意：List[int] 的元素也是 int，约束（如 0 <= nums[i] <= 1）应作用于
                # 数组元素，故 List[int] 同样传入值范围；否则元素会回退到默认 [-1000,1000]，
                # 导致 0/1 题等出现非法大整数（旧 bug）。
                _is_int_like = (t == "int" or _is_list_type(t))
                _rmin = _parsed_min_val if _is_int_like else None
                _rmax = _parsed_max_val if _is_int_like else None
                val = _generate_param_value(
                    type_str, _rmin, _rmax, _parsed_min_len, _parsed_max_len, char_domain,
                )
            args.append(val)
            prev_type = t
            prev_val = val
        # 有序类题目：对 List[int] 输入的「真实元素」升序排序（方案 B）。
        # 在 m/n 长度参数已确定之后统一排序，由 sort_sorted_inputs 保证
        # 只排前 m/n 个真实元素、保留补零、不动 m/n。
        if sort_inputs:
            args = sort_sorted_inputs(function_sig, args)
        results.append(args)

    logger.debug("Generated %d random input sets", len(results))
    return results


_UNPARSEABLE = object()


def parse_arg_value(raw):
    """把一个 ``input_args`` 元素解析成 Python 值：先 JSON 再 Python 字面量。

    顺序必须与 harness 的 ``_eval_arg`` 一致：LeetCode 的树/链表示例用 ``null``
    表示空子树，只有 ``json.loads`` 认得；若只用 ``ast.literal_eval``，
    ``[3,9,20,null,null,15,7]`` 会解析失败 → 整条官方示例被当成 malformed 丢弃
    （题目 179 丢示例 1、132 丢示例 2 即此因）。
    """
    for parser in (json.loads, ast.literal_eval):
        try:
            return parser(raw)
        except Exception:
            continue
    return _UNPARSEABLE


def check_input_domain(func_sig: str, input_args: list, char_domain: str | None = None) -> bool:
    """用例输入是否落在题目声明的域内（拦生成器自己造的越界输入）。

    只查两类实际踩过、且校验层（参考解跑通≠输入合法）发现不了的：

    - **结构体参数拿到标量**：harness 会把非 list 的 TreeNode 参数当成 p/q 节点引用，
      查不到就传 ``None``，参考解「正常」返回 ``[]``，于是垃圾用例带着
      一个看似合法的期望落库（pid 179 的 12 条 ``846 -> []``、pid 110 同理）；
    - **str 参数拿到域外字符**：括号题生成 ``"chhdxk"``，参考解恒 False，
      整套随机用例失去区分度（pid 174 / 90 / 119）。
    """
    params, _ = parse_signature(func_sig)
    if len(input_args) != len(params):
        return False
    for (name, type_str), raw in zip(params, input_args):
        t = (type_str or "").strip()
        value = raw if not isinstance(raw, str) else parse_arg_value(raw)
        if value is _UNPARSEABLE:
            return False
        if _is_struct_type(_unwrap_optional(t)):
            if value is not None and not isinstance(value, list):
                return False
        elif t == "str" and char_domain:
            if not isinstance(value, str) or not set(value) <= set(char_domain):
                return False
    return True


def _extract_code(text: str) -> str:
    """Strip markdown fences from code."""
    if match := re.search(r"```(?:python)?\s*\n(.*?)```", text, re.DOTALL):
        return match.group(1).strip()
    return text.strip()


# 提示某 int 参数是「数组长度」的命名集合（合并两个有序数组的 m/n 等都命中）
_LENGTH_HINTS = {"m", "n", "k", "l", "len", "length", "size"}


def _looks_like_length(name: str) -> bool:
    n = name.lower().strip()
    if n in _LENGTH_HINTS:
        return True
    return any(h in n for h in ("len", "length", "size", "count"))


def _is_list_type(type_str: str) -> bool:
    return bool(re.match(r"^List\[", type_str.strip()))


# 提示题目「输入数组应当有序」的关键词（合并有序数组 / 有序数组二分等）。
# 命中后会对 List[int] 随机输入做升序排序，保证语义正确（方案 B）。
_SORTED_HINTS = (
    "non-decreasing", "non-increasing", "sorted in", "ascending order",
    "descending order", "升序", "降序", "有序", "非递减", "非递增", "已排序",
    "sorted array", "sorted list",
)


def _needs_sorted_inputs(*texts: str) -> bool:
    """题目是否要求输入数组「有序」。

    综合 constraints + description 文本判断，命中任一关键词即认为需要。
    这是「合并两个有序数组」随机用例输入无序的根因修复（方案 B）。
    """
    blob = " \n ".join(t for t in texts if t).lower()
    return any(h in blob for h in _SORTED_HINTS)


def _is_list_of_ints(type_str: str) -> bool:
    """仅匹配 1D 的 List[int]，不匹配 List[List[int]] 等。"""
    return re.match(r"^List\[int\]$", type_str.strip()) is not None


def _is_struct_array_type(type_str: str) -> bool:
    """是否为「结构体数组」，如 ``List[Optional[ListNode]]`` / ``List[List[int]]``。

    典型场景：合并 K 个升序链表。题面要求**每条子链表各自升序**，
    随机生成的外层数组里每个子数组都要单独排序，而不是对外层排序。
    """
    t = (type_str or "").replace(" ", "")
    if t.count("[") < 2:
        return False
    return ("ListNode" in t or "TreeNode" in t or "Node" in t
            or re.match(r"^List\[List\[", t) is not None)


def _sort_struct_array(val) -> list:
    """对结构体数组逐个子数组升序排序（子数组含 None 等不可比元素时原样返回）。"""
    if not isinstance(val, list):
        return val
    out = []
    for sub in val:
        if isinstance(sub, list) and all(
            isinstance(x, (int, float)) and not isinstance(x, bool) for x in sub
        ):
            out.append(sorted(sub))
        else:
            out.append(sub)
    return out


def sort_sorted_inputs(func_sig: str, input_args: list[str]) -> list[str]:
    """「有序」类题目：对 List[int] 输入的「真实元素」升序排序，保留补零。

    与粗暴地对整个数组排序不同，这里按各自的「长度参数」(m/n/len/k…) 只排序
    前 ``length`` 个真实元素，尾部的补零保持不动；没有长度配对（如单有序数组）
    则整体排序。不会改动 m/n 本身。

    例：``['[1,0,0,0,0]','3','[2,3]','2']`` -> ``['[0,0,1,0,0]','3','[2,3]','2']``
    """
    params, _ = parse_signature(func_sig)
    # 为每个 List[int] 找紧随其后的「长度」参数做配对
    pairs: list[tuple[int, int]] = []
    for i, (name, t) in enumerate(params):
        if _is_list_of_ints(t):
            for j in range(i + 1, len(params)):
                if params[j][1].strip() == "int" and _looks_like_length(params[j][0]):
                    pairs.append((i, j))
                    break
    values = []
    for a in input_args:
        v = parse_arg_value(a)
        values.append(a if v is _UNPARSEABLE else v)
    used = set()
    for li, ln in pairs:
        used.add(li)
        if isinstance(values[li], list) and ln < len(values) and isinstance(values[ln], int):
            length = values[ln]
            values[li] = sorted(values[li][:length]) + values[li][length:]
    # 无长度配对的 List[int]（如单有序数组 / 二分查找的 nums）：整体排序
    for i, (name, t) in enumerate(params):
        if _is_list_of_ints(t) and i not in used and isinstance(values[i], list):
            values[i] = sorted(values[i])
    # 结构体数组（如 List[Optional[ListNode]]，合并 K 个升序链表）：
    # 题面要求每条子链表各自升序，逐个子数组排序；对外层数组排序是错的
    # （会把链表长度当排序键）。
    for i, (name, t) in enumerate(params):
        if _is_struct_array_type(t) and isinstance(values[i], list):
            values[i] = _sort_struct_array(values[i])
    # 还原成与 input_args 同格式的字符串。必须走 _to_arg_str：
    # 旧的 ``str(v)`` 会把 str 参数的引号剥掉（``"qk"`` → ``qk``），
    # 于是排序类题面（描述里有「非递减/有序」等关键词）的随机用例变成无法解析的字面量。
    return [_to_arg_str(v) for v in values]


def _to_arg_str(v: Any) -> str:
    """把 Python 值还原回 input_args 用的 JSON 字面量字符串。"""
    if isinstance(v, str):
        return '"' + v + '"'
    if isinstance(v, bool):
        return "True" if v else "False"
    if isinstance(v, (int, float)):
        return str(v)
    return json.dumps(v, ensure_ascii=False)


_KW_PREFIX_RE = re.compile(r"^[A-Za-z_]\w*\s*[:=]\s*(.*)$", re.DOTALL)


def _strip_kw_prefix(raw: str) -> str:
    """剥离 LeetCode 示例常见的变量名前缀。

    LLM 生成的 Example ``Input`` 常写成 ``nums = [1,2,3]`` 或 ``nums: [1,2,3]``，
    直接 ``ast.literal_eval`` 会 SyntaxError → 整条用例被丢弃（见题目 15 那类
    「Sample/visible case input malformed, dropping」）。剥掉 ``name = `` / ``name:``
    前缀再解析即可。合法字面量（无此前缀）原样返回，不受影响。
    """
    s = raw.strip()
    m = _KW_PREFIX_RE.match(s)
    if not m:
        return raw
    return m.group(1).strip()


def sanitize_test_case(func_sig: str, tc: dict, sort_inputs: bool = False) -> dict | None:
    """校验并校正一条测试用例的 ``input_args``，使其满足函数签名契约。

    解决两类由随机/LLM 生成器引入、导致判题误判的常见错误：

    1. 数组长度参数（m/n/len/size/k…）被生成成随机整数
       → 重算为紧随其后的真实数组长度（仅在数组尚未补零时，避免覆盖
       合并类已补零输入里 LLM 声明的 ``m``）；
    2. 双数组合并类（如「合并两个有序数组」）：第一个数组需原地容纳两数组，
       长度应为各数组长度之和，因此对第一个数组补零到 ``m + n``。

    当 ``sort_inputs=True``（「有序」类题目，见方案 B），会对各 ``List[int]``
    输入按各自长度参数排序「真实元素」、保留尾部补零。由于 expected 由参考解
    在同份 input 上跑出，排序只让输入更贴近题面语义，不影响判题 Pass。

    返回校正后的 ``tc``（就地修改后返回）；若 ``input_args`` 无法解析或参数个数
    与签名不匹配，返回 ``None``（调用方应丢弃该用例）。
    """
    if not func_sig:
        return tc
    params, _ = parse_signature(func_sig)
    raw_args = tc.get("input_args") or []
    if not params or len(raw_args) != len(params):
        # 参数个数对不上，无法安全校正 -> 交给调用方丢弃
        return None

    values: list = []
    rewrote = False  # 是否有元素剥掉了变量名前缀（需回写 input_args）
    for a in raw_args:
        # 契约上 input_args 元素是 JSON 字面量字符串，但 LLM 生成边界用例时
        # 偶发直接吐 Python 对象（如嵌套 list ``[[1,2],[3]]``）。此时
        # ``_strip_kw_prefix`` 会对 list 调 .strip() → AttributeError，
        # 整个边界用例分支被一次异常带崩（题目 124 即因此丢光边界用例）。
        # 统一先归一化为 JSON 字符串再解析。
        if not isinstance(a, str):
            a = json.dumps(a, ensure_ascii=False)
            rewrote = True
        v = parse_arg_value(a)
        if v is not _UNPARSEABLE:
            values.append(v)
            continue
        # 兜底：整体无法解析时，尝试剥掉 "name = "/ "name:" 前缀再解析
        # （LeetCode 格式示例： "nums = [1,2,3]"）。
        stripped = _strip_kw_prefix(a)
        if stripped != a:
            v = parse_arg_value(stripped)
            if v is not _UNPARSEABLE:
                values.append(v)
                rewrote = True
                continue
        return None

    # 找出 (List 参数下标, 紧随的长度参数下标) 对
    length_pairs: list[tuple[int, int]] = []
    for j, (name, t) in enumerate(params):
        if j + 1 < len(params) and _is_list_type(t) and params[j + 1][1].strip() == "int":
            if _looks_like_length(params[j + 1][0]):
                length_pairs.append((j, j + 1))

    # 既无长度参数、又未剥前缀 -> 原样返回，保留输入格式（如 "[2,7,11,15]" 不加空格）
    if not length_pairs and not rewrote:
        return tc

    # 重算每个长度参数为真实数组长度。
    # 仅当数组「已补零」（声明长度为正 且 数组长度 > 声明长度，即 len==m+n>m）
    # 时才保留 LLM 声明的 m/n，避免把它改坏；其余情况（乱数/负数/未补零）一律
    # 以真实数组长度覆盖。
    for list_idx, len_idx in length_pairs:
        if isinstance(values[list_idx], list):
            arr = values[list_idx]
            declared = values[len_idx]
            if not (isinstance(declared, int) and declared > 0 and len(arr) > declared):
                values[len_idx] = len(arr)

    # 合并类：存在 ≥2 个 (List, 长度) 对 -> 第一个数组补零到各长度之和
    if len(length_pairs) >= 2:
        first_list_idx = length_pairs[0][0]
        total = sum(
            values[len_idx]
            for _, len_idx in length_pairs
            if isinstance(values[len_idx], int)
        )
        lst = values[first_list_idx]
        if isinstance(lst, list) and 0 <= len(lst) < total:
            values[first_list_idx] = lst + [0] * (total - len(lst))

    arg_strs = [_to_arg_str(v) for v in values]
    # 「有序」类题目：对 List[int] 真实元素升序排序（方案 B），保留补零。
    if sort_inputs:
        arg_strs = sort_sorted_inputs(func_sig, arg_strs)
    tc["input_args"] = arg_strs
    return tc