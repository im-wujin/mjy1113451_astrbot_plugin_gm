"""L3b 群违规检测子域：文本检测（刷屏/涉政/骂人/广告/链接/群号推广）。

迁出自 main.py 的以下方法（逐字保留逻辑等价）：
    _check_spam / _check_political / _collect_profanity_keywords
    _check_profanity / _check_profanity_with_ai / _normalize_severity
    _check_ad / _normalize_host / _is_link_whitelisted / _check_link
    _check_group_promotion

依赖（构造注入，L3b 只依赖 L0/L1/L2，不 import 同层）：
- ConfigStore（L1）：config / get_group_setting
- RuntimeState（L2）：spam_records

延迟导入约束：`_is_link_whitelisted` 内 urlparse 保持函数内局部导入（与原实现一致）。
"""

from __future__ import annotations

import json
import re
import time
from typing import TYPE_CHECKING

from astrbot.api import logger

from ..compat import aiohttp
from ..constants import _DEFAULT_PROFANITY_PROMPT

if TYPE_CHECKING:
    from ..config_store import ConfigStore
    from ..runtime import RuntimeState


class TextModeration:
    """文本违规检测领域服务。"""

    def __init__(
        self,
        *,
        config_store: "ConfigStore",
        runtime: "RuntimeState",
    ):
        self._store = config_store
        self.config = config_store.config
        self._runtime = runtime

    # ===================== 刷屏检测 =====================

    async def _check_spam(self, group_id: str, user_id: str) -> bool:
        try:
            threshold = int(self._store.get_group_setting(group_id, "spam_threshold", 5) or 5)
            window = int(self._store.get_group_setting(group_id, "spam_time_window", 10) or 10)
        except (TypeError, ValueError):
            return False
        if not self._store.get_group_setting(group_id, "spam_check_enabled", True):
            return False
        if threshold <= 0 or window <= 0:
            return False
        now = time.time()
        key = f"{group_id}_{user_id}"
        records = self._runtime.spam_records[key]
        records[:] = [t for t in records if now - t < window]
        records.append(now)
        return len(records) >= threshold

    # ===================== 骂人检测 =====================

    def _check_political(self, msg_text: str) -> str:
        """#204：涉政关键词命中检测（全局配置 political_keywords），返回命中词或空串。

        涉政清单为硬清单：不受 AI 模式/管理员豁免影响前的文本检测顺序由 dispatch 保证。
        """
        if not msg_text:
            return ""
        text_lower = msg_text.lower()
        for kw in self.config.get("political_keywords", []) or []:
            k = str(kw).lower()
            if k and k in text_lower:
                return str(kw)
        return ""

    def _collect_profanity_keywords(self, group_id: str) -> list:
        """汇总骂人/违禁词关键词列表（review#208 config-coverage-regression）。

        来源清单（扩展时勿遗漏）：
        1. group_overrides[gid]["profanity_keywords"]（按群指令写入）
        2. top-level config["profanity_keywords"]（全局默认；无按群覆盖时由 get_group_setting 返回）
        3. 旧 config["violation_keywords"]（WebUI 历史入口，兼容保留）
        """
        kws = list(self._store.get_group_setting(group_id, "profanity_keywords", []) or [])
        for src in (
            self.config.get("profanity_keywords", []) or [],
            self.config.get("violation_keywords", []) or [],
        ):
            for k in src:
                if k not in kws:
                    kws.append(k)
        return kws

    async def _check_profanity(self, msg_text: str, event, group_id: str, user_id: str):
        """检测骂人：返回 (是否违规, 严重程度)。

        #243：严重程度（mild/medium/severe）仅在 AI 判定骂人时由模型返回；
        关键词硬清单命中不给分级（沿用 profanity_ban_duration），
        调用方无分级时按固定时长处理。
        """
        if not self._store.get_group_setting(group_id, "profanity_check_enabled", True):
            return False, ""
        if not msg_text:
            return False, ""
        # #207：违禁词为硬清单，优先匹配且不受 AI 模式影响；
        # 来源统一由 _collect_profanity_keywords 收敛（按群/全局/旧 violation_keywords）
        keywords = self._collect_profanity_keywords(group_id)
        text_lower = msg_text.lower()
        for kw in keywords:
            if str(kw).lower() and str(kw).lower() in text_lower:
                logger.warning(f"[群违规检测] 命中违禁词 用户 {user_id}: {kw}")
                return True, ""
        use_ai = bool(self._store.get_group_setting(group_id, "profanity_use_ai", True))
        if use_ai and aiohttp is not None:
            api_endpoint = self.config.get("api_endpoint", "")
            api_key = self.config.get("api_key", "")
            if api_endpoint:
                is_profanity, reason, severity = await self._check_profanity_with_ai(
                    api_endpoint, api_key, msg_text, group_id)
                if is_profanity:
                    logger.warning(f"[群违规检测] 骂人 用户 {user_id} {reason} 严重度={severity or '未给出'}")
                    return True, severity
        return False, ""

    async def _check_profanity_with_ai(self, api_endpoint: str, api_key: str, msg_text: str,
                                       group_id: str = ""):
        """AI 判骂人，返回 (是否骂人, 原因, 严重程度)。

        #243：提示词要求模型同时输出 severity（mild/medium/severe），解析做容错——
        字段缺失或非法时返回空串，由调用方回退到固定禁言时长（等价旧行为）。
        提示词取配置 `profanity_detection_prompt`（可按群覆盖），留空用内置默认；
        含 `{text}` 占位符时替换为待检测文本，否则把文本追加在末尾。
        """
        model_name = self.config.get("model_name", "gpt-4o")
        custom_prompt = str(
            self._store.get_group_setting(group_id, "profanity_detection_prompt", "") or ""
        ).strip()
        prompt = custom_prompt or _DEFAULT_PROFANITY_PROMPT
        if "{text}" in prompt:
            content_text = prompt.replace("{text}", msg_text)
        else:
            content_text = f"{prompt}\n\n待检测文本：{msg_text}"
        payload = {
            "model": model_name,
            "messages": [{"role": "user", "content": content_text}],
            "max_tokens": 200,
            "temperature": 0.1,
        }
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        try:
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession() as session:
                async with session.post(api_endpoint, json=payload, headers=headers, timeout=timeout) as resp:
                    if resp.status != 200:
                        logger.error(f"[群违规检测] 骂人 AI 失败: {resp.status}")
                        return False, "", ""
                    data = await resp.json()
            content = (((data.get("choices") or [{}])[0]).get("message") or {}).get("content", "")
            content = content.strip()
            if content.startswith("```"):
                lines = content.split("\n")
                content = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
            match = re.search(r"\{[^{}]*\}", content, re.DOTALL)
            text = match.group() if match else content
            obj = json.loads(text)
            return (bool(obj.get("is_profanity", False)), str(obj.get("reason", "")),
                    self._normalize_severity(obj.get("severity")))
        except Exception as e:
            logger.error(f"[群违规检测] 骂人 AI 解析失败: {e}")
            return False, "", ""

    @staticmethod
    def _normalize_severity(value) -> str:
        """#243：把模型返回的严重程度归一化；无法识别时返回空串（回退固定时长）。"""
        if value is None:
            return ""
        v = str(value).strip().lower()
        alias = {
            "mild": "mild", "low": "mild", "轻微": "mild", "轻度": "mild", "1": "mild",
            "medium": "medium", "moderate": "medium", "中等": "medium", "中度": "medium", "2": "medium",
            "severe": "severe", "high": "severe", "严重": "severe", "重度": "severe", "3": "severe",
        }
        return alias.get(v, "")

    # ===================== 广告 / 链接 / 群号推广 =====================

    async def _check_ad(self, msg_text: str, event, group_id: str, user_id: str) -> bool:
        if not self._store.get_group_setting(group_id, "ad_check_enabled", True):
            return False
        if not msg_text:
            return False
        keywords = self._store.get_group_setting(group_id, "ad_keywords", []) or []
        text_lower = msg_text.lower()
        for kw in keywords:
            if str(kw).lower() and str(kw).lower() in text_lower:
                return True
        return False

    @staticmethod
    def _normalize_host(raw_host: str) -> str:
        """归一化域名：去端口、去 www. 前缀、小写。"""
        h = raw_host.split(":")[0].lower().strip(".")
        return h.removeprefix("www.")

    def _is_link_whitelisted(self, msg_text: str, group_id: str) -> bool:
        """#195：检查消息中的链接是否命中白名单（全局+按群）。命中则不触发链接检测。
        匹配规则：精确域名匹配（已做归一化去端口/去 www./小写）。
        白名单条目支持通配前缀 `*.example.com` 表示匹配该域及其所有子域。"""
        if not msg_text:
            return False
        # 提取消息中所有链接的 hostname（用 urlparse 兼容带端口/路径的 URL）
        from urllib.parse import urlparse
        hosts = set()
        for url_match in re.finditer(r"https?://[^\s]+", msg_text, re.IGNORECASE):
            url = url_match.group(0)
            parsed = urlparse(url)
            if parsed.hostname:
                hosts.add(self._normalize_host(parsed.hostname))
        # 也匹配裸 www.example.com（无协议前缀）
        for www_match in re.finditer(r"(?:^|\s)www\.[^\s]+", msg_text, re.IGNORECASE):
            url = www_match.group(0).strip()
            parsed = urlparse("http://" + url)
            if parsed.hostname:
                hosts.add(self._normalize_host(parsed.hostname))
        if not hosts:
            return False
        # 构建归一化白名单集合（精确条目 + 通配条目分开）
        raw_global = self.config.get("link_whitelist", []) or []
        raw_group = self._store.get_group_setting(group_id, "link_whitelist", []) or []
        exact_wl = set()
        wildcard_wl = set()  # 通配域名（去掉 *. 前缀）
        for entry in list(raw_global) + list(raw_group):
            e = self._normalize_host(str(entry))
            if e.startswith("*."):
                wildcard_wl.add(e[2:])  # "*.example.com" → "example.com"
            else:
                exact_wl.add(e)
        # 精确匹配
        if hosts & exact_wl:
            return True
        # 通配匹配：host 以 ".suffix" 结尾或等于 suffix
        for host in hosts:
            for domain in wildcard_wl:
                if host == domain or host.endswith("." + domain):
                    return True
        return False

    async def _check_link(self, msg_text: str, event, group_id: str, user_id: str) -> bool:
        if not self._store.get_group_setting(group_id, "link_check_enabled", False):
            return False
        if not msg_text:
            return False
        # #195：链接白名单命中则跳过
        if self._is_link_whitelisted(msg_text, group_id):
            return False
        pattern = r"(https?://[^\s]+|www\.[^\s]+\.[^\s]+|[^\s]+\.(com|cn|net|org|io|xyz|top|vip|cc|me|tv|edu|gov)[^\s]*)"
        return re.search(pattern, msg_text, re.IGNORECASE) is not None

    async def _check_group_promotion(self, msg_text: str, event, group_id: str, user_id: str) -> bool:
        if not self._store.get_group_setting(group_id, "group_promotion_check_enabled", True):
            return False
        if not msg_text:
            return False
        promotion_keywords = ["进群", "加群", "群号", "入群", "拉群", "建群"]
        if not any(kw in msg_text for kw in promotion_keywords):
            return False
        group_pattern = r"[;；:,，\s]*(\d{5,12})"
        return bool(re.findall(group_pattern, msg_text))


__all__ = ["TextModeration"]
