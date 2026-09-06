"""部署冒烟脚本：对已部署环境（docker compose / 云服务器）打核心端点。

用法：
    python scripts/deploy_smoke.py --base http://YOUR_SERVER:3000 \
        --email 534629255@qq.com --password test123456

不启动任何服务（假设 compose 已 up），只做只读探测 + 一次真实登录：
  1. GET  /health
  2. POST /auth/login（拿 JWT）
  3. GET  /problems（鉴权）
  4. GET  /session/list（鉴权）
  5. GET  /auth/me/profile/v2（鉴权）
  6. 前端首页 GET /（应返回 SPA HTML，验证 nginx/静态服务）

退出码：0=全部通过，1=有失败（可直接接 CI / 部署后自检）。
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

results: list[tuple[str, bool, str]] = []


def _get(url: str, token: str | None = None, timeout: float = 10.0):
    req = urllib.request.Request(url, headers={"accept": "application/json"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read()


def record(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, ok, detail))
    print(f"{'✅' if ok else '❌'} {name}" + (f" — {detail}" if detail else ""))
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description="部署冒烟检测")
    ap.add_argument("--base", required=True, help="环境根地址，如 http://1.2.3.4:3000")
    ap.add_argument("--email", required=True)
    ap.add_argument("--password", required=True)
    args = ap.parse_args()
    base = args.base.rstrip("/")

    # 1. 前端 SPA（nginx / 静态服务）
    try:
        status, body = _get(base + "/", timeout=15.0)
        html = body.decode("utf-8", "replace")
        record("前端首页", status == 200 and "<div id=" in html, f"status={status}")
    except Exception as exc:
        record("前端首页", False, str(exc)[:120])

    # 2. /health
    try:
        status, body = _get(base + "/health")
        record("后端 /health", status == 200, body.decode("utf-8", "replace")[:60])
    except Exception as exc:
        record("后端 /health", False, str(exc)[:120])
        return _summary()  # 后端不可达时后续必挂，直接收尾

    # 3. 登录
    token = ""
    try:
        req = urllib.request.Request(
            base + "/auth/login",
            data=json.dumps({"email": args.email, "password": args.password}).encode(),
            headers={"content-type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10.0) as resp:
            data = json.loads(resp.read())
            token = data.get("token", "")
        record("登录", bool(token), "JWT 已获取" if token else "响应无 token")
    except urllib.error.HTTPError as exc:
        record("登录", False, f"HTTP {exc.code}（检查账号/邀请码体系是否正常）")
    except Exception as exc:
        record("登录", False, str(exc)[:120])

    # 4-6. 鉴权端点
    if token:
        for path, name in [
            ("/problems", "题库列表"),
            ("/session/list", "会话列表"),
            ("/auth/me/profile/v2", "画像 v2"),
        ]:
            try:
                status, _body = _get(base + path, token=token)
                record(name, status == 200, f"status={status}")
            except Exception as exc:
                record(name, False, str(exc)[:120])
    else:
        for name in ("题库列表", "会话列表", "画像 v2"):
            record(name, False, "无 token，跳过")

    return _summary()


def _summary() -> int:
    failed = [r for r in results if not r[1]]
    print(f"\n=== 冒烟结果: {len(results) - len(failed)}/{len(results)} 通过 ===")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
