"""
Fetch LeetCode problem via GraphQL.

Can be imported by both the API endpoint and the CLI script.
"""

import json
import re
import time
import logging
import argparse
import urllib.request
import urllib.error
import html as html_mod
from dataclasses import dataclass, field
from typing import Optional

# 配置日志记录
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("LeetCodeFetcher")


@dataclass
class LeetCodeProblem:
    title: str
    slug: str
    difficulty: str
    description: str
    examples: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    follow_up: str = ""
    hints: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    starter_code: str = ""
    content_html: str = ""


def _html_to_text(html: str) -> str:
    """
    健壮的 HTML 转纯文本工具。
    修复：<sup>4</sup> 被直接删除导致 10^4 变成 104 的问题。
    """
    if not html:
        return ""

    # 1. 保留语义标签
    html = re.sub(r"<sup>(.*?)</sup>", r"^\1", html, flags=re.DOTALL)  # 上标
    html = re.sub(r"<sub>(.*?)</sub>", r"_\1", html, flags=re.DOTALL)  # 下标
    html = re.sub(r"<br\s*/?>", "\n", html)  # 换行
    html = re.sub(r"</p>", "\n\n", html)  # 段落换行
    html = re.sub(r"</li>", "\n", html)  # 列表项换行

    # 2. 提取 <pre> 标签内容并保留换行（用于示例解析）
    # 将 <pre> 内的标签先清空，但保留内容
    html = re.sub(r"<pre>(.*?)</pre>", lambda m: f"<pre>{m.group(1)}</pre>", html, flags=re.DOTALL)

    # 3. 去除所有剩余 HTML 标签
    html = re.sub(r"<[^>]+>", "", html)

    # 4. Unescape HTML 实体 (如 &lt; -> <, &amp; -> &)
    html = html_mod.unescape(html)

    # 5. 规范化空白字符
    html = re.sub(r"[ \t]+", " ", html)
    html = re.sub(r"\n{3,}", "\n\n", html)
    return html.strip()


def _post_with_retry(req: urllib.request.Request, max_retries: int = 3, timeout: int = 15) -> dict:
    """
    发送 POST 请求并处理重试逻辑。
    修复：遇到 429 (Too Many Requests) 或网络错误时指数退避重试。
    """
    for attempt in range(max_retries):
        try:
            logger.debug(f"Attempt {attempt + 1}: Requesting {req.full_url}")
            resp = urllib.request.urlopen(req, timeout=timeout)
            data = json.loads(resp.read().decode())
            return data
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < max_retries - 1:
                wait_time = 2 ** attempt  # 1s, 2s, 4s
                logger.warning(f"Rate limited (429). Retrying in {wait_time} seconds...")
                time.sleep(wait_time)
                continue
            raise ValueError(f"HTTP Error {e.code}: {e.reason} from {req.full_url}")
        except urllib.error.URLError as e:
            if attempt < max_retries - 1:
                wait_time = 2 ** attempt
                logger.warning(f"Network error: {e.reason}. Retrying in {wait_time} seconds...")
                time.sleep(wait_time)
                continue
            raise ValueError(f"Network Error: {e.reason}")
        except json.JSONDecodeError:
            raise ValueError("Failed to decode JSON response from LeetCode.")


def fetch_problem(slug: str, domain: str = "leetcode.cn") -> LeetCodeProblem:
    """
    Fetch a LeetCode problem by slug via GraphQL.
    """
    logger.info(f"Fetching problem '{slug}' from domain '{domain}'...")

    graphql_url = f"https://{domain}/graphql"

    # 修复：增加 translatedContent 和 translatedTitle，适配 leetcode.cn
    query = {
        "query": """
        query getProblem($titleSlug: String!) {
            question(titleSlug: $titleSlug) {
                title
                titleSlug
                translatedTitle
                difficulty
                content
                translatedContent
                exampleTestcases
                topicTags { name translatedName }
                hints
                codeSnippets { langSlug code }
            }
        }
        """,
        "variables": {"titleSlug": slug},
    }

    # 修复：使用完整真实的浏览器 User-Agent，防止被 Cloudflare 拦截
    headers = {
        "Content-Type": "application/json",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Origin": f"https://{domain}",
        "Referer": f"https://{domain}/problems/{slug}/",
    }

    req = urllib.request.Request(
        graphql_url,
        data=json.dumps(query).encode(),
        headers=headers,
    )

    # 发送请求并重试
    data = _post_with_retry(req)

    # 验证响应结构
    if "errors" in data:
        err_msgs = [e.get("message", str(e)) for e in data["errors"]]
        raise ValueError(f"GraphQL errors: {'; '.join(err_msgs)}")

    if not data.get("data") or not data["data"].get("question"):
        raise ValueError(f"Problem '{slug}' not found on {domain}")

    q = data["data"]["question"]

    # 修复：优先使用 translatedContent (中文)，如果不存在再回退到 content (英文)
    content_html = q.get("translatedContent") or q.get("content") or ""

    # 修复：如果 content 为 None，直接赋空字符串，防止后续正则报 TypeError
    if not content_html:
        logger.warning(f"Problem '{slug}' has no content. It might be a premium problem.")

    # 将完整的 HTML 转换为纯文本，便于按关键字切分章节
    full_text = _html_to_text(content_html)

    # ── 解析 Description ──
    # 描述通常在 "Example 1:" 或 "Example:" 之前
    match_ex = re.search(r"Example\s*1:", full_text)
    desc_end = match_ex.start() if match_ex else len(full_text)
    desc = full_text[:desc_end].strip()

    # ── 解析 Examples ──
    # 优先从 HTML <pre> 标签提取完整格式（含 Input/Output 文本）
    examples = []
    if content_html:
        examples_raw = re.findall(r"<pre>(.*?)</pre>", content_html, re.DOTALL)
        for ex in examples_raw:
            ex_clean = _html_to_text(ex)
            if ex_clean and ("Input" in ex_clean or "输入" in ex_clean):
                examples.append(ex_clean)

    # 如果 HTML 中没有格式化的示例，回退到 GraphQL 的 exampleTestcases
    if not examples:
        raw_examples = q.get("exampleTestcases") or ""
        examples = [e.strip() for e in raw_examples.split("\n") if e.strip()]

    # ── 解析 Constraints ──
    constraints = []
    match_const = re.search(r"Constraints:\s*", full_text)
    if match_const:
        const_start = match_const.end()
        # 约束条件一直持续到 "Follow up:" 或字符串末尾
        match_fu = re.search(r"Follow\s*up:", full_text[const_start:])
        const_end = const_start + match_fu.start() if match_fu else len(full_text)
        const_str = full_text[const_start:const_end].strip()
        # 按换行符拆分成列表，并去除多余的项目符号
        constraints = [c.strip("- *").strip() for c in const_str.split("\n") if c.strip()]

    # ── 解析 Follow-up ──
    follow_up = ""
    m = re.search(r"Follow\s*up:\s*(.*?)(?:\n\n|$)", full_text, re.DOTALL)
    if m:
        follow_up = m.group(1).strip()

    # ── 解析 Hints ──
    # 修复：hints 可能是 HTML 片段，统一做一次清洗
    raw_hints = q.get("hints") or []
    hints = [_html_to_text(h) for h in raw_hints if h]

    # ── 解析 Starter code ──
    starter_code = ""
    for s in q.get("codeSnippets", []):
        if s.get("langSlug") in ("python", "python3"):
            starter_code = s.get("code", "")
            break

    logger.info(f"Successfully fetched and parsed '{slug}'.")

    return LeetCodeProblem(
        title=q.get("translatedTitle") or q.get("title", slug),
        slug=slug,
        difficulty=q.get("difficulty", ""),
        description=desc,
        examples=examples,
        constraints=constraints,
        follow_up=follow_up,
        hints=hints,
        tags=[t["name"] for t in q.get("topicTags", [])],
        starter_code=starter_code,
        content_html=content_html,
    )


def problem_to_markdown(p: LeetCodeProblem) -> str:
    """Render a LeetCodeProblem as Markdown."""
    parts = [
        f"# {p.title}",
        "",
        f"**难度**: {p.difficulty}  ",
        f"**标签**: {', '.join(p.tags)}",
        "",
        "---",
        "",
        "## 题目描述",
        "",
        p.description,
    ]

    if p.examples:
        parts += ["", "## 示例"]
        for i, ex in enumerate(p.examples, 1):
            parts += ["", f"### 示例 {i}", "", "", ex, "```"]

    if p.constraints:
        parts += ["", "## 约束条件", ""]
        parts += [f"- {c}" for c in p.constraints]

    if p.follow_up:
        parts += ["", "## 进阶", "", p.follow_up]

    if p.hints:
        parts += ["", "## 提示"]
        for i, h in enumerate(p.hints, 1):
            parts += ["", f"<details><summary>提示 {i}</summary>", "", h, "", "</details>"]

    parts += [
        "",
        "---",
        "",
        "## 模板代码",
        "",
        "```python",
        p.starter_code,
        "```",
        "",
    ]

    return "\n".join(parts)


import argparse
import json


def extract_function_signature(starter_code: str) -> str:
    """Extract a function_signature string from LeetCode-style starter code.

    Parses ``def method(self, nums: List[int], target: int)`` into
    ``"nums: List[int], target: int -> None"`` (return type inferred from
    annotation or ``None``).

    Handles nested brackets (e.g. ``List[List[int]]``), default values
    (e.g. ``x: int = 100``), and methods with only ``self``.
    """
    if not starter_code:
        return ""

    def_match = re.search(r"def\s+(\w+)\s*\(", starter_code)
    if not def_match:
        return ""

    paren_start = def_match.end() - 1

    # Find matching closing paren (handles nested brackets like List[int])
    depth = 0
    i = paren_start
    while i < len(starter_code):
        if starter_code[i] == "(":
            depth += 1
        elif starter_code[i] == ")":
            depth -= 1
            if depth == 0:
                break
        i += 1

    if depth != 0:
        return ""

    params_str = starter_code[paren_start + 1 : i].strip()

    # Extract return type from after the closing paren, up to the trailing colon
    rest = starter_code[i + 1 :].strip()
    return_type = "None"
    if rest.startswith("->"):
        ret_part = rest[2:].strip()
        ret_part = re.split(r"[:\n]", ret_part)[0].strip()
        if ret_part:
            return_type = ret_part

    # Skip 'self' parameter
    if params_str.startswith("self,"):
        params_str = params_str[5:].strip()
    elif params_str == "self":
        params_str = ""

    if not params_str:
        return f"-> {return_type}"

    # Split params by comma, respecting bracket nesting
    clean_params = []
    current = ""
    bracket_depth = 0
    for ch in params_str:
        if ch in "([{":
            bracket_depth += 1
            current += ch
        elif ch in ")]}":
            bracket_depth -= 1
            current += ch
        elif ch == "," and bracket_depth == 0:
            clean_params.append(current.strip())
            current = ""
        else:
            current += ch
    if current.strip():
        clean_params.append(current.strip())

    result = []
    for param in clean_params:
        param = param.strip()
        if not param:
            continue
        if ":" in param:
            name, typ = param.split(":", 1)
            # Remove default value from type (e.g. "List[int] = None")
            typ = typ.split("=", 1)[0].strip()
            result.append(f"{name.strip()}:{typ}")
        else:
            result.append(param)

    if not result:
        return f"-> {return_type}"

    return ",".join(result) + f" -> {return_type}"


def problem_to_api_dict(p: 'LeetCodeProblem') -> dict:
    """Convert to the dict shape expected by the API response."""
    return {
        "title": p.title,
        "description": p.description,
        "description_html": p.content_html or "",
        "difficulty": p.difficulty.lower(),
        "examples": p.examples,
        "constraints": p.constraints,
        "starter_code": p.starter_code,
        "hints": p.hints,
        "tags": p.tags,
        "session_id": "",
        "parsed_test_cases": _parse_examples_to_test_cases(p.examples, p.starter_code),
    }


def _parse_examples_to_test_cases(examples: list[str], starter_code: str) -> list[dict]:
    """Parse LeetCode example text into structured test cases.

    Handles two formats:

    1. Formatted text (from HTML <pre> tags)::

        Input: nums = [2,7,11,15], target = 9
        Output: [0,1]

    2. Raw values (from LeetCode GraphQL ``exampleTestcases``)::

        [2,7,11,15]     ← arg 1 of test case 1
        9                ← arg 2 of test case 1
        [0,1]            ← expected output of test case 1
        [3,2,4]          ← arg 1 of test case 2
        ...

    For format 2, the number of input arguments is inferred from the
    ``starter_code`` method signature.
    """
    import re

    test_cases = []

    # ── Try format 1: formatted Input/Output lines ──
    has_formatted = any("Input" in ex or "输入" in ex for ex in examples)
    if has_formatted:
        for ex in examples:
            lines = ex.strip().split("\n")
            input_line = ""
            output_line = ""
            for line in lines:
                line = line.strip()
                if line.startswith("输入") or line.startswith("Input"):
                    input_line = line
                elif line.startswith("输出") or line.startswith("Output"):
                    output_line = line

            if not input_line or not output_line:
                continue

            input_str = re.sub(r"^(?:输入|Input)\s*[:：]\s*", "", input_line).strip()
            parts = _split_input_args(input_str)
            input_args = []
            for part in parts:
                eq_match = re.search(r"=\s*(.*)", part)
                if eq_match:
                    input_args.append(eq_match.group(1).strip())
                else:
                    input_args.append(part.strip())
            output_val = re.sub(r"^(?:输出|Output)\s*[:：]\s*", "", output_line).strip()
            test_cases.append({
                "input_args": input_args,
                "expected_output": output_val,
                "explanation": f"LeetCode 示例 {len(test_cases) + 1}",
                "is_hidden": False,
            })
        return test_cases

    # ── Format 2: raw values from exampleTestcases ──
    # Determine number of input args from starter_code method signature
    sig_match = re.search(r"def\s+\w+\s*\(self\s*,\s*([^)]+)\)", starter_code)
    num_args = 0
    if sig_match:
        params = sig_match.group(1).split(",")
        num_args = len([p for p in params if p.strip() and not p.strip().startswith("*")])
    if num_args == 0:
        num_args = 1  # fallback

    # Group by (num_args + 1) lines per test case
    # Each group: num_args input lines + 1 output line
    step = num_args + 1
    for i in range(0, len(examples), step):
        group = examples[i:i + step]
        if len(group) < step:
            continue
        input_args = group[:num_args]
        expected_output = group[num_args].strip()
        test_cases.append({
            "input_args": input_args,
            "expected_output": expected_output,
            "explanation": f"LeetCode 示例 {len(test_cases) + 1}",
            "is_hidden": False,
        })

    return test_cases


def _split_input_args(text: str) -> list[str]:
    """Split by ', ' outside brackets/braces.

    E.g., "nums = [2,7,11,15], target = 9" → ["nums = [2,7,11,15]", "target = 9"]
    """
    parts = []
    depth = 0
    current = []
    for ch in text:
        if ch in ("[", "(", "{"):
            depth += 1
            current.append(ch)
        elif ch in ("]", ")", "}"):
            depth -= 1
            current.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
    if current:
        parts.append("".join(current).strip())
    return parts


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fetch LeetCode problem and render as Markdown.")
    parser.add_argument(
        "slug",
        type=str,
        default="container-with-most-water",
        help="Problem slug, e.g. 'two-sum' or 'palindrome-number'"
    )
    parser.add_argument(
        "--domain",
        type=str,
        default="leetcode.cn",
        choices=["leetcode.cn", "leetcode.com"],
        help="LeetCode domain to fetch from (default: leetcode.cn)"
    )
    args = parser.parse_args()

    try:
        # 获取题目数据
        problem = fetch_problem(args.slug, domain=args.domain)
        # problem = fetch_problem("container-with-most-water", domain=args.domain)

        # 打印解析结果 (Markdown 格式)
        print("\n" + "=" * 50 + " Markdown Output " + "=" * 50 + "\n")
        markdown_output = problem_to_markdown(problem)
        print(markdown_output)

        # 也可以选择打印 API 字典格式以验证数据结构
        # print("\n" + "=" * 50 + " API Dict Output " + "=" * 50 + "\n")
        # print(json.dumps(problem_to_api_dict(problem), indent=2, ensure_ascii=False))

    except ValueError as e:
        logger.error(f"Failed to fetch problem: {e}")
    except Exception as e:
        logger.exception(f"An unexpected error occurred: {e}")