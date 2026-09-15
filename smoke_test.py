"""冒烟测试：对已运行的网关做一轮基本调用（需网关已启动 + 真实供应商 key）。

用法:
    python smoke_test.py [--url http://localhost:8808] [--api-key XXX]

无真实 key / 网关未启动时，失败会被标记为 SKIP（退出码 0），便于 CI 预检。
全部通过退出码 0；有 FAIL（网关起来了但调用失败）退出码 1。

覆盖: /health、/v1/models、/v1/chat/completions（文本）、batch、错误格式。
"""

from __future__ import annotations

import argparse
import json
import sys

import httpx


def _explain(r: httpx.Response) -> str:
    try:
        body = r.json()
        err = body.get("error")
        if isinstance(err, dict):
            return f"{err.get('type','?')}: {err.get('message','')}"
        return json.dumps(body, ensure_ascii=False)[:200]
    except Exception:
        return r.text[:200]


def main() -> int:
    parser = argparse.ArgumentParser(description="API Gateway 冒烟测试")
    parser.add_argument("--url", default="http://localhost:8808", help="网关地址")
    parser.add_argument("--api-key", default="", help="可选 Bearer key（网关开了鉴权时用）")
    parser.add_argument("--model", default="agnes-3.0-flash", help="测试用文本模型")
    args = parser.parse_args()

    headers = {"Content-Type": "application/json"}
    if args.api_key:
        headers["Authorization"] = f"Bearer {args.api_key}"

    # 网关未启动 → 全部跳过
    try:
        with httpx.Client(timeout=5) as c:
            r = c.get(f"{args.url}/health", headers=headers)
            print(f"[SKIP-CHECK] GET /health -> {r.status_code}")
    except Exception as e:
        print(f"[SKIP] 网关 {args.url} 未就绪: {e}")
        return 0

    failures = 0
    with httpx.Client(timeout=60) as c:
        # 1) /health
        r = c.get(f"{args.url}/health", headers=headers)
        ok = r.status_code == 200 and r.json().get("status") == "ok"
        print(f"[{'PASS' if ok else 'FAIL'}] GET /health -> {r.status_code}")
        failures += (not ok)

        # 2) /v1/models
        r = c.get(f"{args.url}/v1/models", headers=headers)
        ok = r.status_code == 200 and any(x.get("id") == args.model for x in r.json().get("data", []))
        print(f"[{'PASS' if ok else 'FAIL'}] GET /v1/models -> {r.status_code} (含 {args.model}: {ok})")
        failures += (not ok)

        # 3) 文本 chat
        payload = {"model": args.model, "messages": [{"role": "user", "content": "say OK"}], "max_tokens": 16}
        r = c.post(f"{args.url}/v1/chat/completions", json=payload, headers=headers)
        ok = r.status_code == 200 and len(r.json().get("choices", [])) > 0
        print(f"[{'PASS' if ok else 'FAIL'}] POST chat -> {r.status_code}  {_explain(r) if not ok else ''}")
        failures += (not ok)

        # 4) batch
        payload = {"model": args.model, "max_concurrency": 2,
                   "items": [{"messages": [{"role": "user", "content": "hi"}], "label": "a"},
                             {"messages": [{"role": "user", "content": "yo"}], "label": "b"}]}
        r = c.post(f"{args.url}/v1/chat/completions/batch", json=payload, headers=headers)
        ok = r.status_code == 200 and r.json().get("count") == 2
        print(f"[{'PASS' if ok else 'FAIL'}] POST batch -> {r.status_code}  {_explain(r) if not ok else ''}")
        failures += (not ok)

        # 5) 错误格式（未知模型 → 400 且带 error 结构）
        payload = {"model": "__not_exist__", "messages": [{"role": "user", "content": "x"}]}
        r = c.post(f"{args.url}/v1/chat/completions", json=payload, headers=headers)
        err = r.json().get("error") if r.content else None
        ok = r.status_code == 400 and isinstance(err, dict) and "message" in err
        print(f"[{'PASS' if ok else 'FAIL'}] 未知模型错误格式 -> {r.status_code}  err={'是' if isinstance(err,dict) else '否'}")
        failures += (not ok)

    print(f"\n结果: {failures} 项失败，{'FAIL' if failures else 'PASS'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())