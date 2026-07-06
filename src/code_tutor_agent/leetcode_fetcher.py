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
    # 优先使用 LeetCode 提供的 exampleTestcases 结构化字段
    raw_examples = q.get("exampleTestcases") or ""
    examples = [e.strip() for e in raw_examples.split("\n") if e.strip()]

    # 如果结构化字段为空，则从 HTML 的 <pre> 标签中兜底提取
    if not examples and content_html:
        examples_raw = re.findall(r"<pre>(.*?)</pre>", content_html, re.DOTALL)
        for ex in examples_raw:
            # 对 <pre> 内容单独做一次清洗
            ex_clean = _html_to_text(ex)
            if ex_clean:
                examples.append(ex_clean)

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


def problem_to_api_dict(p: 'LeetCodeProblem') -> dict:
    """Convert to the dict shape expected by the API response."""
    return {
        "title": p.title,
        "description": p.description,
        "difficulty": p.difficulty.lower(),
        "examples": p.examples,
        "constraints": p.constraints,
        "starter_code": p.starter_code,
        "hints": p.hints,
        "tags": p.tags,
        "session_id": "",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fetch LeetCode problem and render as Markdown.")
    parser.add_argument(
        "slug",
        type=str,
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