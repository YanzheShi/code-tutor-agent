"""
Fetch LeetCode official/community solutions via GraphQL.
复用 leetcode_fetcher.py 里的 _post_with_retry / _html_to_text。
"""

import json
import logging
import urllib.request
from typing import Optional

from src.code_tutor_agent.leetcode.leetcode_fetcher import _post_with_retry, _html_to_text

logger = logging.getLogger("LeetCodeSolution")

GRAPHQL_URL_CN = "https://leetcode.cn/graphql/"
GRAPHQL_URL_COM = "https://leetcode.com/graphql/"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)


def _build_request(url: str, query: str, variables: dict, slug: str, domain: str):
    """构造带反爬头的 GraphQL 请求。"""
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


# ── 路径①：从题目接口顺带取官方题解 ──
# leetcode_solution_fetcher.py（仅展示改动的核心部分）

Q_OFFICIAL_FROM_QUESTION = """
query OfficialSolution($titleSlug: String!) {
  question(titleSlug: $titleSlug) {
    solution {
      id
      title
      content
      contentTypeId
      paidOnly
      hasVideoSolution
      paidOnlyVideo
      canSeeDetail
      topic {
        id
        commentCount
        topLevelCommentCount
        viewCount
        subscribed
        solutionTags { name slug }
        post {
          id
          status
          creationDate
          author { username isActive }
        }
      }
    }
  }
}
"""

def fetch_official_solution(slug: str, domain: str = "leetcode.cn") -> Optional[dict]:
    """通过 question.solution 字段拿官方题解（对齐 alfa-leetcode-api）。"""
    # ⚠️ 关键修复1：cn 站带尾斜杠，com 站不带
    if domain == "leetcode.cn":
        url = "https://leetcode.cn/graphql/"
    else:
        url = "https://leetcode.com/graphql"

    # ⚠️ 关键修复2：请求体必须带 operationName，cn 站校验更严
    payload = json.dumps({
        "query": Q_OFFICIAL_FROM_QUESTION,
        "variables": {"titleSlug": slug},
        "operationName": "OfficialSolution",   # ← 必填
    }).encode()

    headers = {
        "Content-Type": "application/json",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Origin": f"https://{domain}" ,
        "Referer": f"https://{domain}/problems/{slug}/solution/" ,
        # ⚠️ 关键修复3：部分 cn 站题解接口需要 csrftoken
        # 若仍 403/400，取消下面注释并先 GET 一次题目页拿 cookie
        # "x-csrftoken": "<从 cookie 里取>",
        # "Cookie": "csrftoken=...; LEETCODE_SESSION=...",
    }

    req = urllib.request.Request(url, data=payload, headers=headers)
    data = _post_with_retry(req)

    if "errors" in data:
        raise ValueError(f"GraphQL errors: {data['errors']}")

    q = (data.get("data") or {}).get("question") or {}
    sol = q.get("solution")
    if not sol:
        logger.warning(f"题 '{slug}' 暂无官方题解（新题/付费题/未登录）")
        return None

    content = sol.get("content") or ""   # ← 不再有 contentTranslated
    return {
        "id": sol.get("id", ""),
        "title": sol.get("title", ""),
        "content_html": content,
        "content_text": _html_to_text(content),
        "paid_only": sol.get("paidOnly", False),
        "topic_id": (sol.get("topic") or {}).get("id"),
        "tags": [t.get("name") for t in
                 ((sol.get("topic") or {}).get("solutionTags") or [])],
    }
# ── 路径②：拉题解列表 ──
Q_SOLUTION_TOPICS = """
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

def fetch_solution_list(
    slug: str,
    domain: str = "leetcode.cn",
    first: int = 10,
    skip: int = 0,
    order_by: str = "MOST_RECENT",
) -> list[dict]:
    """拉取某题的题解列表（官方通常在第一条）。"""
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


# ── 路径③：拉单个题解正文 ──
Q_SOLUTION_DETAIL = """
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
    """根据题解 slug 拉取完整正文。"""
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


# ── 测试入口 ──
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("slug", help="题目 slug，如 two-sum")
    parser.add_argument("--domain", default="leetcode.cn",
                        choices=["leetcode.cn", "leetcode.com"])
    parser.add_argument("--mode", default="official",
                        choices=["official", "list", "detail"],
                        help="official=只取官方题解；list=拉题解列表；detail=需配 --solution-slug")
    parser.add_argument("--solution-slug", default=None,
                        help="mode=detail 时必填")
    args = parser.parse_args()

    if args.mode == "official":
        sol = fetch_official_solution(args.slug, args.domain)
        print(json.dumps(sol, ensure_ascii=False, indent=2) if sol else "无官方题解")
    elif args.mode == "list":
        lst = fetch_solution_list(args.slug, args.domain)
        for i, s in enumerate(lst, 1):
            print(f"{i}. {s['title']}  [{s['slug']}]")
            print(f"   {s['url']}")
    elif args.mode == "detail":
        if not args.solution_slug:
            raise SystemExit("mode=detail 需要 --solution-slug")
        d = fetch_solution_detail(args.solution_slug, args.slug, args.domain)
        print(json.dumps(d, ensure_ascii=False, indent=2))
