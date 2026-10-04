"""L2 权限判定层：插件管理员 / 群主 / 群管理员 / 专项「设管理」名单等权限判定。

迁出自 main.py 的权限相关方法。依赖：
- L1 ConfigStore（构造注入）：读 group_overrides / 全局 config / _normalize_qq_list_value
- 同层 MessageParser（构造注入）：仅 _moderation_require_admin(_msg) 需要读 raw

依赖规则：不 import 同层模块，MessageParser 通过构造注入（TYPE_CHECKING + 字符串注解）。

注意：_moderation_require_admin_msg 必须保持为**普通 async 函数（非生成器）**，
18 个调用点依赖其 bool 返回值。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config_store import ConfigStore
    from .message_parse import MessageParser
    from .onebot_api import OneBotApi


class PermissionService:
    """无状态（相对于插件类）的权限判定服务。"""

    def __init__(
        self,
        *,
        config_store: "ConfigStore",
        message_parse: "MessageParser",
        onebot_api: "OneBotApi",
    ):
        self._store = config_store
        self._mp = message_parse
        # _moderation_require_admin_msg 需发提示（send），故同层注入 OneBotApi（不 import 模块）
        self._api = onebot_api

    # ---------- 基础角色判定 ----------

    def _is_authorized(self, raw: dict, user_id: str = "") -> bool:
        """是否具备插件管理权限：仅 QQ 群管理员 + QQ 群主。"""
        return self._is_group_admin_or_owner(raw)

    def _is_group_owner(self, raw: dict) -> bool:
        role = raw.get("sender", {}).get("role", "")
        return role == "owner"

    def _is_group_admin(self, raw: dict) -> bool:
        role = raw.get("sender", {}).get("role", "")
        return role in {"admin", "owner"}

    def _is_group_admin_or_owner(self, raw: dict) -> bool:
        return self._is_group_admin(raw) or self._is_group_owner(raw)

    def _is_sender_group_admin_only(self, raw: dict) -> bool:
        return raw.get("sender", {}).get("role", "") == "admin"

    def _sender_has_special_title(self, raw: dict) -> bool:
        sender = raw.get("sender", {}) if isinstance(raw, dict) else {}
        for key in ("title", "special_title"):
            value = str(sender.get(key, "")).strip()
            if value:
                return True
        return False

    # ---------- 「设管理」专项名单 ----------

    def _effective_group_admin_admins(self, group_id: str) -> list:
        """本群生效的「设管理」专项名单（#219 owner 要求：可像 group_overrides 一样按群覆盖）。

        优先级：
        1. group_overrides[群号]["group_admin_admins"]（群内 /添加管理管理 等指令维护）
        2. group_admin_admins_by_group[群号]（WebUI 按群名单）
        命中即替换（不与上层合并），与插件既有「按群覆盖 > 按群 WebUI 名单」语义一致。
        """
        # 注意：group_overrides 已迁移到 runtime.json，#219 上游实现仍读框架配置会永远取不到
        # 群内指令写入的名单，故这里统一改走运行时映射（与 set_group_setting 写入侧一致）。
        ov = self._store.runtime_map("group_overrides").get(str(group_id), {}).get("group_admin_admins")
        ov_list = self._store.normalize_qq_list_value(ov)
        if ov_list:
            return ov_list
        by_group = self._store.config.get("group_admin_admins_by_group", {}) or {}
        if isinstance(by_group, dict):
            g_list = self._store.normalize_qq_list_value(by_group.get(str(group_id)))
            if g_list:
                return g_list
        return []

    def has_group_admin_rights(self, user_id: str, group_id: str, raw: dict) -> bool:
        """设管理/取消管理权限：设管理专项名单或插件管理员（群管理员/群主）。

        专项名单来源见 _effective_group_admin_admins（群内指令 > WebUI 按群名单）。
        """
        uid = str(user_id)
        if uid in self._effective_group_admin_admins(group_id):
            return True
        if self._is_group_admin_or_owner(raw):
            return True
        return False

    # ---------- 违规检测管理命令前置校验 ----------

    def _moderation_require_admin(self, event):
        """校验消息发送者是否为插件管理员。是则返回 user_id，否则返回 None。
        #132：群管理员与群主也视为插件管理员。"""
        raw = self._mp._get_raw_message(event)
        if not raw or not raw.get("group_id"):
            return None
        sender_id = str(raw.get("user_id", ""))
        if not self._is_authorized(raw, sender_id):
            return None
        return sender_id

    async def _moderation_require_admin_msg(self, event) -> bool:
        """校验插件管理员/群聊环境，不通过则发提示并返回 False。
        必须保持为普通 async 函数（不能 yield），否则 18 个调用点拿不到 bool。"""
        if self._moderation_require_admin(event) is not None:
            return True
        raw = self._mp._get_raw_message(event)
        if raw and not raw.get("group_id"):
            await self._api._send(event, self._mp._build_text("此指令只能在群聊中使用"))
            return False
        await self._api._send(event, self._mp._build_text("只有插件管理员可执行此操作"))
        return False
