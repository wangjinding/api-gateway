"""活体检查：端到端验证（需网关已启动 + 真实网关键）。

用法: python live_check.py --key <wjd-xxx> [--url http://127.0.0.1:8808]
网关已开鉴权，所有端点带 Authorization: Bearer <key>。

第二十七轮（2026-09-18）：路由别名 wjd 已整体删除 → 本脚本里的模型一律用**真实模型 id**，
并新增「三线直传」与「跨线 400」检查（chat/completions · messages · responses 各按自家协议直传）。
"""
from __future__ import annotations

import argparse
import sys

import httpx

PASS, FAIL, WARN = [], [], []

# 第二十七轮: 三线直传用到的真实模型 id
CHAT_MODEL = "agnes-3.0-flash"        # 只声明 chat/completions 线
THREE_LINE_MODEL = "deepseek-v4.1-flash"   # 实测三线全通（chat/completions · messages · responses）


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"[{'PASS' if cond else 'FAIL'}] {name}  {detail}")


def check_or_warn(name: str, cond: bool, detail: str = "") -> None:
    """上游侧的结果不定（模型/额度/协议细节），不算硬失败 —— 但网关侧的闸门必须硬判。"""
    if cond:
        PASS.append(name)
        print(f"[PASS] {name}  {detail}")
    else:
        warn(name, detail)


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

        # 2) 真实 chat（第二十七轮: 不再有路由别名 —— model 就是真实模型 id，
        #    网关不替用户挑模型，served_model 必须与请求的 id 一致）
        r = c.post(f"{base}/v1/chat/completions", json={
            "model": CHAT_MODEL, "messages": [{"role": "user", "content": "回复：OK"}], "max_tokens": 12})
        ok = r.status_code == 200 and len(r.json().get("choices", [])) > 0
        served = r.json().get("served_model") if ok else None
        check(f"POST chat 真实模型 {CHAT_MODEL}", ok, f"{r.status_code} served={served}")
        check("served_model == 请求的 model（不再有 fallback 换模型）", served == CHAT_MODEL,
              f"served={served}")

        # 3) 未知模型 → 400（Bug 15）
        r = c.post(f"{base}/v1/chat/completions", json={
            "model": "__nope__", "messages": [{"role": "user", "content": "x"}]})
        check("未知模型 → 400（Bug 15）", r.status_code == 400, f"{r.status_code}")

        # 4) /metrics：模型维度在记账；路由维度已随路由功能删除（不该再出现）
        r = c.get(f"{base}/metrics")
        j = r.json() if r.status_code == 200 else {}
        mine = (j.get("calls") or {}).get(CHAT_MODEL) or {}
        check("GET /metrics 已启用", r.status_code == 200, f"{r.status_code}")
        check(f"calls.{CHAT_MODEL}.ok ≥ 1（模型维度记账）", mine.get("ok", 0) >= 1, f"{mine}")
        check("路由维度已删除（无 routes / fallback_total）",
              "routes" not in j and "fallback_total" not in j, f"keys={sorted(j)[:8]}…")

        # 5) 流式 SSE（真实模型）
        body = ""
        with c.stream("POST", f"{base}/v1/chat/completions", json={
                "model": CHAT_MODEL, "messages": [{"role": "user", "content": "数到三"}],
                "stream": True, "max_tokens": 16}) as sr:
            if sr.status_code == 200:
                body = "".join(sr.iter_text())
        check("流式 SSE 且以 [DONE] 收尾", sr.status_code == 200 and "[DONE]" in body,
              f"{sr.status_code} len={len(body)}")

        # 6) 流式也计入模型维度指标
        r = c.get(f"{base}/metrics")
        mine2 = ((r.json().get("calls") or {}).get(CHAT_MODEL) or {}) if r.status_code == 200 else {}
        check("流式也计入 calls（Bug 8 补充）", mine2.get("ok", 0) > mine.get("ok", 0),
              f"before={mine.get('ok',0)} after={mine2.get('ok',0)}")

        # 7) 三线直传（第二十七轮新增）
        #   7a) messages 线：Anthropic 协议原样直传
        r = c.post(f"{base}/v1/messages", json={
            "model": THREE_LINE_MODEL, "max_tokens": 16,
            "messages": [{"role": "user", "content": "回复：OK"}]})
        check_or_warn(f"POST /v1/messages 直传（{THREE_LINE_MODEL}）", r.status_code == 200,
                      f"{r.status_code} {str(r.text)[:120]}")
        #   7b) responses 线：OpenAI Responses 协议原样直传
        r = c.post(f"{base}/v1/responses", json={
            "model": THREE_LINE_MODEL, "input": "回复：OK", "max_tokens": 16})
        check_or_warn(f"POST /v1/responses 直传（{THREE_LINE_MODEL}）", r.status_code == 200,
                      f"{r.status_code} {str(r.text)[:120]}")
        #   7c) 跨线闸门：只支持 chat/completions 的模型打 /v1/messages → 必须 400
        #        （上游对错线请求回的是 500/401 这类跟"格式选错"无关的错误，网关必须在入口挡住）
        r = c.post(f"{base}/v1/messages", json={
            "model": CHAT_MODEL, "max_tokens": 16,
            "messages": [{"role": "user", "content": "x"}]})
        detail = str(r.text)[:160]
        check(f"跨线请求 → 400（{CHAT_MODEL} 不支持 messages 线）",
              r.status_code == 400 and "messages" in detail, f"{r.status_code} {detail}")

        # 8) Prometheus 指标
        r = c.get(f"{base}/metrics/prometheus")
        check("GET /metrics/prometheus", r.status_code == 200 and "api_gateway" in r.text, f"{r.status_code}")

        # 9) 访问日志
        r = c.get(f"{base}/logs?n=5")
        check("GET /logs", r.status_code == 200 and isinstance(r.json().get("entries"), list), f"{r.status_code}")

        # 10) provider 健康（可达性 = 2xx，Bug 11）
        r = c.get(f"{base}/health/agnes")
        j = r.json() if r.status_code == 200 else {}
        check("GET /health/agnes reachable(2xx)", r.status_code == 200 and j.get("reachable") is True,
              f"{r.status_code} reachable={j.get('reachable')} code={j.get('status_code')}")

        # 11) 动态 CORS（Bug 6）：预检 + 通配来源
        r = c.options(f"{base}/v1/chat/completions", headers={
            "Origin": "https://demo.example", "Access-Control-Request-Method": "POST"})
        check("CORS 预检 200 且回 ACAO", r.status_code == 200 and
              r.headers.get("access-control-allow-origin") in ("*", "https://demo.example"),
              f"{r.status_code} ACAO={r.headers.get('access-control-allow-origin')}")

        # 12) 413 体积限制
        r = c.post(f"{base}/v1/chat/completions", content=b"x" * 3_000_000,
                   headers={"Content-Type": "application/json"})
        check("超大请求体 → 413", r.status_code == 413, f"{r.status_code}")

        # 13) batch（真实模型）
        r = c.post(f"{base}/v1/chat/completions/batch", json={
            "model": CHAT_MODEL, "max_concurrency": 2,
            "items": [{"messages": [{"role": "user", "content": "说 hi"}], "label": "a"},
                      {"messages": [{"role": "user", "content": "说 yo"}], "label": "b"}]})
        ok = r.status_code == 200 and r.json().get("count") == 2 and all(x["ok"] for x in r.json()["results"])
        check("POST batch 2 项全成功", ok, f"{r.status_code}")

        # 14) 图像（真实，可能较慢 / 上游不支持则 WARN）
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
