# 一行拿官方题解
from code_tutor_agent.leetcode.leetcode_solution_fetcher import fetch_official_solution, fetch_solution_list, \
    fetch_solution_detail

sol = fetch_official_solution("two-sum", "leetcode.cn")
print(sol["content_text"])

# 先列表、再逐条详情
for item in fetch_solution_list("two-sum", first=5):
    detail = fetch_solution_detail(item["slug"], "two-sum")
    print(detail["title"], len(detail["content_text"]))
