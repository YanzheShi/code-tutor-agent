"""随机用例「输入域」防线 + 签名类型归一化回归测试。

对应线上两处坏数据（problems 表实测）：

- pid 179 二叉树的层序遍历：签名写作 ``root:TreeNode | None``（PEP 604 联合），
  生成器只认 ``Optional[TreeNode]``，于是回退成「随机整数」；harness 又把非 list
  的树参数当成 p/q 节点引用、查不到就传 None，参考解「正常」返回 ``[]``，
  12 条 ``846 -> []`` 顺利落库。同时官方示例 ``[3,9,20,null,null,15,7]`` 里的
  ``null`` 过不了 ``ast.literal_eval``，整条示例被 sanitize 丢弃。
- pid 174 有效的括号 / pid 90 有效的括号序列：``_random_str`` 无视题面
  「s 仅由括号 '()[]{}' 组成」，生成 ``"chhdxk"`` 这类域外输入，参考解恒返回
  False，整套随机用例零区分度。
"""
from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from code_tutor_agent.generation.suite import _generate_random_cases, _verify_visible_cases
from code_tutor_agent.generation.state import NullSink
from code_tutor_agent.sandbox.input_generator import (
    check_input_domain,
    infer_char_domain,
    parse_signature,
    generate_random_inputs,
    sanitize_test_case,
)

# ──────────────────────────────────────────────
# 1) 签名类型归一化：树/链表/数组参数不得退化成标量
# ──────────────────────────────────────────────

def test_normalize_pep604_union_and_lowercase_generics():
    params, return_type = parse_signature("root:TreeNode | None -> list[list[int]]")
    assert params == [("root", "Optional[TreeNode]")]
    assert return_type == "List[List[int]]"


def test_normalize_quoted_forward_ref():
    params, _ = parse_signature("head: 'ListNode' -> 'ListNode'")
    assert params == [("head", "ListNode")]


def test_bare_list_type_becomes_int_list():
    params, _ = parse_signature("nums: list -> int")
    assert params == [("nums", "List[int]")]


def test_tree_param_generates_level_order_array_not_scalar():
    """pid 179 的签名：修复前随机生成 ``['846']``，现在必须是层序数组。"""
    sig = "root:TreeNode | None -> list[list[int]]"
    for args in generate_random_inputs(sig, count=6, seed=179):
        raw = args[0]
        assert raw.startswith("["), raw
        assert check_input_domain(sig, args) is True, raw


def test_lowercase_list_param_generates_list():
    sig = "nums:list[int] -> list[list[int]]"
    for args in generate_random_inputs(sig, count=4, seed=19):
        assert isinstance(json.loads(args[0]), list), args


def test_quoted_listnode_param_generates_list():
    sig = "head: 'ListNode' -> 'ListNode'"
    for args in generate_random_inputs(sig, count=4, seed=110):
        assert isinstance(json.loads(args[0]), list), args


# ──────────────────────────────────────────────
# 2) 字符域推断：括号题不再生成英文字母
# ──────────────────────────────────────────────

def test_infer_char_domain_from_chinese_and_english_wording():
    assert infer_char_domain("s 仅由括号 '()[]{}' 组成") == "()[]{}"
    assert infer_char_domain("给定一个只包括 '('，')'，'{'，'}'，'['，']' 的字符串") == "()[]{}"
    assert infer_char_domain("s 仅由 '(' 和 ')' 组成") == "()"
    assert infer_char_domain("s consists of brackets") == "()[]{}"
    assert infer_char_domain("grid 由 '0' 和 '1' 组成") == "01"
    assert infer_char_domain("s 仅由数字组成") == "0123456789"
    assert infer_char_domain("1 <= s.length <= 10^4") is None
    assert infer_char_domain("") is None


def test_mixed_digit_letter_domain_is_not_restricted():
    """「仅由数字和英文字母组成」不能当成纯数字域，否则该类题的随机用例会被全判越界丢弃。"""
    assert infer_char_domain("s 仅由数字和英文字母组成") is None
    assert infer_char_domain("字符串 s 由小写字母和数字组成") is None
    assert infer_char_domain("s consists of digits and English letters") is None


def test_bracket_domain_inputs_are_in_domain_and_mixed_validity():
    """括号题随机输入：字符全在括号集内，且合法/非法都有（否则判题无区分度）。"""
    sig = "s:str -> bool"
    domain = infer_char_domain("s 仅由括号 '()[]{}' 组成")
    args = generate_random_inputs(sig, count=60, seed=174, constraints=["s 仅由括号 '()[]{}' 组成"])
    values = [json.loads(a[0]) for a in args]
    assert all(set(v) <= set("()[]{}") for v in values), values
    assert all(check_input_domain(sig, [json.dumps(v)], domain) for v in values)

    def _valid(s: str) -> bool:
        pairs = {")": "(", "]": "[", "}": "{"}
        stack: list[str] = []
        for ch in s:
            if ch in "([{":
                stack.append(ch)
            elif not stack or stack.pop() != pairs[ch]:
                return False
        return not stack

    flags = [_valid(v) for v in values]
    assert any(flags) and not all(flags), flags


def test_out_of_domain_string_input_is_rejected():
    domain = infer_char_domain("s 仅由括号 '()[]{}' 组成")
    assert check_input_domain("s:str -> bool", ['"chhdxk"'], domain) is False
    assert check_input_domain("s:str -> bool", ['"([])"'], domain) is True


def test_scalar_for_struct_input_is_rejected():
    assert check_input_domain("root:TreeNode | None -> list[list[int]]", ["846"]) is False
    assert check_input_domain("root:TreeNode | None -> list[list[int]]", ["[1,null,2]"]) is True
    # 空树（null）是合法输入
    assert check_input_domain("root:Optional[TreeNode] -> bool", ["null"]) is True


# ──────────────────────────────────────────────
# 3) sanitize 认得 null：官方示例不再被整条丢弃
# ──────────────────────────────────────────────

def test_sanitize_keeps_leetcode_example_with_null_nodes():
    sig = "root:TreeNode | None -> list[list[int]]"
    out = sanitize_test_case(sig, {"input_args": ["[3,9,20,null,null,15,7]"]})
    assert out is not None, "含 null 的官方示例不应被当成 malformed 丢弃"
    assert out["input_args"] == ["[3,9,20,null,null,15,7]"]


def test_sanitize_still_rejects_unparseable_input():
    sig = "nums: List[int], target: int -> List[int]"
    assert sanitize_test_case(sig, {"input_args": ["[1,2,3", "9"]}) is None


def test_sorted_hint_problem_keeps_str_quoting():
    """题面含「非递减」会走 sort_sorted_inputs，旧实现把 str 入参的引号剥掉
    （``"qk"`` → ``qk``），随机用例因此变成无法解析的字面量（pid 160）。"""
    sig = "word: str -> int"
    outs = generate_random_inputs(
        sig, count=6, seed=160,
        constraints=["word 仅由小写英文字母组成"],
        description="元音字母在该字符串中以字典序非递减的方式出现",
    )
    for o in outs:
        assert check_input_domain(sig, o) is True, o


# ──────────────────────────────────────────────
# 4) 生成编排层：域校验先于参考解验证；退化套件不占可见槽位
# ──────────────────────────────────────────────

class _FakeSandbox:
    """按 input_args 查表返回参考解输出，避免真起子进程。"""

    def __init__(self, answers: dict[str, str]):
        self._answers = answers
        self.ran: list[list[str]] = []

    def sanitize(self, func_sig, tc, sort_inputs=False):
        return {"input_args": list(tc["input_args"])}

    def char_domain(self, constraints, description=""):
        return infer_char_domain(*(constraints or []), description or "")

    def input_in_domain(self, func_sig, input_args, char_domain=None):
        return check_input_domain(func_sig, input_args, char_domain)

    def run_solution(self, code, test_cases, function_signature=""):
        args = test_cases[0]["input_args"]
        self.ran.append(args)
        detail = self._answers.get(json.dumps(args))
        if detail is None:
            return [SimpleNamespace(status="Runtime Error", detail="")]
        return [SimpleNamespace(status="Skipped", detail=detail)]


def _run_random_branch(sandbox, *, func_sig, random_inputs, char_domain=None):
    agent = SimpleNamespace(sandbox=sandbox)
    cases, _ = _generate_random_cases(
        agent, NullSink(),
        func_sig=func_sig, sort_inputs=False, use_cross_validation=False,
        optimal_code="x", brute_code="", ref_code="x",
        random_inputs=random_inputs, char_domain=char_domain, problem_id=179,
    )
    return cases


def test_random_branch_drops_out_of_domain_input_before_running_reference():
    """越界输入（树参数拿到标量）必须直接丢弃，而不是让参考解跑出一个 []。"""
    sb = _FakeSandbox({
        json.dumps(["[3,9,20]"]): "[[3],[9,20]]",
        json.dumps(["846"]): "[]",
    })
    cases = _run_random_branch(
        sb, func_sig="root:Optional[TreeNode] -> list[list[int]]",
        random_inputs=[["846"], ["[3,9,20]"]],
    )
    assert [c["input_args"] for c in cases] == [["[3,9,20]"]], cases
    assert sb.ran == [["[3,9,20]"]], "域外输入不应触发参考解执行"


def test_random_branch_hides_degenerate_expected_outputs():
    """随机分支期望全同 ⇒ 无区分度，转为隐藏用例，不再占据可见示例位。"""
    answers = {json.dumps([a]): "True" for a in ["[1]", "[2]", "[3]", "[4]", "[5]"]}
    sb = _FakeSandbox(answers)
    cases = _run_random_branch(
        sb, func_sig="nums: List[int] -> bool",
        random_inputs=[[a] for a in ["[1]", "[2]", "[3]", "[4]", "[5]"]],
    )
    assert len(cases) == 5
    assert all(c["is_hidden"] is True for c in cases), cases


def test_random_branch_keeps_visible_when_expected_outputs_vary():
    answers = {json.dumps([a]): ("True" if i % 2 else "False")
               for i, a in enumerate(["[1]", "[2]", "[3]", "[4]", "[5]"])}
    sb = _FakeSandbox(answers)
    cases = _run_random_branch(
        sb, func_sig="nums: List[int] -> bool",
        random_inputs=[[a] for a in ["[1]", "[2]", "[3]", "[4]", "[5]"]],
    )
    assert any(not c["is_hidden"] for c in cases), cases


def test_visible_branch_drops_stale_scalar_tree_input():
    """库里已有的坏数据（树参数拿到标量）不应经「既有可见用例」通道被带回来。"""
    full = SimpleNamespace(test_cases=[
        {"input_args": ["846"], "expected_output": "[]",
         "explanation": "随机生成测试 1", "is_hidden": False},
        {"input_args": ["[3,9,20]"], "expected_output": "[[3],[9,20]]",
         "explanation": "LeetCode 示例 1", "is_hidden": False},
    ])
    sb = _FakeSandbox({
        json.dumps(["846"]): "[]",
        json.dumps(["[3,9,20]"]): "[[3],[9,20]]",
    })
    cases, _ = _verify_visible_cases(
        SimpleNamespace(sandbox=sb),
        func_sig="root:Optional[TreeNode] -> list[list[int]]",
        sort_inputs=False, use_cross_validation=False,
        optimal_code="x", brute_code="", ref_code="x",
        full=full, problem_id=179,
    )
    assert [c["input_args"] for c in cases] == [["[3,9,20]"]], cases


def test_visible_branch_does_not_apply_char_domain():
    """可见通道只校验形态：官方示例即便不被字符域推断命中也不能丢。"""
    full = SimpleNamespace(test_cases=[
        {"input_args": ['"abc"'], "expected_output": "False",
         "explanation": "LeetCode 示例 1", "is_hidden": False},
    ])
    sb = _FakeSandbox({json.dumps(['"abc"']): "False"})
    cases, _ = _verify_visible_cases(
        SimpleNamespace(sandbox=sb),
        func_sig='s:str -> bool',
        sort_inputs=False, use_cross_validation=False,
        optimal_code="x", brute_code="", ref_code="x",
        full=full, problem_id=174,
    )
    assert [c["input_args"] for c in cases] == [['"abc"']], cases


# ──────────────────────────────────────────────
# 5) LeetCode 导入：中文题面的「提示：」小节要解析成 constraints
# ──────────────────────────────────────────────

_CN_CONTENT = (
    "<p>给你二叉树的根节点&nbsp;root&nbsp;，返回其节点值的 层序遍历 。</p>"
    "<p><strong>示例 1：</strong></p>"
    "<pre>输入：root = [3,9,20,null,null,15,7]\n输出：[[3],[9,20],[15,7]]</pre>"
    "<p><strong>提示：</strong></p>"
    "<ul><li>树中节点数目在范围 [0, 2000] 内</li>"
    "<li>-1000 &lt;= Node.val &lt;= 1000</li></ul>"
)


def test_fetch_problem_parses_chinese_constraints_section(monkeypatch):
    from code_tutor_agent.leetcode import leetcode_fetcher

    def _fake_post(req, max_retries=3, timeout=15):
        return {
            "data": {
                "question": {
                    "title": "Binary Tree Level Order Traversal",
                    "titleSlug": "binary-tree-level-order-traversal",
                    "translatedTitle": "二叉树的层序遍历",
                    "difficulty": "MEDIUM",
                    "content": _CN_CONTENT,
                    "translatedContent": _CN_CONTENT,
                    "exampleTestcases": "[3,9,20,null,null,15,7]",
                    "topicTags": [{"name": "tree", "translatedName": "树"}],
                    "hints": [],
                    "codeSnippets": [{
                        "langSlug": "python3",
                        "code": (
                            "class Solution:\n"
                            "    def levelOrder(self, root: Optional[TreeNode]) "
                            "-> List[List[int]]:\n        pass\n"
                        ),
                    }],
                }
            }
        }

    monkeypatch.setattr(leetcode_fetcher, "_post_with_retry", _fake_post)
    problem = leetcode_fetcher.fetch_problem("binary-tree-level-order-traversal")

    assert problem.constraints == [
        "树中节点数目在范围 [0, 2000] 内",
        "-1000 <= Node.val <= 1000",
    ], problem.constraints
    # 约束解析出来才能把随机取值收紧到题面区间
    assert infer_char_domain(*problem.constraints) is None
