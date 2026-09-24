#!/usr/bin/env python3
"""模型清单同步：把 data/gateway.yaml 的模型清单和官方文档对齐。

用途：每天定时抓官方文档，与网关配置比对并报告漂移；带 --apply 时**直接落盘同步**。
（老板 2026-09-24 定：文档新增的加进去、下线的删掉、参数变了就更新，网关要和文档同步。）

数据源（都是官方页面，没有第三方）：
  · OpenCode Go  https://opencode.ai/v2/docs/console/go/
      末尾 `## Endpoints` 表 → 模型 id + 官方协议线（chat/completions | messages | responses）
      价格表 → 免费模型识别（全 Free 的那些）
  · Agnes        https://www.agnes-ai.cn/zh-Hans/docs/pricing （Mintlify 的 .md 版）
      文本 / 图片 / 视频 三段 → 模型 id + 现价（取「现价」列，不是划掉的刊例价）

落盘边界（--apply）：
  · 文档新增的模型   → 插到该 provider 模型列表末尾，带「自动同步」注释
  · 文档已下线的模型 → 删除（连它的 pricing 条目一起删）
  · 价格 / 免费标记变化 → 就地更新
  · 协议线只加不砍：文档给的新线会补进去，网关逐条实测出来的更宽的线不动
  · PROTECTED 名单 + 被别名 / media 引用的 id → **永不自动删**，只在报告里点名
    （官方文档表有收录滞后：实测 deepseek-flash / omen-alpha 上游在服务、文档表还没补，
     照文档删等于把能用的模型删掉）

写盘走网关自带的 write_config（备份 + 原子替换 + 权限 600），落盘前后回读校验，
校验不过就中止、不写盘。改的是**文本层**，手写注释不会被整份重渲染冲掉。

输出：默认人读报告；--json 给程序用。
退出码：0 = 无漂移（或已同步成功）；1 = 有漂移但没落盘（只报告模式）；2 = 抓取/解析/写盘失败。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CFG = ROOT / "data" / "gateway.yaml"

UA = "api-gateway-model-sync/1.0 (+ops; contact admin)"
OC_DOC_URL = "https://opencode.ai/v2/docs/console/go/"
AGNES_PRICING_URL = "https://www.agnes-ai.cn/zh-Hans/docs/pricing.md"

# Agnes 文档段标题 → 网关 models[].type
AGNES_SECTION_TYPE = {"文本模型": "text", "图片模型": "image", "视频模型": "video"}

# 同步的两个供应商 → 各自「文档里的模型清单」从哪来
SYNC_PROVIDERS = ("opencode", "agnes")

# 永不自动删的名单：官方文档表**收录滞后**，实测上游还在服务的模型。
#   删了 = 白丢一个能用的模型，所以列在这里；每次报告都会点名，要删说一声就删。
PROTECTED: dict[str, dict[str, str]] = {
    "opencode": {
        "deepseek-flash": "2026-09-21 上游新出现、文档表未收；实测 chat/messages/responses 三线全通",
        "omen-alpha": "上游新模型、文档表未收；实测 chat 线可用",
    },
}


def _get(url: str, timeout: int = 30) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def _strip_tags(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", s)
    return s.replace("`", "").strip()


def _norm(s: str) -> str:
    """把展示名归一成可比较的键（去括号后缀/空格/连字符）。"""
    s = re.sub(r"\(.*?\)", "", s)
    return re.sub(r"[^a-z0-9]", "", s.lower())


# ── OpenCode Go ────────────────────────────────────────────────
def fetch_opencode() -> dict[str, dict]:
    """返回 {model_id: {name, protocol, free}}。"""
    html = _get(OC_DOC_URL)
    models: dict[str, dict] = {}
    for row in re.findall(r"<tr>(.*?)</tr>", html, re.S):
        cells = [_strip_tags(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
        if len(cells) < 4 or "/zen/go/v1/" not in cells[2]:
            continue
        tail = cells[2].split("/zen/go/v1/", 1)[1].strip("/").split("?")[0]
        models[cells[1]] = {"name": cells[0], "protocol": tail, "free": False}
    if not models:
        raise RuntimeError("OpenCode 文档没解析出 Endpoints 表（页面可能改版）")

    # 免费模型：价格表里 Input/Output 都是 Free 的那些（名字对 id 做归一匹配）
    by_norm = {_norm(mid): mid for mid in models}
    for row in re.findall(r"<tr>(.*?)</tr>", html, re.S):
        cells = [_strip_tags(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
        if len(cells) >= 3 and cells[1].lower() == "free":
            mid = by_norm.get(_norm(cells[0]))
            if mid:
                models[mid]["free"] = True
    return models


# ── Agnes ──────────────────────────────────────────────────────
_MODEL_ID_RE = re.compile(r"[a-z][a-z0-9.\-]*[0-9][a-z0-9.\-]*")


def _price_of(cell: str) -> float | None:
    m = re.search(r"¥([0-9.]+)", cell)
    return float(m.group(1)) if m else None


def fetch_agnes() -> dict[str, dict]:
    """返回 {model_id: {section, type, input, output}}（input/output = 现价，¥/M，仅文本模型有）。

    页面是 JSX 风格的 HTML 表：模型列有的带 rowSpan（一行跨多行计费项）、有的单行自带，
    所以按 `<tr>` 块解析，**第一格是模型 id 就认模型行，否则归到上一个模型**。
    """
    md = _get(AGNES_PRICING_URL)
    out: dict[str, dict] = {}
    # 按 ## / ### 标题切块，只在「文本/图片/视频模型」三块里找
    parts = re.split(r"^(#{2,3}\s+.+?)\s*$", md, flags=re.M)
    for i in range(1, len(parts) - 1, 2):
        title = parts[i].lstrip("#").strip()
        body = parts[i + 1]
        if title not in AGNES_SECTION_TYPE:
            continue
        cur: str | None = None
        for row in re.findall(r"<tr>(.*?)</tr>", body, re.S):
            cells = [_strip_tags(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
            if not cells:
                continue
            first = cells[0]
            if _MODEL_ID_RE.fullmatch(first):
                cur = first
                out.setdefault(cur, {"section": title, "type": AGNES_SECTION_TYPE[title],
                                     "input": None, "output": None})
                continue
            if not cur:
                continue
            price = _price_of(cells[-1]) if len(cells) >= 2 else None
            if price is None:
                continue
            if first.startswith("输入 Token"):
                out[cur]["input"] = price
            elif first.startswith("输出 Token"):
                out[cur]["output"] = price
    if not out:
        raise RuntimeError("Agnes 定价页没解析出模型（页面可能改版）")
    return out


# ── 比对 ───────────────────────────────────────────────────────
def _references(cfg: dict) -> dict[str, str]:
    """被别名 / media 指向的模型 id → 引用原因。删这些会直接打断功能，故永不自动删。"""
    refs: dict[str, str] = {}
    for name, alias in (cfg.get("aliases") or {}).items():
        target = alias.get("target") if isinstance(alias, dict) else None
        if target:
            refs[target] = f"别名 {name} 的目标"
    for prov, pcfg in (cfg.get("providers") or {}).items():
        for key, val in ((pcfg or {}).get("media") or {}).items():
            if isinstance(val, str) and val:
                refs[val] = f"{prov}.media.{key}"
    return refs


def _split_removed(prov: str, cur: dict, doc: dict, refs: dict) -> tuple[list[str], dict[str, str]]:
    """「网关有、文档没收录」的模型 → (该删的, 该保的 {id: 原因})。

    文档没收录 ≠ 上游下线（实测有 deepseek-flash / omen-alpha 这种上游在服务、文档表
    还没补的），所以保护名单和被引用的 id 一律不删，只在报告里点名。
    """
    guard = PROTECTED.get(prov, {})
    drop: list[str] = []
    keep: dict[str, str] = {}
    for mid in sorted(set(cur) - set(doc)):
        if mid in guard:
            keep[mid] = f"保护名单（{guard[mid]}）"
        elif mid in refs:
            keep[mid] = f"被引用：{refs[mid]} —— 删了会打断功能"
        else:
            drop.append(mid)
    return drop, keep


def diff(cfg: dict, oc_doc: dict, agnes_doc: dict) -> dict:
    providers = cfg.get("providers", {})
    pricing = (cfg.get("pricing") or {}).get("per_model") or {}
    refs = _references(cfg)
    report: dict[str, dict] = {}

    # OpenCode
    oc_cur = {m["id"]: m for m in providers.get("opencode", {}).get("models", [])}
    proto_drift = []
    for mid, meta in sorted(oc_doc.items()):
        if mid not in oc_cur:
            continue
        lines = oc_cur[mid].get("protocols") or ["chat/completions"]
        if meta["protocol"] not in lines:
            proto_drift.append({"id": mid, "doc": meta["protocol"], "cfg": lines})
    free_drift = [{"id": mid, "doc_free": meta["free"], "cfg_free": bool(oc_cur[mid].get("free"))}
                  for mid, meta in sorted(oc_doc.items())
                  if mid in oc_cur and meta["free"] != bool(oc_cur[mid].get("free"))]
    oc_drop, oc_keep = _split_removed("opencode", oc_cur, oc_doc, refs)
    report["opencode"] = {
        "doc_count": len(oc_doc), "cfg_count": len(oc_cur),
        "added": [{"id": k, "name": v["name"], "protocol": v["protocol"], "free": v["free"]}
                  for k, v in sorted(oc_doc.items()) if k not in oc_cur],
        "removed": oc_drop,
        "protected": oc_keep,
        "protocol_drift": proto_drift,
        "free_drift": free_drift,
    }

    # Agnes
    ag_cur = {m["id"]: m for m in providers.get("agnes", {}).get("models", [])}
    price_drift = []
    for mid, meta in sorted(agnes_doc.items()):
        if meta["input"] is None:                     # 图片/视频模型文档没给单价
            continue
        want = {"input": meta["input"], "output": meta["output"]}
        have = pricing.get(mid)
        if have != want:
            price_drift.append({"id": mid, "cfg": have, "doc": want})
    ag_drop, ag_keep = _split_removed("agnes", ag_cur, agnes_doc, refs)
    report["agnes"] = {
        "doc_count": len(agnes_doc), "cfg_count": len(ag_cur),
        "added": [{"id": k, "section": v["section"], "type": v["type"],
                   "display_name": pretty_name(k), "input": v["input"], "output": v["output"]}
                  for k, v in sorted(agnes_doc.items()) if k not in ag_cur],
        "removed": ag_drop,
        "protected": ag_keep,
        "price_drift": price_drift,
    }
    return report


def pretty_name(mid: str) -> str:
    """模型 id → 展示名（文档没给展示名时用）：agnes-image-2.0-flash → Agnes Image 2.0 Flash"""
    return " ".join(p[:1].upper() + p[1:] for p in mid.split("-"))


def render(report: dict) -> str:
    lines: list[str] = []
    oc, ag = report["opencode"], report["agnes"]
    lines.append(f"== OpenCode Go ==  文档 {oc['doc_count']} 个 / 网关 {oc['cfg_count']} 个")
    if oc["added"]:
        lines.append("  新增（文档有、网关没有）→ 落盘补进配置：")
        lines += [f"    + {m['id']}  [{m['protocol']}]  {m['name']}" for m in oc["added"]]
    if oc["removed"]:
        lines.append("  下线（文档已不收录）→ 落盘删除：")
        lines += [f"    - {m}" for m in oc["removed"]]
    if oc.get("protected"):
        lines.append("  文档未收录但**受保护不删**（要删请说一声）：")
        lines += [f"    · {m} —— {why}" for m, why in oc["protected"].items()]
    if oc["protocol_drift"]:
        lines.append("  协议线漂移（文档的线网关没有 → 落盘时补上，现有线不砍）：")
        lines += [f"    ~ {d['id']}  文档={d['doc']}  网关={','.join(d['cfg'])}" for d in oc["protocol_drift"]]
    if oc["free_drift"]:
        lines.append("  免费标记漂移 → 落盘时更新：")
        lines += [f"    ~ {d['id']}  文档={d['doc_free']}  网关={d['cfg_free']}" for d in oc["free_drift"]]
    if not any(oc[k] for k in ("added", "removed", "protocol_drift", "free_drift")):
        lines.append("  无漂移 ✓")

    lines.append("")
    lines.append(f"== Agnes ==  文档 {ag['doc_count']} 个 / 网关 {ag['cfg_count']} 个")
    if ag["added"]:
        lines.append("  新增（文档有、网关没有）→ 落盘补进配置：")
        lines += [f"    + {m['id']}  ({m['section']} / {m['type']})" for m in ag["added"]]
    if ag["removed"]:
        lines.append("  下线（文档已不收录）→ 落盘删除：")
        lines += [f"    - {m}" for m in ag["removed"]]
    if ag.get("protected"):
        lines.append("  文档未收录但**受保护不删**（要删请说一声）：")
        lines += [f"    · {m} —— {why}" for m, why in ag["protected"].items()]
    if ag["price_drift"]:
        lines.append("  定价漂移（网关 pricing.per_model vs 文档现价，¥/M）→ 落盘更新：")
        for d in ag["price_drift"]:
            lines.append(f"    ~ {d['id']}  网关={d['cfg']}  文档={d['doc']}")
    if not any(ag[k] for k in ("added", "removed", "price_drift")):
        lines.append("  无漂移 ✓")
    return "\n".join(lines)


_DRIFT_KEYS = ("added", "removed", "protocol_drift", "free_drift", "price_drift")


def has_drift(report: dict) -> bool:
    """有没有需要落盘的差异。

    `protected`（文档没收录但受保护不删的那些）**故意不算**漂移 —— 它们永远不会被
    自动改，算进去等于每天推一条同样的提示，看久了就没人看了。
    """
    return any(report[p].get(k) for p in report for k in _DRIFT_KEYS)


# ── 落盘（--apply）：文本级增 / 删 / 改 ─────────────────────────
# 为什么不整份重渲染：data/gateway.yaml 里有大量**逐条实测**的手写注释（哪条线实测通了、
# 哪个模型上游有文档没收录……），整份 dump 会把它们全冲掉。所以这里只动需要动的行。
_MODEL_ID_LINE = re.compile(r'^      - id:\s*"?(?P<id>[^"\s]+)"?\s*$')
_SECTION_LINE = re.compile(r"^(?:  [A-Za-z_][A-Za-z0-9_]*:|[A-Za-z_])")
_PRICING_ANY = re.compile(r"^    (?P<mid>[A-Za-z0-9._\-]+):")
_PRICING_LINE = re.compile(
    r"^(?P<ind>    )(?P<mid>[A-Za-z0-9._\-]+):(?P<pad>\s+)"
    r"\{ input: (?P<in>[0-9.]+), output: (?P<out>[0-9.]+) \}(?P<tail>.*)$"
)
PRICE_COL = 26          # per_model 里 `{` 的起始列（跟现网对齐风格一致）


def _find_model_blocks(lines: list[str], provider: str) -> dict[str, tuple[int, int]]:
    """定位 <provider>.models 下每个模型块的行区间 [start, end)（半开）。

    一个块 = 从 `      - id: "x"` 到下一个模型块 / 下一个段（2 空格缩进的 key）/
    文件尾。块尾的空行不算进块里（留给下一段）。
    """
    def _find(pattern: str, lo: int) -> int | None:
        for i in range(lo, len(lines)):
            if re.fullmatch(pattern, lines[i]):
                return i
        return None

    p = _find(rf"  {re.escape(provider)}:\s*", 0)
    if p is None:
        return {}
    m = _find(r"    models:\s*", p + 1)
    if m is None:
        return {}

    starts: dict[str, int] = {}
    order: list[str] = []
    stop = len(lines)
    for i in range(m + 1, len(lines)):
        if _SECTION_LINE.match(lines[i]):
            stop = i
            break
        hit = _MODEL_ID_LINE.match(lines[i])
        if hit:
            starts[hit.group("id")] = i
            order.append(hit.group("id"))

    blocks: dict[str, tuple[int, int]] = {}
    for n, mid in enumerate(order):
        start = starts[mid]
        end = starts[order[n + 1]] if n + 1 < len(order) else stop
        while end > start and not lines[end - 1].strip():
            end -= 1
        blocks[mid] = (start, end)
    return blocks


def _pricing_span(lines: list[str]) -> tuple[int, int] | None:
    """定位 pricing.per_model 段内条目行的区间 [start, end)（半开）。"""
    def _find(pattern: str, lo: int) -> int | None:
        for i in range(lo, len(lines)):
            if re.fullmatch(pattern, lines[i]):
                return i
        return None

    p = _find(r"pricing:\s*", 0)
    if p is None:
        return None
    m = _find(r"  per_model:\s*", p + 1)
    if m is None:
        return None
    end = m + 1
    for i in range(m + 1, len(lines)):
        if lines[i].strip() and not lines[i].startswith("    "):
            break
        end = i + 1
    # 段尾空行不算进段里：补价目要贴在最后一条价目后面，不能掉到空行下面去
    while end > m + 1 and not lines[end - 1].strip():
        end -= 1
    return m + 1, end


def _insert_after(seg: list[str], pattern: str, newline: str) -> list[str]:
    """把一行插到 seg 里第一条匹配 pattern 的行之后（找不到就原样返回）。"""
    for i, ln in enumerate(seg):
        if re.match(pattern, ln):
            return seg[:i + 1] + [newline] + seg[i + 1:]
    return seg


def _render_model_block(provider: str, mid: str, meta: dict, date: str) -> list[str]:
    """渲染一个新模型块（缩进跟现网一致：`- id:` 6 空格、字段 8 空格）。"""
    out = [f"      # {date} 自动同步：官方文档新增（scripts/sync_models.py）\n",
           f'      - id: "{mid}"\n',
           f'        display_name: "{meta.get("display_name") or pretty_name(mid)}"\n',
           f'        type: "{meta.get("type") or "text"}"\n']
    if provider == "opencode":
        out.append("        # 协议线取自官方 Endpoints 表，未逐线实测（现网其它模型是实测过的）\n")
        out.append(f'        protocols: ["{meta.get("protocol") or "chat/completions"}"]\n')
        if meta.get("free"):
            out.append("        free: true     # 官方价格表标 Free\n")
    elif meta.get("input") is not None:
        out.append(f"        # 文档现价 ¥{meta['input']:g}/M 输入、¥{meta['output']:g}/M 输出\n")
    return out


def _add_protocol(seg: list[str], proto: str, date: str) -> list[str]:
    """把文档声明的一条协议线补进模型块（**只加不砍**）。"""
    for i, ln in enumerate(seg):
        m = re.match(r"^(\s*protocols:\s*)\[(.*?)\](.*)$", ln)
        if not m:
            continue
        items = [x.strip().strip("\"'") for x in m.group(2).split(",") if x.strip()]
        if proto in items:
            return seg
        items.append(proto)
        tail = m.group(3)
        if "#" not in tail:
            tail = f"   # {date} 自动同步：文档声明的线（现网其它线是实测出来的）"
        body = ", ".join(json.dumps(x, ensure_ascii=False) for x in items)
        return seg[:i] + [f"{m.group(1)}[{body}]{tail}\n"] + seg[i + 1:]
    return _insert_after(seg, r"^\s*type:", f'        protocols: ["{proto}"]\n')


def _set_free(seg: list[str], want: bool, date: str) -> list[str]:
    """更新免费标记（文档价格表说全 Free 的模型）。原注释保留，只追加同步说明。"""
    note = f"{date} 自动同步：文档价格表{'标 Free' if want else '已不标 Free'}"
    for i, ln in enumerate(seg):
        if re.match(r"^\s*free:\s*", ln):
            comment = ln.split("#", 1)[1].strip() if "#" in ln else ""
            comment = f"{comment}；{note}" if comment else note
            return seg[:i] + [f"        free: {'true' if want else 'false'}     # {comment}\n"] + seg[i + 1:]
    if want:
        return _insert_after(seg, r"^\s*type:", f"        free: true     # {note}\n")
    return seg


def build_plan(report: dict, cfg: dict, oc_doc: dict, agnes_doc: dict) -> dict:
    """把报告翻成可执行动作（纯计算，不碰磁盘）。"""
    docs = {"opencode": oc_doc, "agnes": agnes_doc}
    pricing = (cfg.get("pricing") or {}).get("per_model") or {}
    plan: dict = {"add": {}, "remove": {}, "price_set": {}, "price_add": {},
                  "proto_add": {}, "free_set": {}}
    for prov in SYNC_PROVIDERS:
        plan["add"][prov] = {m["id"]: m for m in report[prov]["added"]}
        plan["remove"][prov] = list(report[prov]["removed"])
    plan["proto_add"] = {d["id"]: d["doc"] for d in report["opencode"]["protocol_drift"]}
    plan["free_set"] = {d["id"]: d["doc_free"] for d in report["opencode"]["free_drift"]}
    for mid, meta in sorted(agnes_doc.items()):
        if meta["input"] is None:                     # 图片/视频模型文档没给单价
            continue
        want = {"input": meta["input"], "output": meta["output"]}
        if mid in pricing:
            if pricing[mid] != want:
                plan["price_set"][mid] = {"old": pricing[mid], "new": want}
        else:
            plan["price_add"][mid] = want
    return plan


def plan_is_empty(plan: dict) -> bool:
    return not any(plan.get(k) for k in
                   ("add", "remove", "price_set", "price_add", "proto_add", "free_set"))


def render_plan(plan: dict, date: str) -> str:
    lines = [f"-- 落盘动作（{date}）--"]
    for prov in SYNC_PROVIDERS:
        for mid in sorted(plan["add"].get(prov) or {}):
            lines.append(f"  新增 {prov}/{mid}")
        for mid in plan["remove"].get(prov) or []:
            lines.append(f"  删除 {prov}/{mid}")
    for mid, proto in sorted(plan["proto_add"].items()):
        lines.append(f"  补协议线 opencode/{mid} += {proto}")
    for mid, want in sorted(plan["free_set"].items()):
        lines.append(f"  免费标记 opencode/{mid} → {want}")
    for mid, spec in sorted(plan["price_set"].items()):
        lines.append(f"  改价 {mid} {spec['old']} → {spec['new']}")
    for mid, want in sorted(plan["price_add"].items()):
        lines.append(f"  补价 {mid} → {want}")
    if plan_is_empty(plan):
        lines.append("  （无：网关已与文档一致）")
    return "\n".join(lines)


def apply_plan(text: str, plan: dict, date: str) -> str:
    """按 plan 改配置文本。只动该动的行，其余（含注释）原样保留。"""
    lines = text.splitlines(keepends=True)

    # 1) 删除模型块（行号从后往前删，避免下标漂移）
    cuts: list[tuple[int, int]] = []
    for prov in SYNC_PROVIDERS:
        blocks = _find_model_blocks(lines, prov)
        for mid in plan["remove"].get(prov) or []:
            if mid in blocks:
                cuts.append(blocks[mid])
    for s, e in sorted(cuts, reverse=True):
        del lines[s:e]

    # 2) 新增模型块（插在该 provider 最后一个模型块之后）
    for prov in SYNC_PROVIDERS:
        add = plan["add"].get(prov) or {}
        if not add:
            continue
        blocks = _find_model_blocks(lines, prov)
        if not blocks:
            continue
        at = max(e for _, e in blocks.values())
        block: list[str] = []
        for mid, meta in sorted(add.items()):
            block += _render_model_block(prov, mid, meta, date)
        lines[at:at] = block

    # 3) 块内改：补协议线 / 免费标记（按块起点倒序处理，插入不影响未处理块的下标）
    touched: dict[str, set[str]] = {}
    for mid in plan["proto_add"]:
        touched.setdefault("opencode", set()).add(mid)
    for mid in plan["free_set"]:
        touched.setdefault("opencode", set()).add(mid)
    for prov, mids in touched.items():
        blocks = _find_model_blocks(lines, prov)
        for mid in sorted(mids, key=lambda m: blocks.get(m, (0, 0))[0], reverse=True):
            if mid not in blocks:
                continue
            s, e = blocks[mid]
            seg = lines[s:e]
            if mid in plan["proto_add"]:
                seg = _add_protocol(seg, plan["proto_add"][mid], date)
            if mid in plan["free_set"]:
                seg = _set_free(seg, plan["free_set"][mid], date)
            lines[s:e] = seg

    # 4) 定价：改价 / 补价 / 删掉被删模型的价
    gone = {m for prov in SYNC_PROVIDERS for m in plan["remove"].get(prov) or []}
    span = _pricing_span(lines)
    if span and gone:
        s, e = span
        keep = [ln for ln in lines[s:e] if not (m := _PRICING_ANY.match(ln)) or m.group("mid") not in gone]
        lines[s:e] = keep
        span = _pricing_span(lines)
    if span:
        s, e = span
        for i in range(s, e):
            m = _PRICING_LINE.match(lines[i])
            if not m or m.group("mid") not in plan["price_set"]:
                continue
            mid = m.group("mid")
            old, new = plan["price_set"][mid]["old"], plan["price_set"][mid]["new"]
            tail = m.group("tail")
            if "#" not in tail:
                tail = (f"   # 同步 {date}（原 {old['input']:g}/{old['output']:g}；"
                        f"文档现价 ¥{new['input']:g}/M 输入、¥{new['output']:g}/M 输出）")
            lines[i] = (f"{m.group('ind')}{mid}:{m.group('pad')}"
                        f"{{ input: {new['input']:g}, output: {new['output']:g} }}{tail}\n")
        if plan["price_add"]:
            fresh = []
            for mid, want in sorted(plan["price_add"].items()):
                key = f"{mid}:"
                pad = " " * max(1, PRICE_COL - 4 - len(key))
                fresh.append(f"    {key}{pad}{{ input: {want['input']:g}, output: {want['output']:g} }}"
                             f"   # {date} 自动同步（文档现价）\n")
            lines[e:e] = fresh

    return "".join(lines)


def verify(new_text: str, cfg_before: dict, plan: dict) -> list[str]:
    """回读校验：不通过就**不写盘**。宁可这次不同步，也不能把配置写坏。"""
    problems: list[str] = []
    try:
        cfg2 = yaml.safe_load(new_text)
    except Exception as e:                                # noqa: BLE001
        return [f"YAML 解析失败：{type(e).__name__}: {e}"]
    if not isinstance(cfg2, dict):
        return ["YAML 顶层不是映射"]

    for prov in SYNC_PROVIDERS:
        before = {m["id"] for m in (cfg_before.get("providers", {}).get(prov) or {}).get("models", [])}
        after = [m["id"] for m in (cfg2.get("providers", {}).get(prov) or {}).get("models", [])]
        if len(after) != len(set(after)):
            problems.append(f"{prov}: 模型 id 出现重复")
        expect = (before - set(plan["remove"].get(prov) or [])) | set(plan["add"].get(prov) or {})
        if set(after) != expect:
            problems.append(
                f"{prov}: 模型集合不符 期望 {len(expect)} 个 / 实际 {len(set(after))} 个"
                f"（多 {sorted(set(after) - expect)} / 少 {sorted(expect - set(after))}）")

    # 无关 provider 一个都不能碰
    for prov, pcfg in (cfg_before.get("providers") or {}).items():
        if prov in SYNC_PROVIDERS:
            continue
        b = [m["id"] for m in (pcfg or {}).get("models", [])]
        a = [m["id"] for m in ((cfg2.get("providers", {}).get(prov) or {}).get("models", []))]
        if b != a:
            problems.append(f"{prov}: 与本次同步无关，却被改动了")

    # 块内改的字段真写进去了
    oc2 = {m["id"]: m for m in (cfg2.get("providers", {}).get("opencode") or {}).get("models", [])}
    for mid, proto in plan["proto_add"].items():
        lines = oc2.get(mid, {}).get("protocols") or []
        if proto not in lines:
            problems.append(f"协议线 {mid} 没补上（现有 {lines}）")
    for mid, want in plan["free_set"].items():
        if bool(oc2.get(mid, {}).get("free")) != bool(want):
            problems.append(f"免费标记 {mid} 没改成 {want}")

    # 定价
    pr2 = ((cfg2.get("pricing") or {}).get("per_model") or {})
    for mid, spec in list(plan["price_set"].items()):
        got = pr2.get(mid)
        want = spec["new"]
        if not got or float(got["input"]) != want["input"] or float(got["output"]) != want["output"]:
            problems.append(f"定价 {mid} 没写成 {want}（现 {got}）")
    for mid, want in plan["price_add"].items():
        got = pr2.get(mid)
        if not got or float(got["input"]) != want["input"] or float(got["output"]) != want["output"]:
            problems.append(f"定价 {mid} 没补上（现 {got}）")
    gone = {m for prov in SYNC_PROVIDERS for m in plan["remove"].get(prov) or []}
    for mid in gone:
        if mid in pr2:
            problems.append(f"被删模型 {mid} 的定价条目还在")

    # 别名目标不能因为同步消失
    ids_now = {m["id"] for prov in SYNC_PROVIDERS
               for m in (cfg2.get("providers", {}).get(prov) or {}).get("models", [])}
    for name, alias in (cfg_before.get("aliases") or {}).items():
        target = alias.get("target") if isinstance(alias, dict) else None
        if target and target not in ids_now:
            problems.append(f"别名 {name} 的目标 {target} 同步后不存在了")
    return problems


def _write_config_text(path: Path, text: str) -> dict:
    """写盘：复用网关自己的 write_config（备份 + 原子替换 + 权限 600）。

    延迟导入：默认只读路径不碰 app 包，脚本在裸 python 下也能跑起来。
    """
    sys.path.insert(0, str(ROOT))
    from app.settings import write_config              # noqa: PLC0415
    return write_config(path, text)


def _append_log(path: str, text: str, code: int) -> None:
    """把报告追加到日志文件（定时任务用，避免报告随 console 过期丢失）。

    只追加不覆盖；父目录不存在就建；写日志失败不抛异常 —— 不能因为日志问题
    让一次同步任务整体失败（报告还是会打到 stdout）。
    """
    try:
        p = Path(path)
        if p.parent and not p.parent.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with p.open("a", encoding="utf-8") as fh:
            fh.write(f"\n{'=' * 72}\n[{stamp}] exit={code}\n{text}\n")
    except OSError as e:                                     # noqa: BLE001
        print(f"写日志失败（不影响本次结果）：{type(e).__name__}: {e}", file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser(description="模型清单同步（默认只报告，--apply 才写盘）")
    ap.add_argument("--config", default=str(DEFAULT_CFG), help="gateway.yaml 路径")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--log", default=None,
                    help="把报告追加写入该文件（带时间戳）。定时任务用，避免报告丢失")
    ap.add_argument("--apply", action="store_true",
                    help="真的写盘同步（新增 / 删除 / 改参数）。默认只报告；写盘前回读校验，"
                         "校验不过就中止、不落盘")
    ap.add_argument("--date", default=None, help="报告里用的日期（默认当天；仅测试用）")
    args = ap.parse_args()

    date = args.date or time.strftime("%Y-%m-%d")
    cfg_path = Path(args.config)

    try:
        oc_doc = fetch_opencode()
        agnes_doc = fetch_agnes()
    except Exception as e:                                   # noqa: BLE001
        msg = f"抓取/解析失败：{type(e).__name__}: {e}"
        print(msg, file=sys.stderr)
        if args.log:
            _append_log(args.log, msg, 2)
        return 2

    try:
        text_before = cfg_path.read_text(encoding="utf-8")
        cfg = yaml.safe_load(text_before)
    except Exception as e:                                   # noqa: BLE001
        msg = f"读配置失败：{type(e).__name__}: {e}"
        print(msg, file=sys.stderr)
        if args.log:
            _append_log(args.log, msg, 2)
        return 2

    report = diff(cfg, oc_doc, agnes_doc)
    plan = build_plan(report, cfg, oc_doc, agnes_doc)
    drift = has_drift(report)
    code = 1 if drift else 0

    out = [json.dumps(report, ensure_ascii=False, indent=2) if args.json else render(report),
           render_plan(plan, date)]

    if args.apply:
        if plan_is_empty(plan):
            out.append("→ 无需落盘：网关已与文档一致 ✓")
            code = 0
        else:
            new_text = apply_plan(text_before, plan, date)
            problems = verify(new_text, cfg, plan)
            if problems:
                out.append("→ 回读校验不通过，**未写盘**：")
                out += [f"    ✗ {p}" for p in problems]
                code = 2
            else:
                info = _write_config_text(cfg_path, new_text)
                out.append(f"→ 已写盘：{info['path']}（{info['bytes']} 字节，备份 {info['backup']}）")
                out.append("   server.hot_reload 开着，15 秒内自动生效，无需重启")
                code = 0
    elif drift:
        out.append("→ 只报告模式，未写盘（加 --apply 才同步）")

    text = "\n".join(out)
    print(text)
    if args.log:
        _append_log(args.log, text, code)
    return code


if __name__ == "__main__":
    sys.exit(main())
