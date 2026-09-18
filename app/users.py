"""页面配置的账号体系（替代原先写死在 data/gateway.yaml / .env 的鉴权）。

- 用户持久化在 data/users.json（自动创建，权限 600，不进 git）：密码只存 PBKDF2 哈希；
  API Key 存哈希用于鉴权 + **额外存一份明文**，为的是页面随时能再复制（详见 keys() 的取舍说明）。
- 初始管理员 admin/admin，唯一管理员；admin 可建账号（账号名仅字母数字，默认密码=账号名）。
- 普通账号只能管自己的 API Key（**每人最多 3 把**）；admin 看全部指标/日志，且**建 Key 不限数量**。
- 凭证分两类：**会话**（sess-，密码登录换来、短时、可被吊销）与 **API Key**（wjd-，长期、
  只能调模型）。改密码 / 铸 Key / 管账号这类「动凭证」的操作只认会话凭证。
- 吊销：删账号、改密、管理员重置密码 → 该账号全部会话立即失效。
- 停用 / 启用（第二十四轮，老板要求）：账号记录里的 `disabled` 字段。停用后 ①密码正确也不给
  会话；②名下 API Key 一律失效（`resolve_key` 直接跳过，但**不删除** —— 重新启用即恢复）；
  ③停用瞬间由调用方吊销其全部网页会话。admin 账号永不可停用（唯一管理员被停用后没人能进
  管理页，只能改盘上 JSON 救回）。
- 纯标准库（hashlib.pbkdf2 + hmac），无第三方依赖。

如需改密码哈希强度或迭代次数，改 ITERATIONS 即可（已存的 salt 仍可校验）。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time

log = logging.getLogger("api-gateway.users")

USERNAME_RE = re.compile(r"^[A-Za-z0-9]+$")
ITERATIONS = 200_000
KEY_PREFIX = "wjd-"        # 新建 API Key 的统一前缀（与对外品牌一致）
# 迁移期仍承认旧前缀，免得已发出去的长期 Key 被无声作废；想一刀切就改成 ()
LEGACY_KEY_PREFIXES: tuple[str, ...] = ("ak-",)
_KEY_PREFIXES: tuple[str, ...] = (KEY_PREFIX,) + LEGACY_KEY_PREFIXES
KEY_BYTES = 28  # token_urlsafe(28) ≈ 38 chars → 整把 key 42 字符
MIN_PASSWORD = 8          # 显式设置/修改的密码下限（默认密码=账号名不受此限，按老板约定）
# 第三十轮把下限从 4 提到 8：4 位口令在可离线爆破的空间里（1 万种）几乎等于没设。
# 只对**新建时显式给密码 / 事后改密**生效 —— 登录校验不做长度判断，
# 所以既有短口令账号（如 test）照常登录，只是不能再被改回短口令。
MAX_KEYS_PER_MEMBER = 3   # 普通账号每人最多 3 把 API Key（防失控膨胀 / 泄漏后无限铸 Key）


def key_limit_for_role(role: str) -> int | None:
    """该角色的 API Key 数量上限。None = 不限（admin 按需发长期凭证）。"""
    return None if role == "admin" else MAX_KEYS_PER_MEMBER

# 账号不存在时也走一次 PBKDF2：否则「不存在的账号」明显更快返回，
# 成了用户名枚举的时序侧信道（第八轮审计）。
_DUMMY_SALT = secrets.token_hex(16)


class UserError(Exception):
    """业务校验失败（映射为 400/409）。"""


def _hash_pw(password: str, salt_hex: str | None = None):
    salt = salt_hex or secrets.token_hex(16)
    dig = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                              bytes.fromhex(salt), ITERATIONS).hex()
    return salt, dig


def _hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


class UsersStore:
    def __init__(self, path: str):
        self._path = path
        self._lock = threading.Lock()
        self._data: dict = {}
        self._load()
        self.ensure_admin()

    # ── 持久化 ─────────────────────────────
    def _load(self) -> None:
        try:
            with open(self._path, encoding="utf-8") as f:
                self._data = json.load(f)
        except FileNotFoundError:
            self._data = {"users": {}}
        except Exception:
            self._data = {"users": {}}
        self._data.setdefault("users", {})

    def _save(self) -> None:
        d = os.path.dirname(self._path)
        if d and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        tmp = self._path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)
        # 文件里是密码哈希 + API Key 哈希，属凭证材料 → 仅属主可读写（第八轮审计补）。
        os.chmod(tmp, 0o600)
        os.replace(tmp, self._path)

    # ── 初始管理员 ─────────────────────────
    def ensure_admin(self) -> None:
        """首次运行自动创建 admin/admin（唯一管理员，登录后应立刻改密）。已存在则跳过。"""
        with self._lock:
            if "admin" in self._data["users"]:
                self._keep_admin_usable()
                return
            salt, dig = _hash_pw("admin")
            self._data["users"]["admin"] = {
                "password_salt": salt, "password_hash": dig, "role": "admin",
                "created": round(time.time(), 1),
                "api_keys": {},
            }
            self._save()

    def _keep_admin_usable(self) -> None:
        """兜底：admin 被人手工改盘上 JSON 标成 disabled 时自动恢复（调用方需已持锁）。

        接口层已经拒绝停用 admin（set_disabled 抛 UserError），这条只防「手工编辑 users.json」——
        唯一管理员被停用意味着谁也进不了管理页，只能停机改文件，代价太大。
        """
        u = self._data["users"].get("admin") or {}
        if u.get("disabled"):
            u["disabled"] = False
            u.pop("disabled_at", None)
            self._data["users"]["admin"] = u
            self._save()
            log.warning("检测到 admin 被标记为停用，已自动恢复启用（管理员账号不允许停用）")

    # ── 账号查询 ───────────────────────────
    def find(self, name: str) -> dict | None:
        with self._lock:
            return self._data["users"].get(name)

    def is_admin(self, name: str) -> bool:
        u = self.find(name)
        return bool(u and u.get("role") == "admin")

    def role(self, name: str) -> str:
        u = self.find(name)
        return (u or {}).get("role", "member")

    def list_users(self) -> list[dict]:
        """不含密码/Key 明文，仅管理视图（含停用状态，页面据此显示「已停用」并切换按钮）。"""
        with self._lock:
            users = self._data["users"]
            return [{
                "name": n,
                "role": u.get("role", "member"),
                "created": u.get("created"),
                "api_key_count": len(u.get("api_keys") or {}),
                "disabled": bool(u.get("disabled")),
                "disabled_at": u.get("disabled_at"),
            } for n, u in sorted(users.items())]

    def is_disabled(self, name: str) -> bool:
        """账号是否已停用（账号不存在按未停用处理：不存在自会撞上别的判断）。"""
        u = self.find(name)
        return bool(u and u.get("disabled"))

    def set_disabled(self, name: str, disabled: bool = True, by: str | None = None) -> bool:
        """停用 / 启用账号。返回账号是否存在（不存在 → False，由上层回 404）。

        幂等：重复设置同一状态也算成功（页面连点两下不会报错）。
        两条拒绝规则都放在这里（规则只有一处，接口层不用再判一遍）：
          - admin 永不可停用 —— 唯一管理员被停用后没人能进管理页，只能改盘上 JSON 救回；
          - 不能停用自己（by=操作者）—— 手滑点自己那行的按钮就把自己锁在外面，没必要。
        """
        if disabled and name == "admin":
            raise UserError("不能停用管理员账号 admin")
        if disabled and by and by == name:
            raise UserError("不能停用当前登录的账号")
        with self._lock:
            u = self._data["users"].get(name)
            if u is None:
                return False
            if bool(u.get("disabled")) != bool(disabled):
                u["disabled"] = bool(disabled)
                if disabled:
                    u["disabled_at"] = round(time.time(), 1)   # 页面 tooltip 用（「什么时候停的」）
                else:
                    u.pop("disabled_at", None)
                self._save()
            return True

    # ── 创建 / 删除账号 ────────────────────
    def create_user(self, name: str, password: str | None = None, role: str = "member") -> str:
        if not name or not USERNAME_RE.match(name):
            raise UserError("账号名只能由字母和数字组成")
        if len(name) < 2 or len(name) > 32:
            raise UserError("账号名长度需在 2~32 之间")
        if password is not None:
            # 显式传密码才校验强度；不传则按约定用账号名作默认密码
            if len(str(password)) < MIN_PASSWORD:
                raise UserError(f"密码至少 {MIN_PASSWORD} 位")
            password = str(password)
        with self._lock:
            if name in self._data["users"]:
                raise UserError(f"账号 {name} 已存在")
            pw = password if password is not None else name  # 默认密码=账号名
            salt, dig = _hash_pw(pw)
            self._data["users"][name] = {
                "password_salt": salt, "password_hash": dig,
                "role": role, "created": round(time.time(), 1),
                "api_keys": {},
            }
            self._save()
        return name

    def delete_user(self, name: str) -> bool:
        """删除账号。返回是否真的删掉了（不存在 → False，供上层回 404）。

        第八轮审计: 原先返回 `bytes | None` 且不存在与已删除都返回 None，
        调用方无从区分，前端「删除一个不存在的账号」也报成功。
        """
        with self._lock:
            users = self._data["users"]
            if name not in users:
                return False
            if name == "admin":
                raise UserError("不能删除管理员账号 admin")
            del users[name]
            self._save()
            return True

    # ── 密码 ───────────────────────────────
    def verify(self, name: str, password: str) -> bool:
        u = self.find(name)
        if not password:
            # 空密码不做校验，但补一次哑哈希，保持与「账号不存在」同样的耗时
            _hash_pw("", _DUMMY_SALT)
            return False
        if not u:
            _hash_pw(password, _DUMMY_SALT)   # 时序对齐：防用户名枚举
            return False
        salt, dig = u.get("password_salt", ""), u.get("password_hash", "")
        if not salt or not dig:
            return False
        _, calc = _hash_pw(password, salt)
        return hmac.compare_digest(dig, calc)

    def change_password(self, name: str, new_password: str) -> None:
        if not new_password or len(new_password) < MIN_PASSWORD:
            raise UserError(f"新密码至少 {MIN_PASSWORD} 位")
        with self._lock:
            users = self._data["users"]
            if name not in users:
                raise UserError("账号不存在")
            salt, dig = _hash_pw(new_password)
            users[name]["password_salt"] = salt
            users[name]["password_hash"] = dig
            self._save()

    # ── API Key（各账号自建，默认无；member 最多 3 把，admin 不限） ──
    def add_api_key(self, name: str, label: str = "") -> str:
        key = KEY_PREFIX + secrets.token_urlsafe(KEY_BYTES)
        kh = _hash_key(key)
        with self._lock:
            users = self._data["users"]
            u = users.get(name)
            if u is None:
                raise UserError("账号不存在")
            keys = u.setdefault("api_keys", {})
            # 第八轮审计: 不设上限时，一个泄漏的凭证可以无限铸 Key 把 users.json 撑爆。
            # 现在按角色区分：普通账号 3 把封顶，admin 不限（要能按需发长期凭证）。
            # 角色与计数都在**同一次持锁**里读，避免"检查时是 member、写入前变了"的错位。
            limit = key_limit_for_role(u.get("role", "member"))
            if limit is not None and len(keys) >= limit:
                # 第十六轮（老板要求）：这条提示只留「已达上限」四个字 —— 上限是几把、要不要删，
                # 页面上（按钮 tooltip / 顶部告警）已经写着了，接口里再铺一段话只会让 toast 变啰嗦。
                raise UserError("已达上限")
            keys[kh] = {"label": label or "", "created": round(time.time(), 1),
                        "key": key}   # 明文一并存：老板要能在页面上反复复制（见下方取舍说明）
            self._save()
        return key  # 创建响应里也给一次明文

    def clear_api_keys(self, name: str) -> int:
        """一次性吊销该账号的全部 API Key，返回删掉的把数。

        第八轮审计: 改密/重置密码只能踢会话，救不了**泄漏的长期 Key**（Key 与密码无关）。
        所以给管理员一个一键止血手段；本人也随时可在设置页逐把删除。
        """
        with self._lock:
            u = self._data["users"].get(name)
            if u is None:
                raise UserError("账号不存在")
            keys = u.get("api_keys") or {}
            n = len(keys)
            u["api_keys"] = {}
            if n:
                self._save()
            return n

    def remove_api_key(self, name: str, key_or_hint: str) -> bool:
        # 传入完整明文 Key → 直接哈希匹配；传入 8 位 hint（id）→ 按前缀匹配存储的 key 哈希。
        kh = _hash_key(key_or_hint) if len(key_or_hint) > 40 else key_or_hint
        with self._lock:
            users = self._data["users"]
            if name not in users:
                return False
            keys = users[name].get("api_keys", {})
            target = kh
            if len(key_or_hint) <= 40:
                target = next((k for k in keys if k[:8] == key_or_hint), None)
                if target is None:
                    return False
            if target not in keys:
                return False
            del keys[target]
            self._save()
            return True

    def keys(self, name: str) -> list[dict]:
        """Key 元信息 + 明文（明文回显是为了能反复复制）。

        取舍（老板 2026-09-13 拍板）：以前只存 SHA-256、明文只在创建那一秒返回，
        页面刷新就再也看不到，只能重新建一把。现在明文随记录一起存在 data/users.json，
        换来「随时可复制」。代价是凭证以明文落在盘上 —— 所以这个文件的权限固定 600、
        且在 .gitignore 里；因此**只有会话凭证**能读这个列表（泄漏的 API Key 不能再
        顺走同账号其它 Key 的明文）。
        本改动之前发的 Key 无法反推明文，`key` 字段回空串，页面按 hint 显示。
        """
        with self._lock:
            u = self._data["users"].get(name) or {}
            return [{
                "id": kh[:8],
                "label": k.get("label", ""),
                "created": k.get("created"),
                "hint": k.get("key") or kh[:8],   # 有明文就给明文（页面直接展示/复制），旧数据退回哈希前缀
                "key": k.get("key", ""),
            } for kh, k in sorted((u.get("api_keys") or {}).items())]

    def resolve_key(self, key: str) -> tuple[str, str] | None:
        """任一**未停用**账号拥有该 API Key 则返回 (account, role)。恒定时间比较。

        前缀只是**廉价预筛**（真正的判定是哈希查表），故这里同时接受新前缀与
        LEGACY_KEY_PREFIXES 里的旧前缀，避免已发放的长期 Key 被格式变更无声作废。
        第二十四轮：被停用账号的 Key 在这里直接当作无效（Key 本身不删，重新启用即恢复）。
        """
        if not key or not key.startswith(_KEY_PREFIXES):
            return None
        kh = _hash_key(key)
        with self._lock:
            users = self._data["users"]
            for name, u in users.items():
                if u.get("disabled"):
                    continue
                if kh in (u.get("api_keys") or {}):
                    return name, u.get("role", "member")
        return None

    def disabled_key_owner(self, key: str) -> str | None:
        """该 API Key 是否属于一个**已停用**账号 → 返回该账号名（否则 None）。

        只给鉴权层用来把「Key 有效但账号被停用」从 401 里挑出来回 403 —— 对拿不出 Key 的人
        仍然是 401，不泄露任何账号状态。返回的账号名只进日志，不回给调用方。
        """
        if not key or not key.startswith(_KEY_PREFIXES):
            return None
        kh = _hash_key(key)
        with self._lock:
            for name, u in self._data["users"].items():
                if u.get("disabled") and kh in (u.get("api_keys") or {}):
                    return name
        return None