"""意见反馈（第二十二轮）：普通账号提意见（可附图），管理员在「用户反馈」页看全部。

为什么单独一个文件、不塞进 history.py：
  history 的语义是「每个账号自己的生成记录 + 按 id upsert 覆盖」（前端可以整份重推）；
  反馈是「一次性提交、不可改、管理员全局只读」—— 生命周期与读写模式都不一样，
  混在一起只会让两边的边界都变模糊。

存储形态（沿用本项目「无 DB」的约束）：
- data/feedback.json：`{"items": [条目…]}`，条目按「新 → 旧」；
- data/feedback_media/<fid>-<i>.<ext>：图片正文（解码后的二进制）。

为什么图片落文件、缩略图进 JSON：
  一条最多 3 张图、每张压到 ~300KB（base64 后 ×1.37）→ 单单图片就 1.2MB。
  若全塞进 JSON，而 JSON 是「每次写入都重写整份」的，几十条就能把它顶到几十 MB
  （history.py 的注释里记过同一个坑）。拆开之后：
  - JSON 维持百 KB 级，列表页靠内嵌的**缩略图**（≤320px，一二十 KB）零额外请求就能预览；
  - 点开大图才走 GET /feedback/media/{fid}/{i} 取那一张。

容量与边界：
- 最多 max_entries 条（默认 500），超了丢最旧的一条并**连带删掉它的图片文件**；
- 正文 ≤ MAX_CONTENT 字符；图片 ≤ MAX_IMAGES 张、每张解码后 ≤ MAX_IMAGE_BYTES；
- 图片类型以**解码后的魔数**为准（不信任客户端声明的 MIME），只收 jpeg/png/webp/gif；
- 正文/缩略图都按 UTF-8 逐字符计数，避免"看着没超，字节超了"；
- 每条另存 `history`（处理记录）：提交 + 每一次状态变更（谁/何时/从哪到哪/说明），
  只增不改，最多 MAX_STEPS 条；老数据在 _load 时按现有字段补出来（见 _fix_history）。

权限在 main.py 里判（存储层只管数据）：提交看自己 = 任意登录会话；看全部/看别人的图 = admin；
「撤回」= 提交人本人 + 还停在「待处理」（`withdraw`）。
"""

from __future__ import annotations

import base64
import json
import os
import re
import threading
import time
from uuid import uuid4

MAX_ENTRIES = 500          # 保留条数（超出丢最旧，含其图片文件）
MAX_CONTENT = 2000         # 正文上限（字符）
MAX_NOTE = 500             # 管理员处理说明上限（字符）
MAX_IMAGES = 3             # 每条最多几张图
MAX_IMAGE_BYTES = 700_000  # 单张原图解码后上限（字节）
MAX_THUMB_BYTES = 150_000  # 单张缩略图解码后上限（字节）
MAX_NAME_CHARS = 80        # 原文件名只做展示，截断防超长

# 处理进度（第二十二轮追加）：提交即 pending，管理员在「用户反馈」里改。
# 状态机就三档，别搞复杂 —— 提交人看进度、管理员改状态，够了。
STATUSES = ("pending", "doing", "done")
MAX_STEPS = 50             # 每条反馈最多留多少条处理记录（超出丢最旧，只影响展示）

_URI_HEAD = re.compile(r"^data:image/[a-z0-9.+-]+;base64,", re.I)


def _step(ts, frm, to, note, by) -> dict:
    """一条处理记录。`from` 为空串表示「提交」那一步（没有前一个状态）。"""
    return {
        "ts": round(float(ts or 0), 1),
        "from": str(frm or ""),
        "to": str(to or "pending"),
        "note": note if isinstance(note, str) else "",
        "by": str(by or ""),
    }


def _fix_history(e: dict) -> list[dict]:
    """处理记录：老数据（本轮之前提交的）没有 history —— 用现有字段补出来，
    保证「每个环节都有记录」对历史单子也成立：
      第 1 条「提交 → 待处理」（提交人自己）；
      若已经推进过，再补 1 条「待处理 → 现状态」（当时是谁点的已不可考，by 留空）。
    """
    h = e.get("history")
    if isinstance(h, list) and h:
        out = []
        for s in h:
            if not isinstance(s, dict):
                continue
            out.append(_step(
                s.get("ts") or e.get("created"),
                s.get("from"),
                s.get("to") if s.get("to") in STATUSES else "pending",
                s.get("note"),
                s.get("by"),
            ))
        if out:
            return out[-MAX_STEPS:]
    created = e.get("created") or 0
    built = [_step(created, "", "pending", "", e.get("account"))]
    st = e.get("status") or "pending"
    if st != "pending":
        built.append(_step(e.get("updated") or created, "pending", st, e.get("note") or "", ""))
    return built


class FeedbackError(Exception):
    """校验失败（main.py 映射为 400）。"""


def _sniff(data: bytes) -> str | None:
    """按魔数认图片类型。不信任客户端写的 MIME —— 扩展名/声明都能伪造。"""
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    return None


def _decode_image(uri, max_bytes: int, label: str) -> tuple[bytes, str]:
    """data URI → (二进制, 类型)。任何不合规都抛 FeedbackError（不静默丢弃）。"""
    if not isinstance(uri, str) or not uri.startswith("data:"):
        raise FeedbackError(f"{label}必须是 data URI 图片")
    i = uri.find(",")
    if i < 0 or not _URI_HEAD.match(uri[: i + 8]):
        raise FeedbackError(f"{label}只支持 base64 编码的图片 data URI")
    try:
        raw = base64.b64decode(uri[i + 1:], validate=True)
    except Exception:
        raise FeedbackError(f"{label}的 base64 内容损坏")
    if not raw:
        raise FeedbackError(f"{label}是空文件")
    if len(raw) > max_bytes:
        raise FeedbackError(f"{label}解码后 {len(raw) // 1024}KB，超过 {max_bytes // 1024}KB 上限")
    kind = _sniff(raw)
    if kind is None:
        raise FeedbackError(f"{label}不是可识别的图片（只收 jpeg/png/webp/gif）")
    return raw, kind


def _clean_text(v, limit: int, label: str) -> str:
    s = v if isinstance(v, str) else ""
    s = s.strip()
    if len(s) > limit:
        raise FeedbackError(f"{label}最多 {limit} 字（当前 {len(s)}）")
    return s


def _thumb_uri(raw: bytes, kind: str) -> str:
    """把落盘的缩略图重新拼回 data URI（列表页 <img> 直接用，CSP 已放行 data:）。"""
    mime = "image/jpeg" if kind in ("jpeg", "jpg") else f"image/{kind}"
    return f"data:{mime};base64," + base64.b64encode(raw).decode("ascii")


class FeedbackStore:
    def __init__(self, path: str, media_dir: str, max_entries: int = MAX_ENTRIES):
        self._path = path
        self._media_dir = media_dir
        self._max = max(1, int(max_entries))
        self._lock = threading.Lock()
        self._items: list[dict] = []
        self._load()
        self._prune_media()

    # ── 持久化 ─────────────────────────────
    def _load(self) -> None:
        try:
            with open(self._path, encoding="utf-8") as f:
                d = json.load(f)
            items = d.get("items") if isinstance(d, dict) else d
            self._items = [it for it in (items or []) if isinstance(it, dict)]
        except FileNotFoundError:
            self._items = []
        except Exception:
            # 坏文件不能让网关起不来：备份后从空开始（与 history.py 同一口径）
            try:
                os.replace(self._path, self._path + ".bad")
            except OSError:
                pass
            self._items = []
        # 补齐/纠正进度字段：老数据（没有 status 的）按待处理算，非法值也归到待处理，
        # 免得前端拿到一个它不认识的状态白屏；处理记录缺失的按现有字段补出来。
        for e in self._items:
            if e.get("status") not in STATUSES:
                e["status"] = "pending"
            e["note"] = e.get("note") if isinstance(e.get("note"), str) else ""
            if not isinstance(e.get("updated"), (int, float)):
                e["updated"] = e.get("created") or 0
            e["history"] = _fix_history(e)

    def _save(self) -> None:
        # 调用方持锁
        d = os.path.dirname(self._path)
        if d and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        tmp = self._path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"items": self._items}, f, ensure_ascii=False)
        os.chmod(tmp, 0o600)   # 反馈是隐私内容
        os.replace(tmp, self._path)

    # ── 图片文件 ───────────────────────────
    def _img_path(self, fid: str, idx: int, ext: str) -> str:
        return os.path.join(self._media_dir, f"{fid}-{idx}.{ext}")

    def _write_image(self, fid: str, idx: int, raw: bytes, kind: str) -> None:
        if not os.path.isdir(self._media_dir):
            os.makedirs(self._media_dir, exist_ok=True)
        p = self._img_path(fid, idx, kind)
        with open(p, "wb") as f:
            f.write(raw)
        os.chmod(p, 0o600)

    def _drop_media(self, entry: dict) -> None:
        """删掉某条反馈的全部图片文件（条目被淘汰 / 数据修复时用）。"""
        fid = str(entry.get("id") or "")
        if not fid:
            return
        for i in range(len(entry.get("images") or [])):
            for ext in ("jpeg", "png", "webp", "gif"):
                try:
                    os.remove(self._img_path(fid, i, ext))
                except OSError:
                    pass

    def _prune_media(self) -> None:
        """清掉不属于任何现存条目的图片文件（异常中断留下的孤儿）。"""
        if not os.path.isdir(self._media_dir):
            return
        alive = {str(e.get("id")) for e in self._items if e.get("id")}
        try:
            names = os.listdir(self._media_dir)
        except OSError:
            return
        for name in names:
            fid = name.rsplit("-", 1)[0]
            if fid not in alive:
                try:
                    os.remove(os.path.join(self._media_dir, name))
                except OSError:
                    pass

    # ── 写 ─────────────────────────────────
    def add(self, account: str, content, images) -> dict:
        """新增一条反馈，返回落库后的条目（含缩略图）。校验失败抛 FeedbackError。"""
        text = _clean_text(content, MAX_CONTENT, "反馈内容")
        if not text:
            raise FeedbackError("反馈内容不能为空")
        imgs = images if isinstance(images, list) else []
        if len(imgs) > MAX_IMAGES:
            raise FeedbackError(f"最多上传 {MAX_IMAGES} 张图片（当前 {len(imgs)} 张）")

        # 先全部解码校验（任何一张不合格就整条拒绝，不留半成品文件）
        decoded: list[tuple[bytes, str, str, bytes | None, str | None, str]] = []
        for i, it in enumerate(imgs):
            if not isinstance(it, dict):
                raise FeedbackError(f"第 {i + 1} 张图片格式不对")
            label = f"第 {i + 1} 张图片"
            raw, kind = _decode_image(it.get("uri"), MAX_IMAGE_BYTES, label)
            thumb_raw = thumb_kind = None
            if it.get("thumb"):
                thumb_raw, thumb_kind = _decode_image(it.get("thumb"), MAX_THUMB_BYTES, f"{label}的缩略图")
            name = _clean_text(it.get("name"), MAX_NAME_CHARS, f"{label}文件名")
            decoded.append((raw, kind, name, thumb_raw, thumb_kind, label))

        fid = "f%d-%s" % (int(time.time()), uuid4().hex[:6])
        entry_images: list[dict] = []
        for i, (raw, kind, name, thumb_raw, thumb_kind, _label) in enumerate(decoded):
            self._write_image(fid, i, raw, kind)
            entry_images.append({
                "name": name,
                "bytes": len(raw),
                # 缩略图内嵌 JSON：列表页零请求就能预览（体积：一二十 KB/张）
                "thumb": _thumb_uri(thumb_raw, thumb_kind) if thumb_raw else "",
                "type": kind,
            })

        now = round(time.time(), 1)
        who = account or "anonymous"
        entry = {
            "id": fid,
            "account": who,
            "content": text,
            "created": now,
            "images": entry_images,
            # 进度：提交即「待处理」，管理员在「用户反馈」里推进
            "status": "pending",
            "note": "",
            "updated": now,
            # 处理记录第 1 条＝提交（后面的每一步由 update() 追加，只增不改）
            "history": [_step(now, "", "pending", "", who)],
        }
        with self._lock:
            self._items.insert(0, entry)
            evicted = self._items[self._max:]
            del self._items[self._max:]
            self._save()
        for e in evicted:            # 落盘之后再删文件，避免"删了文件但条目还在"
            self._drop_media(e)
        return entry

    # ── 处理进度（只有管理员能改，权限在 main.py 判） ──────────
    def update(self, fid: str, status, note, by: str = "") -> dict:
        """更新某条反馈的处理状态 / 处理说明，返回更新后的条目。

        **每一步都进 `history`（只增不改）**：谁（by）、什么时候（ts）、
        从哪个状态到哪个状态（from → to）、留了什么说明（note）。
        提交人看到的「处理记录」就是这个数组，管理员改几次就有几条。

        fid 不存在返回 {}（调用方据此 404）；参数不合法抛 FeedbackError。
        """
        if status not in STATUSES:
            raise FeedbackError("处理状态只能是 pending / doing / done")
        text = _clean_text(note, MAX_NOTE, "处理说明")
        with self._lock:
            hit = next((e for e in self._items if str(e.get("id")) == str(fid)), None)
            if hit is None:
                return {}
            step = _step(time.time(), hit.get("status"), status, text, by)
            hit["status"] = status
            hit["note"] = text
            hit["updated"] = step["ts"]
            hist = hit.get("history") if isinstance(hit.get("history"), list) else []
            hit["history"] = (hist + [step])[-MAX_STEPS:]
            self._save()
            return dict(hit)

    # ── 撤回（只有提交人本人、且还停在「待处理」时） ────────────
    def withdraw(self, account: str, fid: str) -> dict:
        """提交人撤回自己的反馈，返回 {"ok": True, "item": …} 或 {"ok": False, "reason": …}。

        规则（老板 2026-09-13 追加）：**待处理**的单子提交人可以自己撤回；
        一旦管理员推进到「处理中 / 已处理」，就不允许再撤 —— 否则管理员白干一场，
        而且"已处理"的单子从提交人列表里凭空消失也不合理。
        reason：notfound（没这条）/ forbidden（不是自己的）/ locked（已进流程）。
        """
        who = account or "anonymous"
        with self._lock:
            idx = next((i for i, e in enumerate(self._items) if str(e.get("id")) == str(fid)), None)
            if idx is None:
                return {"ok": False, "reason": "notfound"}
            entry = self._items[idx]
            if str(entry.get("account") or "") != who:
                return {"ok": False, "reason": "forbidden"}
            if entry.get("status") != "pending":
                return {"ok": False, "reason": "locked"}
            del self._items[idx]
            self._save()
        # 落盘之后再删图片文件（与容量淘汰同一顺序，避免"文件没了条目还在"）
        self._drop_media(entry)
        return {"ok": True, "item": entry}

    # ── 归档 / 删除（管理员，收件箱专属；第N轮追加）─────────────────
    def set_archived(self, fid: str, archived: bool) -> dict:
        """归档/取消归档一条反馈（管理员在收件箱里整理用）。

        归档不清数据、不动处理状态 —— 只是打 `archived` 标记，让收件箱能按
        「未归档 / 全部 / 已归档」过滤看。旧数据（本轮之前）没有该字段：视为未归档。
        提交人自己仍然能看到自己的那条（归档只影响管理员收件箱的整洁度）。
        """
        with self._lock:
            hit = next((e for e in self._items if str(e.get("id")) == str(fid)), None)
            if hit is None:
                return {}
            hit["archived"] = bool(archived)
            hit["updated"] = round(time.time(), 1)
            self._save()
            return dict(hit)

    def admin_delete(self, fid: str) -> dict:
        """管理员永久删除一条反馈（连同它的全部图片文件）。不存在返回 {}。"""
        with self._lock:
            idx = next((i for i, e in enumerate(self._items) if str(e.get("id")) == str(fid)), None)
            if idx is None:
                return {}
            entry = self._items.pop(idx)
            self._save()
        self._drop_media(entry)          # 落盘后再删图片，避免"文件没了条目还在"
        return entry

    # ── 读 ─────────────────────────────────
    def mine(self, account: str) -> list[dict]:
        with self._lock:
            return [dict(e) for e in self._items if e.get("account") == (account or "anonymous")]

    def all(self) -> list[dict]:
        with self._lock:
            return [dict(e) for e in self._items]

    def count(self) -> int:
        with self._lock:
            return len(self._items)

    def owner_of(self, fid: str) -> str | None:
        """条目归属账号；条目不存在返回 None（调用方据此 404）。"""
        with self._lock:
            for e in self._items:
                if str(e.get("id")) == str(fid):
                    return str(e.get("account") or "")
        return None

    def image_count(self, fid: str) -> int:
        with self._lock:
            for e in self._items:
                if str(e.get("id")) == str(fid):
                    return len(e.get("images") or [])
        return 0

    def media_uri(self, fid: str, idx: int) -> str | None:
        """读原图并拼成 data URI。fid 必须先在库里存在（查不到直接 None，不做路径拼接猜测）。"""
        with self._lock:
            hit = next((e for e in self._items if str(e.get("id")) == str(fid)), None)
            if hit is None or idx < 0 or idx >= len(hit.get("images") or []):
                return None
            meta = hit["images"][idx]
            kind = str(meta.get("type") or "jpeg")
        if kind not in ("jpeg", "png", "webp", "gif"):
            return None
        try:
            with open(self._img_path(fid, idx, kind), "rb") as f:
                raw = f.read()
        except OSError:
            return None
        return _thumb_uri(raw, kind)

    def stats(self) -> dict:
        with self._lock:
            return {
                "count": len(self._items),
                "with_images": sum(1 for e in self._items if e.get("images")),
                "cap": self._max,
                "bytes": os.path.getsize(self._path) if os.path.exists(self._path) else 0,
            }
