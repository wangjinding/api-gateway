"""活体检查：端到端验证（需网关已启动 + 真实网关键）。

用法: python live_check.py --key <wjd-xxx> [--url http://127.0.0.1:8808]
网关已开鉴权，所有端点带 Authorization: Bearer <key>。
"""
from __future__ import annotations

import argparse
import sys

import httpx

PASS, FAIL, WARN = [], [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"[{'PASS' if cond else 'FAIL'}] {name}  {detail}")


def warn(name: str, detail: str = "") -> None:
    WARN.append(name)
    print(f"[WARN] {name}  {detail}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8808")
    ap.add_argument("--key", default=None, help="网关键（Bearer）")
    a = ap.parse_args()
    base = a.url
    hdr = {"Authorization": f"Bearer {a.key}"} if a.key else {}

    with httpx.Client(timeout=90, headers=hdr) as c:
        # 1) 根导航
        r = c.get(f"{base}/")
        j = r.json() if r.status_code == 200 else {}
        check("GET / 根导航", r.status_code == 200 and "endpoints" in j, f"{r.status_code}")

        # 2) 走路由别名 wjd 的真实 chat（验证 route 维度指标）
        r = c.post(f"{base}/v1/chat/completions", json={
            "model": "wjd", "messages": [{"role": "user", "content": "回复：OK"}], "max_tokens": 12})
        ok = r.status_code == 200 and len(r.json().get("choices", [])) > 0
        served = r.json().get("served_model") if ok else None
        check("POST chat 走路由 wjd", ok, f"{r.status_code} served={served}")

        # 3) 未知模型 → 400（Bug 15）
        r = c.post(f"{base}/v1/chat/completions", json={
            "model": "__nope__", "messages": [{"role": "user", "content": "x"}]})
        check("未知模型 → 400（Bug 15）", r.status_code == 400, f"{r.status_code}")

        # 4) /metrics 里 routes.wjd 的 ok 应 ≥1（Bug 8：route 维度不再恒 0）
        r = c.get(f"{base}/metrics")
        j = r.json() if r.status_code == 200 else {}
        wjd = (j.get("routes") or {}).get("wjd") or {}
        check("GET /metrics 已启用", r.status_code == 200, f"{r.status_code}")
        check("routes.wjd.ok ≥ 1（Bug 8）", wjd.get("ok", 0) >= 1, f"wjd={wjd}")

        # 5) 流式 SSE（走 wjd）
        body = ""
        with c.stream("POST", f"{base}/v1/chat/completions", json={
                "model": "wjd", "messages": [{"role": "user", "content": "数到三"}],
                "stream": True, "max_tokens": 16}) as sr:
            if sr.status_code == 200:
                body = "".join(sr.iter_text())
        check("流式 SSE 走 wjd 且以 [DONE] 收尾", sr.status_code == 200 and "[DONE]" in body,
              f"{sr.status_code} len={len(body)}")

        # 6) 流式后 routes.wjd.ok 继续累加（流式也记 route）
        r = c.get(f"{base}/metrics")
        wjd2 = ((r.json().get("routes") or {}).get("wjd") or {}) if r.status_code == 200 else {}
        check("流式也计入 routes.wjd（Bug 8 补充）", wjd2.get("ok", 0) > wjd.get("ok", 0),
              f"before={wjd.get('ok',0)} after={wjd2.get('ok',0)}")

        # 7) Prometheus 指标
        r = c.get(f"{base}/metrics/prometheus")
        check("GET /metrics/prometheus", r.status_code == 200 and "api_gateway" in r.text, f"{r.status_code}")

        # 8) 访问日志
        r = c.get(f"{base}/logs?n=5")
        check("GET /logs", r.status_code == 200 and isinstance(r.json().get("entries"), list), f"{r.status_code}")

        # 9) provider 健康（可达性 = 2xx，Bug 11）
        r = c.get(f"{base}/health/agnes")
        j = r.json() if r.status_code == 200 else {}
        check("GET /health/agnes reachable(2xx)", r.status_code == 200 and j.get("reachable") is True,
              f"{r.status_code} reachable={j.get('reachable')} code={j.get('status_code')}")

        # 10) 动态 CORS（Bug 6）：预检 + 通配来源
        r = c.options(f"{base}/v1/chat/completions", headers={
            "Origin": "https://demo.example", "Access-Control-Request-Method": "POST"})
        check("CORS 预检 200 且回 ACAO", r.status_code == 200 and
              r.headers.get("access-control-allow-origin") in ("*", "https://demo.example"),
              f"{r.status_code} ACAO={r.headers.get('access-control-allow-origin')}")

        # 11) 413 体积限制
        r = c.post(f"{base}/v1/chat/completions", content=b"x" * 3_000_000,
                   headers={"Content-Type": "application/json"})
        check("超大请求体 → 413", r.status_code == 413, f"{r.status_code}")

        # 12) batch（真实）
        r = c.post(f"{base}/v1/chat/completions/batch", json={
            "model": "wjd", "max_concurrency": 2,
            "items": [{"messages": [{"role": "user", "content": "说 hi"}], "label": "a"},
                      {"messages": [{"role": "user", "content": "说 yo"}], "label": "b"}]})
        ok = r.status_code == 200 and r.json().get("count") == 2 and all(x["ok"] for x in r.json()["results"])
        check("POST batch 2 项全成功", ok, f"{r.status_code}")

        # 13) 图像（真实，可能较慢 / 上游不支持则 WARN）
        try:
            r = c.post(f"{base}/v1/images/generations", json={
                "model": "agnes-image-2.5-flash", "prompt": "a plain red square", "size": "1K"}, timeout=120)
            if r.status_code == 200 and r.json().get("data"):
                check("POST 图像生成", True, f"{r.status_code} n={len(r.json()['data'])}")
            else:
                warn("POST 图像生成", f"{r.status_code} {str(r.text)[:120]}")
        except Exception as e:
            warn("POST 图像生成", f"异常 {e}")

    print(f"\n结果: {len(PASS)} PASS / {len(FAIL)} FAIL / {len(WARN)} WARN")
    if FAIL:
        print("失败项: " + ", ".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
