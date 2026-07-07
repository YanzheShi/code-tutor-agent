"""Fetch LeetCode problem via GraphQL.

Can be imported by both the API endpoint and the CLI script.
"""

import json
import re
import urllib.request
import html as html_mod
from dataclasses import dataclass, field
from typing import Optional


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


def fetch_problem(slug: str, domain: str = "leetcode.cn") -> LeetCodeProblem:
    """Fetch a LeetCode problem by slug via GraphQL.

    Args:
        slug: Problem slug, e.g. "two-sum" or "palindrome-number".
        domain: "leetcode.cn" (default) or "leetcode.com".

    Returns:
        A LeetCodeProblem dataclass with all extracted fields.

    Raises:
        ValueError: If the problem is not found or the GraphQL query fails.
    """
    graphql_url = f"https://{domain}/graphql"
    query = {
        "query": """
        query getProblem($titleSlug: String!) {
            question(titleSlug: $titleSlug) {
                title
                titleSlug
                difficulty
                content
                exampleTestcases
                topicTags { name }
                hints
                codeSnippets { langSlug code }
            }
        }
        """,
        "variables": {"titleSlug": slug},
    }

    req = urllib.request.Request(
        graphql_url,
        data=json.dumps(query).encode(),
        headers={
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0",
            "Referer": f"https://{domain}/problems/{slug}/",
        },
    )
    resp = urllib.request.urlopen(req, timeout=15)
    data = json.loads(resp.read())

    # ── Validate response structure ──
    if "errors" in data:
        err_msgs = [e.get("message", str(e)) for e in data["errors"]]
        raise ValueError(f"GraphQL errors: {'; '.join(err_msgs)}")

    if not data.get("data") or not data["data"].get("question"):
        raise ValueError(f"Problem '{slug}' not found on {domain}")

    q = data["data"]["question"]

    content_html = q.get("content", "")

    # ── Description (strip examples from the HTML first) ──
    desc_html = re.sub(r"<pre>.*?</pre>", "", content_html, flags=re.DOTALL)
    # Also strip "Example N:" / "Constraints:" / "Follow-up:" sections
    desc_html = re.sub(
        r"<strong\b[^>]*>Example\s+\d+:?</strong>.*?(?=<strong|<p>\s*$|$)",
        "", desc_html, flags=re.DOTALL,
    )
    desc_html = re.sub(r"Constraints:.*?(?:</ul>|$)", "", desc_html, flags=re.DOTALL)
    desc_html = re.sub(r"<strong>Follow.?up:?</strong>.*?(?:</p>|$)", "", desc_html, flags=re.DOTALL)
    desc = re.sub(r"<[^>]+>", " ", desc_html)
    desc = html_mod.unescape(desc)
    desc = re.sub(r"\s+", " ", desc).strip()

    # ── Examples from <pre> tags ──
    examples_raw = re.findall(r"<pre>(.*?)</pre>", content_html, re.DOTALL)
    examples = []
    for ex in examples_raw:
        ex_clean = html_mod.unescape(re.sub(r"<[^>]+>", "", ex))
        ex_clean = re.sub(r"\s+", " ", ex_clean).strip()
        if ex_clean:
            examples.append(ex_clean)

    # ── Constraints (list items after "Constraints:") ──
    constraints = []
    constraints_section = re.search(
        r"Constraints:\s*(.*?)(?:</ul>|$)", content_html, re.DOTALL
    )
    if constraints_section:
        constraint_items = re.findall(
            r"<li>(.*?)</li>", constraints_section.group(1), re.DOTALL
        )
        for item in constraint_items:
            c = re.sub(r"<[^>]+>", " ", item)
            c = html_mod.unescape(c)
            c = re.sub(r"\s+", " ", c).strip()
            if c:
                constraints.append(c)
    if not constraints:
        # Fallback: comma-separated text
        m = re.search(r"Constraints:\s*(.*?)(?:Follow-up:|$)", content_html, re.DOTALL)
        if m:
            c_text = re.sub(r"<[^>]+>", " ", m.group(1))
            c_text = html_mod.unescape(c_text)
            c_text = re.sub(r"\s+", " ", c_text).strip()
            constraints = [c.strip(" .,") for c in c_text.split(",") if c.strip()]

    # ── Follow-up ──
    follow_up = ""
    m = re.search(r"<strong>Follow.?up:?</strong>\s*(.*?)(?:</p>|$)", content_html, re.DOTALL)
    if m:
        follow_up = re.sub(r"<[^>]+>", " ", m.group(1))
        follow_up = html_mod.unescape(follow_up)
        follow_up = re.sub(r"\s+", " ", follow_up).strip()

    # ── Hints ──
    hints = q.get("hints") or []

    # ── Starter code ──
    starter_code = ""
    for s in q.get("codeSnippets", []):
        if s.get("langSlug") in ("python", "python3"):
            starter_code = s.get("code", "")
            break

    return LeetCodeProblem(
        title=q.get("title", slug),
        slug=slug,
        difficulty=q.get("difficulty", "Medium"),
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
            parts += ["", f"### 示例 {i}", "", "```", ex, "```"]

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


def problem_to_api_dict(p: LeetCodeProblem) -> dict:
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