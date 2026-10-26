"""SandboxGateway — 沙箱执行薄封装（设计 §8）。

全部方法可在单测中 mock。run_solution 一律 force_local（可信参考解），
避免 JUDGE0_URL 配置存在时出题阶段误走远端。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class SandboxGateway:
    def run_solution(self, code: str, test_cases: list[dict], function_signature: str = "") -> list:
        """在本地沙箱跑参考解，返回 RunnerResult 列表。"""
        from code_tutor_agent.sandbox.runner import run_solution

        return run_solution(
            code,
            test_cases,
            timeout=10.0,
            function_signature=function_signature or None,
            force_local=True,
        )

    def compile(self, code: str) -> bool:
        """校验代码可编译。"""
        try:
            compile(code, "<draft>", "exec")
            return True
        except SyntaxError as exc:
            logger.warning("compile check failed: %s", exc)
            return False

    def random_inputs(
        self,
        func_sig: str,
        n: int,
        seed: int,
        constraints: list[str] | None = None,
        description: str = "",
    ) -> list:
        """按函数签名生成 n 组随机输入（后台用例生成用）。"""
        from code_tutor_agent.sandbox.input_generator import generate_random_inputs

        return generate_random_inputs(
            func_sig,
            count=n,
            seed=seed,
            constraints=constraints,
            description=description,
        )

    def sanitize(self, func_sig: str, tc: dict, sort_inputs: bool = False) -> dict | None:
        """校正用例 input 契约（重算 m/n、补零到 m+n 等），无法校正返回 None。"""
        from code_tutor_agent.sandbox.input_generator import sanitize_test_case

        return sanitize_test_case(func_sig, tc, sort_inputs=sort_inputs)

    def char_domain(self, constraints: list[str] | None, description: str = "") -> str | None:
        """题面声明的 str 合法字符集（括号 / 01 / 数字），推断不出返回 None。"""
        from code_tutor_agent.sandbox.input_generator import infer_char_domain

        return infer_char_domain(*(constraints or []), description or "")

    def input_in_domain(
        self, func_sig: str, input_args: list, char_domain: str | None = None
    ) -> bool:
        """用例输入是否落在题目声明的域内（结构体参数非标量、str 字符集合规）。"""
        from code_tutor_agent.sandbox.input_generator import check_input_domain

        return check_input_domain(func_sig, input_args, char_domain)

    def needs_sorted_inputs(self, *texts: str) -> bool:
        """「有序」类题目（合并有序数组 / 有序数组二分等）需对输入数组排序。"""
        from code_tutor_agent.sandbox.input_generator import _needs_sorted_inputs

        return _needs_sorted_inputs(*texts)

    def struct_prologue(self, topic: str, description: str = "", starter_code: str = "") -> str:
        """注入树/图/链表结构体定义到 starter_code（无则返回空串）。"""
        from code_tutor_agent.sandbox.ds import get_struct_prologue

        return get_struct_prologue(topic, description, starter_code)
