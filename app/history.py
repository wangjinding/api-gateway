"""按账号持久化的前端历史（图像 / 视频 / 对话）。

背景：这三类历史原先只存在浏览器里（图像、视频在 localStorage，对话在 sessionStorage），
于是有三个老问题：① 换设备/换浏览器就看不见；② 关标签页对话即丢；③ 视频每条带一大段
上游 rawtext，很容易把 5MB 配额撑满，而前端 `setItem` 失败是被静默 catch 掉的 ——
表现就是"生成过的东西莫名其妙没了"。

这里给一份最小落盘存储（纯标准库、无数据库，符合本项目"无 DB"的架构约束）：
data/history.json，结构 `{账号: {类别: [条目...]}}`，条目按"新→旧"排列。

写入语义是 **按 id upsert**（前端提交增量或整份都可以）：带已存在 id 的条目就地更新
（视频状态从"生成中"变"完成"就是走这条路），没有 id 或 id 没见过的新插到最前。
这样两台设备各自生成图像/视频时不会互相整份覆盖掉 —— 这是"全量 PUT"最容易踩的坑。

边界控制：
- 每账号每类别最多 MAX_ITEMS 条（超出截掉最旧的尾部）；
- 单条序列化超过 MAX_ITEM_BYTES 时，把里面过长的字符串字段截断（rawtext/推理正文这类）；
- 整份文件超过 MAX_FILE_BYTES 时，按账号逐个削条目，保证不会无限膨胀；
- 文件权限 600（里面是提示词/回答正文，属隐私），且已进 .gitignore。

并发：写操作都在锁内做「改 + 落盘」，读走快照，单实例够用。
"""

from __future__ import annotations

import json
import os
import threading

KINDS = ("chat", "images", "videos")
# 各类别的容量按"内容天然体量"分开定：对话一条就是一整段多轮消息（正文可能很长），
# 图像一条只有 URL + prompt，视频一条带一段上游 raw 回执 —— 一刀切会把对话正文截坏。
LIMITS: dict[str, dict[str, int]] = {
    "chat":   {"items": 50,  "item_bytes": 512 * 1024, "field_chars": 20000},
    "images": {"items": 200, "item_bytes": 8 * 1024,   "field_chars": 1200},
    "videos": {"items": 200, "item_bytes": 16 * 1024,  "field_chars": 2000},
}
MAX_ITEMS = 200                    # 兜底默认（未知类别用）
MAX_ITEM_BYTES = 8 * 1024
MAX_FIELD_CHARS = 1200
MAX_FILE_BYTES = 4 * 1024 * 1024   # 整份 history.json 的软上限


def _lim(kind: str) -> dict[str, int]:
    return LIMITS.get(kind, {"items": MAX_ITEMS, "item_bytes": MAX_ITEM_BYTES,
                             "field_chars": MAX_FIELD_CHARS})


class HistoryError(Exception):
    """校验失败（映射为 400）。"""


def _shrink(obj, field_chars: int, depth: int = 0):
    """递归压缩：长字符串截断、容器规模限制，防单条撑爆配额。"""
    if isinstance(obj, str):
        return obj if len(obj) <= field_chars else obj[:field_chars] + "…(已截断)"
    if depth > 6:
        return "…(层级过深)"
    if isinstance(obj, list):
        return [_shrink(v, field_chars, depth + 1) for v in obj[:2000]]
    if isinstance(obj, dict):
        return {str(k)[:64]: _shrink(v, field_chars, depth + 1) for k, v in list(obj.items())[:64]}
    if isinstance(obj, (int, float, bool)) or obj is None:
        return obj
    return _shrink(str(obj), field_chars, depth + 1)


def _normalize_item(item, kind: str) -> dict | None:
    """条目必须是对象；顺手做体积收缩。返回 None 表示非法条目（丢弃）。"""
    if not isinstance(item, dict):
        return None
    lim = _lim(kind)
    s = json.dumps(item, ensure_ascii=False, default=str)
    if len(s.encode("utf-8")) > lim["item_bytes"]:
        s = json.dumps(_shrink(item, lim["field_chars"]), ensure_ascii=False, default=str)
        try:
            item = json.loads(s)
        except Exception:
            return None
    return item


class HistoryStore:
    def __init__(self, path: str):
        self._path = path
        self._lock = threading.Lock()
        self._data: dict = {}
        self._load()

    # ── 持久化 ─────────────────────────────
    def _load(self) -> None:
        try:
            with open(self._path, encoding="utf-8") as f:
                d = json.load(f)
            self._data = d if isinstance(d, dict) else {}
        except FileNotFoundError:
            self._data = {}
        except Exception:
            # 坏文件不能让网关起不来：备份后从空开始
            try:
                os.replace(self._path, self._path + ".bad")
            except OSError:
                pass
            self._data = {}

    def _save(self) -> None:
        # 调用方持锁。超软上限时按账号逐个削最旧的尾部（前端提交顺序是新→旧）。
        while len(self._data) and len(json.dumps(self._data, ensure_ascii=False).encode()) > MAX_FILE_BYTES:
            trimmed = False
            for acc in list(self._data):
                for kind in list(self._data.get(acc) or {}):
                    lst = self._data[acc][kind]
                    if len(lst) > 10:
                        del lst[len(lst) // 2:]
                        trimmed = True
                        break
                if trimmed:
                    break
            if not trimmed:
                break
        d = os.path.dirname(self._path)
        if d and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        tmp = self._path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self._path)

    # ── 读写 ───────────────────────────────
    @staticmethod
    def _check_kind(kind: str) -> str:
        if kind not in KINDS:
            raise HistoryError(f"未知历史类别 {kind!r}，可选：{'/'.join(KINDS)}")
        return kind

    def get(self, account: str, kind: str) -> list[dict]:
        self._check_kind(kind)
        with self._lock:
            return list(((self._data.get(account) or {}).get(kind)) or [])

    def upsert(self, account: str, kind: str, items: list) -> list[dict]:
        """按 id 合并写入，返回合并后的整份列表。

        - 条目必须是对象；带 `id` 且已存在 → 原地替换（保留原位置，视频状态刷新走这里）
        - 无 `id` 或 id 没见过 → 新插到最前面
        - 合并后裁剪到 MAX_ITEMS 条（丢最旧的尾部）
        """
        if not isinstance(items, list):
            raise HistoryError("items 必须是数组")
        lim = _lim(kind)
        incoming = [it for it in (_normalize_item(x, kind) for x in items) if it is not None]
        if not incoming:
            return self.get(account, kind)
        with self._lock:
            acc = self._data.setdefault(account or "anonymous", {})
            lst = acc.setdefault(kind, [])
            index = {str(it.get("id")): i for i, it in enumerate(lst) if it.get("id") is not None}
            for it in incoming:
                iid = it.get("id")
                key = str(iid) if iid is not None else None
                if key is not None and key in index:
                    lst[index[key]] = it
                else:
                    lst.insert(0, it)
                    # 第十八轮: 无条件重建索引 —— 此前只在"带 id 的新条目"分支重建，
                    #   而**无 id 的插入同样会把后面所有元素顶后一位**，旧下标随即失效：
                    #   同一批 items 里「无 id 新增 + 带 id 更新」混排时，后者会按过期下标
                    #   覆盖到错误的位置（接口契约允许这么传，只是前端目前总带 id）。
                    index = {str(x.get("id")): i for i, x in enumerate(lst)
                             if x.get("id") is not None}
            del lst[lim["items"]:]
            self._save()
            return list(lst)

    def delete_item(self, account: str, kind: str, item_id: str) -> bool:
        """按 id 删单条（前端逐条删除用这个）。"""
        self._check_kind(kind)
        with self._lock:
            acc = self._data.get(account or "anonymous") or {}
            lst = acc.get(kind) or []
            keep = [it for it in lst if str(it.get("id")) != str(item_id)]
            if len(keep) == len(lst):
                return False
            acc[kind] = keep
            self._save()
            return True

    def clear(self, account: str, kind: str) -> int:
        self._check_kind(kind)
        with self._lock:
            acc = self._data.get(account or "anonymous") or {}
            n = len(acc.get(kind) or [])
            acc[kind] = []
            self._save()
            return n

    def stats(self) -> dict:
        with self._lock:
            return {
                "accounts": len(self._data),
                "items": {k: sum(len((a or {}).get(k) or []) for a in self._data.values()) for k in KINDS},
                "bytes": os.path.getsize(self._path) if os.path.exists(self._path) else 0,
            }
