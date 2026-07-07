"""
Fetch LeetCode official/community solutions via GraphQL.

Usage:
    # As module
    from code_tutor_agent.leetcode.leetcode_solution_fetcher import fetch_official_solution
    sol = fetch_official_solution("two-sum", "leetcode.cn")

    # As PoC script
    PYTHONPATH=src python -m code_tutor_agent.leetcode.leetcode_solution_fetcher two-sum
"""
from __future__ import annotations

import json
import logging
import urllib.request
from typing import Optional

from code_tutor_agent.leetcode.leetcode_fetcher import _post_with_retry, _html_to_text

logger = logging.getLogger("LeetCodeSolution")

GRAPHQL_URL_CN = "https://leetcode.cn/graphql/"
GRAPHQL_URL_COM = "https://leetcode.com/graphql"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)


# ── GraphQL: 官方题解 ──
# NOTE: leetcode.cn uses ArticleNode as the solution type, which only supports:
#   id, title, content, contentTypeId, canSeeDetail
# leetcode.com supports additional fields: paidOnly, hasVideoSolution,
#   paidOnlyVideo, rating { ... }, topic { ... }

Q_OFFICIAL_FROM_QUESTION = """\
query OfficialSolution($titleSlug: String!) {
  question(titleSlug: $titleSlug) {
    solution {
      id
      title
      content
      contentTypeId
      canSeeDetail
    }
  }
}
"""


def fetch_official_solution(slug: str, domain: str = "leetcode.cn") -> Optional[dict]:
    """Fetch the official solution for a LeetCode problem via GraphQL.

    Args:
        slug: Problem title slug (e.g. "two-sum").
        domain: "leetcode.cn" or "leetcode.com".

    Returns:
        Dict with id, title, content_html, content_text, can_see_detail,
        or None if no solution exists.
    """
    # leetcode.cn requires trailing slash on /graphql/
    url = f"https://{domain}/graphql" if domain == "leetcode.com" else f"https://{domain}/graphql/"

    payload = json.dumps({
        "query": Q_OFFICIAL_FROM_QUESTION,
        "variables": {"titleSlug": slug},
        "operationName": "OfficialSolution",
    }).encode()

    headers = {
        "Content-Type": "application/json",
        "User-Agent": UA,
        "Accept": "application/json",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Origin": f"https://{domain}",
        "Referer": f"https://{domain}/problems/{slug}/solution/",
    }

    req = urllib.request.Request(url, data=payload, headers=headers)
    data = _post_with_retry(req)

    if "errors" in data:
        raise ValueError(f"GraphQL errors: {data['errors']}")

    q = (data.get("data") or {}).get("question") or {}
    sol = q.get("solution")
    if not sol:
        logger.warning("Problem '%s' has no official solution (new/paid/not logged in)", slug)
        return None

    content_html = sol.get("content") or ""
    return {
        "id": sol.get("id", ""),
        "title": sol.get("title", ""),
        "content_html": content_html,
        "content_text": _html_to_text(content_html),
        "can_see_detail": sol.get("canSeeDetail", False),
    }


# ── GraphQL: 社区题解列表 ──

Q_SOLUTION_TOPICS = """\
query questionSolutionTopics(
  $questionSlug: String!
  $first: Int!
  $skip: Int!
  $orderBy: TopicSortingOptionEnum
  $tagSlugs: [String!]
) {
  questionSolutionTopics(
    questionSlug: $questionSlug
    first: $first
    skip: $skip
    orderBy: $orderBy
    tagSlugs: $tagSlugs
  ) {
    edges {
      node {
        title
        slug
        url
        post { id content }
      }
    }
    totalCount
  }
}
"""


def _build_request(url: str, query: str, variables: dict, slug: str, domain: str):
    """Build a GraphQL request with anti-crawl headers."""
    payload = json.dumps({"query": query, "variables": variables}).encode()
    return urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "User-Agent": UA,
            "Accept": "application/json",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Origin": f"https://{domain}",
            "Referer": f"https://{domain}/problems/{slug}/solution/",
        },
    )


def fetch_solution_list(
    slug: str,
    domain: str = "leetcode.cn",
    first: int = 10,
    skip: int = 0,
    order_by: str = "MOST_RECENT",
) -> list[dict]:
    """Fetch community solution list for a problem.

    NOTE: The community solution GraphQL schema differs between leetcode.cn and
    leetcode.com, and may require authentication. This is a placeholder that
    works on leetcode.com with the correct query schema. For leetcode.cn the
    query types need discovery (see `questionSolutions` on leetcode.com).
    """
    url = GRAPHQL_URL_CN if domain == "leetcode.cn" else GRAPHQL_URL_COM
    req = _build_request(
        url,
        Q_SOLUTION_TOPICS,
        {
            "questionSlug": slug,
            "first": first,
            "skip": skip,
            "orderBy": order_by,
            "tagSlugs": [],
        },
        slug,
        domain,
    )
    data = _post_with_retry(req)
    if "errors" in data:
        raise ValueError(f"GraphQL errors: {data['errors']}")

    topics = (data.get("data") or {}).get("questionSolutionTopics") or {}
    edges = topics.get("edges") or []
    results = []
    for e in edges:
        node = e.get("node") or {}
        post = node.get("post") or {}
        results.append({
            "title": node.get("title", ""),
            "slug": node.get("slug", ""),
            "url": node.get("url", ""),
            "content_html": post.get("content", ""),
            "content_text": _html_to_text(post.get("content", "")),
        })
    return results


# ── GraphQL: 单篇社区题解正文 ──

Q_SOLUTION_DETAIL = """\
query solutionDetail($slug: String!, $titleSlug: String!) {
  solution(slug: $slug, titleSlug: $titleSlug) {
    title
    slug
    content
    contentTranslated
    author { username }
    summary
  }
}
"""


def fetch_solution_detail(
    solution_slug: str,
    question_slug: str,
    domain: str = "leetcode.cn",
) -> dict:
    """Fetch the full body of a community solution by its slug."""
    url = GRAPHQL_URL_CN if domain == "leetcode.cn" else GRAPHQL_URL_COM
    req = _build_request(
        url,
        Q_SOLUTION_DETAIL,
        {"slug": solution_slug, "titleSlug": question_slug},
        question_slug,
        domain,
    )
    data = _post_with_retry(req)
    if "errors" in data:
        raise ValueError(f"GraphQL errors: {data['errors']}")

    sol = (data.get("data") or {}).get("solution") or {}
    content = sol.get("contentTranslated") or sol.get("content") or ""
    return {
        "title": sol.get("title", ""),
        "slug": sol.get("slug", solution_slug),
        "author": (sol.get("author") or {}).get("username", ""),
        "summary": sol.get("summary", ""),
        "content_html": content,
        "content_text": _html_to_text(content),
    }


# ── PoC / CLI entry point ──

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Fetch LeetCode solutions")
    parser.add_argument("slug", help="Problem title slug, e.g. two-sum")
    parser.add_argument(
        "--domain", default="leetcode.cn",
        choices=["leetcode.cn", "leetcode.com"],
    )
    parser.add_argument(
        "--mode", default="official",
        choices=["official", "list", "detail"],
        help="official=official solution; list=community list; detail=needs --solution-slug",
    )
    parser.add_argument("--solution-slug", default=None)
    args = parser.parse_args()

    if args.mode == "official":
        sol = fetch_official_solution(args.slug, args.domain)
        if sol:
            print(f"Title: {sol['title']}")
            print(f"Content length: {len(sol['content_html'])} chars")
            print(f"Can see detail: {sol['can_see_detail']}")
            print()
            print("=== Content (first 500 chars) ===")
            print(sol["content_text"][:500])
        else:
            print("No official solution found.")

    elif args.mode == "list":
        lst = fetch_solution_list(args.slug, args.domain)
        for i, s in enumerate(lst, 1):
            print(f"{i}. {s['title']}  [{s['slug']}]")
            print(f"   {s['url']}")

    elif args.mode == "detail":
        if not args.solution_slug:
            raise SystemExit("mode=detail requires --solution-slug")
        d = fetch_solution_detail(args.solution_slug, args.slug, args.domain)
        print(f"Title: {d['title']}")
        print(f"Author: {d['author']}")
        print(f"Content length: {len(d['content_html'])} chars")