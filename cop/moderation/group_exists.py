"""L3b 群违规检测子域：QQ 群号存在性探测（群头像比对）。

检测思路（owner 提出）：
    GET https://p.qlogo.cn/gh/{群号}/{群号}/0
- 群号真实存在 → 返回该群的真实头像；
- 群号不存在   → 返回腾讯默认群头像（MD5 固定为 185ff6f0cfc14f3bb8b838288d7dcc3c）。
因此把响应体 MD5 与该固定值比对，即可判定群号是否存在。

返回值语义（三态）：
- True ：群号存在（头像非默认头像）；
- False：群号不存在（头像 MD5 与默认头像一致）；
- None ：无法判定（aiohttp 缺失 / 网络异常 / 非 200 / 开关关闭）；
         调用方按「无法判定不处置」的保守策略处理，避免网络抖动导致误撤回误禁言。

依赖（构造注入，L3b 只依赖 L0/L1/L2，不 import 同层）：
- ConfigStore（L1）：config / get_group_setting（读按群覆盖的缓存 TTL 与开关）
- RuntimeState（L2）：group_exists_cache（进程内 TTL 缓存，不持久化）

延迟导入约束：本模块使用 compat 提供的模块级 aiohttp（与 image.py 一致）。
"""

from __future__ import annotations

import hashlib
import time
from typing import TYPE_CHECKING

from astrbot.api import logger

from ..compat import aiohttp
from ..constants import (
    _GROUP_AVATAR_URL_TEMPLATE,
    _GROUP_DEFAULT_AVATAR_MD5,
    _GROUP_EXISTS_CACHE_TTL,
    _GROUP_EXISTS_TIMEOUT,
)

if TYPE_CHECKING:
    from ..config_store import ConfigStore
    from ..runtime import RuntimeState


class GroupExistsProbe:
    """QQ 群号存在性探测器（默认头像 MD5 比对 + 进程内 TTL 缓存）。"""

    def __init__(
        self,
        *,
        config_store: "ConfigStore",
        runtime: "RuntimeState",
    ):
        self._store = config_store
        self.config = config_store.config
        self._runtime = runtime

    # ===================== 对外探测入口 =====================

    async def check_group_exists(self, group_number: str, group_id: str = "") -> bool | None:
        """探测 QQ 群号是否存在。

        参数：
        - group_number：待探测的群号（5-12 位纯数字，非法格式直接返回 None）；
        - group_id：当前会话群号，用于读取按群覆盖的配置（可留空）。

        返回 True / False / None，语义见模块 docstring。命中缓存时直接复用结果，
        不发起网络请求；探测成功（非 None）时写入缓存。
        群号推广存在性验证开关 `group_promotion_exists_check` 关闭时直接返回 None（跳
        过探测，调用方按「未验证不处置」处理，即退化为原纯关键词检测行为）。
        """
        if not bool(self._store.get_group_setting(
                group_id or "", "group_promotion_exists_check", True)):
            return None
        gid = str(group_number or "").strip()
        if not gid.isdigit() or not (5 <= len(gid) <= 12):
            return None
        cache = self._cache()
        ttl = self._cache_ttl(group_id)
        cached = cache.get(gid)
        if isinstance(cached, tuple) and len(cached) == 2 and ttl > 0:
            exists, ts = cached
            try:
                if time.time() - float(ts) < ttl:
                    return bool(exists)
            except (TypeError, ValueError):
                pass
        result = await self._probe_remote(gid)
        if result is not None:
            cache[gid] = (result, time.time())
        return result

    # ===================== 缓存 =====================

    def _cache(self) -> dict:
        return self._runtime.group_exists_cache

    def _cache_ttl(self, group_id: str) -> int:
        """缓存有效期（秒），可按群覆盖 `group_promotion_exists_cache_ttl`；0 表示不缓存。"""
        try:
            return max(
                0,
                int(
                    self._store.get_group_setting(
                        group_id or "",
                        "group_promotion_exists_cache_ttl",
                        _GROUP_EXISTS_CACHE_TTL,
                    )
                ),
            )
        except (TypeError, ValueError):
            return _GROUP_EXISTS_CACHE_TTL

    # ===================== 实际探测 =====================

    async def _probe_remote(self, gid: str) -> bool | None:
        """请求群头像并与默认头像 MD5 比对；无法判定时返回 None。"""
        if aiohttp is None:
            logger.warning("[群号检测] aiohttp 未安装，跳过群号存在性校验")
            return None
        url = _GROUP_AVATAR_URL_TEMPLATE.format(group_id=gid)
        try:
            timeout = aiohttp.ClientTimeout(total=_GROUP_EXISTS_TIMEOUT)
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=timeout) as resp:
                    if resp.status != 200:
                        logger.warning(f"[群号检测] 群 {gid} 头像请求失败: HTTP {resp.status}")
                        return None
                    data = await resp.read()
        except Exception as e:
            logger.warning(f"[群号检测] 群 {gid} 头像请求异常: {e}")
            return None
        if not data:
            return None
        exists = hashlib.md5(data).hexdigest() != _GROUP_DEFAULT_AVATAR_MD5
        logger.debug(f"[群号检测] 群 {gid} 头像比对结果: {'存在' if exists else '不存在'}")
        return exists


__all__ = ["GroupExistsProbe"]