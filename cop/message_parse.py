"""L2 消息解析层：从事件 / OneBot 原始报文中提取文本、AT、图片、语音、命令等。

迁出自 main.py 的消息解析方法。依赖：
- L0 text_utils：_parse_qq_list
- L0 constants：GM_COMMAND_NAMES（_extract_command_tail / _is_plugin_command）
- 同层 OneBotApi 实例（构造注入）：仅 _extract_reply_image_url 需要 _call_action_fallback
- AstrBot 消息组件 Plain / At（_build_text）

依赖规则：不 import 同层模块，OneBotApi 通过构造注入（TYPE_CHECKING + 字符串注解）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import At, Plain

from .constants import GM_COMMAND_NAMES
from .text_utils import _parse_qq_list

if TYPE_CHECKING:
    from .onebot_api import OneBotApi


class MessageParser:
    """无状态的报文/消息链解析工具集合。"""

    def __init__(self, *, onebot_api: "OneBotApi"):
        # 仅供 _extract_reply_image_url 回退拉取被引用消息使用
        self._api = onebot_api

    def _get_group_id_or_none(self, event) -> str:
        """从事件取群号；非群聊返回空串。review#192：raw 访问异常统一兜底为空串。"""
        try:
            raw = self._get_raw_message(event)
            if not raw or not raw.get("group_id"):
                return ""
            return str(raw.get("group_id"))
        except Exception:
            return ""

    def _get_raw_message(self, event: AstrMessageEvent):
        """Robustly extract the raw message dict from the event."""
        try:
            return event.message_obj.raw_message
        except Exception:
            pass
        return getattr(event, "raw_message", None)

    def _parse_qq(self, text: str) -> str:
        nums = _parse_qq_list(text)
        return nums[0] if nums else ""

    def _extract_at_qq(self, raw: dict) -> str:
        """Extract first QQ number from At components in the raw message."""
        if not raw:
            return ""
        for seg in raw.get("message", []):
            if isinstance(seg, dict) and seg.get("type") == "at":
                qq = str(seg.get("data", {}).get("qq", ""))
                if qq:
                    return qq
        return ""

    def _extract_at_qqs(self, raw: dict) -> list:
        """Extract all QQ numbers from At components in the raw message."""
        if not raw:
            return []
        qqs = []
        for seg in raw.get("message", []):
            if isinstance(seg, dict) and seg.get("type") == "at":
                qq = str(seg.get("data", {}).get("qq", ""))
                if qq and qq not in qqs:
                    qqs.append(qq)
        return qqs

    def _extract_image_url(self, event: AstrMessageEvent) -> str:
        """从 event 的消息链中提取第一张图片的 URL。"""
        try:
            chain = getattr(event.message_obj, "message", None) or getattr(event, "message", None)
            if chain is None:
                return ""
            # AstrBot 的 message chain 可能是 MessageChain 或 list
            if hasattr(chain, "chain"):
                segs = chain.chain
            elif isinstance(chain, (list, tuple)):
                segs = chain
            else:
                segs = []
            return self._pick_image_url(segs)
        except Exception as e:
            logger.error(f"提取图片URL失败: {e}")
        return ""

    @staticmethod
    def _pick_image_url(segs) -> str:
        """从消息段列表中取出第一张图片的 url/file。"""
        for seg in segs or []:
            # 兼容不同的 Image 表示
            if isinstance(seg, dict):
                if seg.get("type") == "image":
                    data = seg.get("data") or {}
                    return data.get("url") or data.get("file") or ""
                continue
            if getattr(seg, "type", None) == "image":
                return getattr(seg, "url", "") or getattr(seg, "file", "")
        return ""

    async def _extract_reply_image_url(self, event: AstrMessageEvent) -> str:
        """#230：从被引用（回复）的消息里提取图片。

        `/改群头像` 等指令要求"引用图片消息"，但图片在**被引用**的那条消息里，
        旧实现只看当前消息链，所以永远提示"请引用一条图片消息"。
        这里通过 get_msg 拉取被引用消息后再取图。
        """
        reply_id = self._get_reply_id(event)
        if not reply_id:
            return ""
        ok, res = await self._api._call_action_fallback(event, ("get_msg",),
                                                        message_id=int(reply_id))
        if not ok or not isinstance(res, dict):
            # 回退：AstrBot 部分适配器会把被引用消息直接挂在 reply 段上
            return self._pick_image_url(self._replied_segments_from_event(event))
        data = res.get("data") if isinstance(res.get("data"), (dict, list)) else res
        segs = data.get("message") if isinstance(data, dict) else data
        url = self._pick_image_url(segs)
        if url:
            return url
        return self._pick_image_url(self._replied_segments_from_event(event))

    def _replied_segments_from_event(self, event: AstrMessageEvent):
        """兼容直接从当前消息链的 reply/quote 段里取被引用消息内容（部分适配器会内联）。"""
        chain = getattr(event.message_obj, "message", None) or getattr(event, "message", None)
        segs = getattr(chain, "chain", chain) if chain is not None else []
        if not isinstance(segs, (list, tuple)):
            return []
        out = []
        for seg in segs:
            if isinstance(seg, dict) and seg.get("type") in ("reply", "quote"):
                data = seg.get("data") or {}
                inner = data.get("message") or data.get("content")
                if isinstance(inner, list):
                    out.extend(inner)
                elif isinstance(data.get("url"), str) and data.get("url"):
                    out.append({"type": "image", "data": {"url": data["url"]}})
        return out

    def _build_text(self, text: str, at: str = None):
        if at:
            return [Plain(text), At(qq=at)] if text else [At(qq=at)]
        return [Plain(text)]

    def _get_reply_id(self, event: AstrMessageEvent):
        """提取被引用/回复的消息 ID。优先从 message_obj，回退 raw message 字段。"""
        mo = getattr(event, "message_obj", None)
        if mo:
            for attr in ("reply_id", "quote_id"):
                v = getattr(mo, attr, None)
                if v:
                    return str(v)
        # 尝试从 raw message 的 segment 中找 Reply 类型
        raw = self._get_raw_message(event)
        if isinstance(raw, dict):
            for seg in raw.get("message", []) or []:
                if isinstance(seg, dict):
                    t = seg.get("type")
                    if t in ("reply", "quote"):
                        data = seg.get("data", {})
                        rid = data.get("id") or data.get("message_id")
                        if rid:
                            return str(rid)
        return None

    def _extract_message_content_from_segments(self, segments) -> str:
        """从 OneBot 消息段列表提取纯文本内容预览（图片/表情/@/引用 标记化）。"""
        content_parts = []
        for seg in segments or []:
            if not isinstance(seg, dict):
                continue
            stype = seg.get("type")
            if stype == "text":
                text = seg.get("data", {}).get("text", "").strip()
                if text:
                    content_parts.append(text)
            elif stype == "image":
                content_parts.append("[图片]")
            elif stype == "face":
                content_parts.append("[表情]")
            elif stype == "at":
                content_parts.append("[@]")
            elif stype == "reply":
                content_parts.append("[引用]")
            elif stype == "record":
                content_parts.append("[语音]")
        content = "".join(content_parts)
        return content[:50] if content else "[无文本内容]"

    def _extract_audio_urls(self, segments) -> list:
        """从 OneBot 消息段提取语音/音频 URL（type=record/data.url 或 type=audio）。"""
        urls = []
        for seg in segments or []:
            if not isinstance(seg, dict):
                continue
            stype = seg.get("type")
            data = seg.get("data") or {}
            if stype in ("record", "audio"):
                url = data.get("url") or data.get("file") or ""
                if url:
                    urls.append(url)
        return urls

    def _extract_audio_url(self, segments) -> str:
        """返回第一条语音 URL，没有则返回空串。"""
        urls = self._extract_audio_urls(segments)
        return urls[0] if urls else ""

    def _strip_command_prefix(self, text: str) -> str:
        """剥离开头的命令前缀符号（/、.、!、# 等）。"""
        return text.lstrip("/.!#$%^&*~-+=?，。、！!＠@＃#＄$％%＾^＆&＊*～~｀`｜|＼\\ 　\t")

    def _extract_command_tail(self, raw_text: str, cmd_names) -> str:
        """从原始文本提取命令名之后的参数部分；非本命令返回空串。"""
        if not raw_text:
            return ""
        stripped = self._strip_command_prefix(raw_text)
        if not stripped:
            return ""
        # 按长度降序匹配，避免短命令名先命中（如"撤回"先于"撤回自身"）
        for cmd in sorted(cmd_names, key=len, reverse=True):
            if stripped.startswith(cmd):
                return stripped[len(cmd):].strip()
        return ""

    # 原类属性 _GM_COMMAND_NAMES 已迁至 cop/constants.py 模块常量 GM_COMMAND_NAMES。

    def _is_plugin_command(self, text: str) -> bool:
        """判断文本是否为本插件的指令消息（用户发送）。
        指令消息不记录进历史，避免编号偏移。"""
        t = (text or "").strip()
        if not t:
            return False
        stripped = self._strip_command_prefix(t)
        if not stripped:
            return False
        # 指令词后必须是空格、数字、逗号、@ 或结尾，避免误过滤普通聊天
        for cmd in sorted(GM_COMMAND_NAMES, key=len, reverse=True):
            if stripped.startswith(cmd):
                after = stripped[len(cmd):]
                if after == "" or after[0] in (" ", "\t", ",", "，", "@", "0", "1", "2", "3", "4", "5", "6", "7", "8", "9"):
                    return True
        return False

    def _extract_text(self, raw: dict) -> str:
        parts = []
        for seg in raw.get("message", []) or []:
            if isinstance(seg, dict) and seg.get("type") == "text":
                parts.append(seg.get("data", {}).get("text", ""))
        return "".join(parts)

    @staticmethod
    def _extract_text_from_chain(chain) -> str:
        """从待发送/已发送的消息链（AstrBot 消息组件列表）提取纯文本。

        用于自动撤回关键词匹配：after_message_sent 中只能从 event.get_result().chain
        拿到 bot 本次实际发出的内容。兼容组件对象（Plain.text）与 dict 两种形态。
        """
        parts = []
        for comp in chain or []:
            text = getattr(comp, "text", None)
            if text is None and isinstance(comp, dict):
                data = comp.get("data")
                text = data.get("text") if isinstance(data, dict) else None
            if text:
                parts.append(str(text))
        return "".join(parts)

    @staticmethod
    def _plain_text_from_segments(segments) -> str:
        """从 OneBot 消息段列表提取纯文本（不截断，用于关键词匹配）。"""
        parts = []
        for seg in segments or []:
            if isinstance(seg, dict) and seg.get("type") == "text":
                parts.append(str(seg.get("data", {}).get("text", "")))
        return "".join(parts)

    @staticmethod
    def _parse_history_messages(result) -> list:
        """兼容多种返回结构，从 get_group_msg_history 返回值中取出消息列表。"""
        if isinstance(result, list):
            return [m for m in result if isinstance(m, dict)]
        if not isinstance(result, dict):
            return []
        data = result.get("data")
        if isinstance(data, dict) and isinstance(data.get("messages"), list):
            return [m for m in data["messages"] if isinstance(m, dict)]
        if isinstance(data, list):
            return [m for m in data if isinstance(m, dict)]
        if isinstance(result.get("messages"), list):
            return [m for m in result["messages"] if isinstance(m, dict)]
        return []

    def _message_at_bot(self, raw: dict, self_id: str) -> bool:
        """判断消息中是否 @ 了 bot 自身。"""
        if not raw or not self_id:
            return False
        for seg in raw.get("message", []) or []:
            if isinstance(seg, dict) and seg.get("type") == "at":
                if str(seg.get("data", {}).get("qq", "")) == str(self_id):
                    return True
        return False

    def _extract_message_text_with_mentions(self, raw: dict, skip_qq: str = "") -> str:
        """提取文本，把 @ 段替换为可见「@名字」便于口语化匹配；跳过 bot 自身提及。

        OneBot 的 @ 段没有 QQ 号时（如复制文字里的 @张三），data.qq 可能缺失，
        统一折成 "@名字" 文本参与「@他/@张三」等目标判断。
        """
        parts = []
        for seg in raw.get("message", []) or []:
            if not isinstance(seg, dict):
                continue
            stype = seg.get("type")
            data = seg.get("data") or {}
            if stype == "text":
                parts.append(str(data.get("text", "")))
            elif stype == "at":
                qq = str(data.get("qq", "") or "")
                if skip_qq and qq == str(skip_qq):
                    continue  # 跳过 @bot 自身，避免污染意图词
                name = data.get("name") or qq or ""
                parts.append(f" @{name} " if name else " @ ")
        return "".join(parts)

    def _extract_at_targets(self, raw: dict) -> dict:
        """提取消息中的 @ 目标映射 {名称或QQ: QQ}。"""
        targets = {}
        for seg in raw.get("message", []) or []:
            if isinstance(seg, dict) and seg.get("type") == "at":
                data = seg.get("data") or {}
                qq = str(data.get("qq", "") or "")
                name = str(data.get("name", "") or "")
                if qq:
                    targets[name or qq] = qq
        return targets

    @staticmethod
    def _strip_mention_tokens(text: str) -> str:
        """去掉 @名字/@ 占位，避免名字里的数字被当作 QQ 号。"""
        import re
        t = re.sub(r"@\S+", " ", text or "")
        return t.replace(" @ ", " ")


# ===================== L2 目标解析（原 target_resolver.py 并入） =====================
# 合并理由：同层 L2、同属「raw 报文 / 目标解析」，且 TargetResolver 直接依赖 MessageParser
# 实例（同模块合并后无需跨模块 TYPE_CHECKING），行为逐字保留、对外类名不变。


class TargetResolver:
    """口语化指令目标解析：@提及 > 回复消息 > 群内 QQ 号 > @名字。"""

    def __init__(self, *, message_parse: "MessageParser", onebot_api: "OneBotApi"):
        self._mp = message_parse
        self._api = onebot_api

    def _resolve_text_target_by_name(self, raw: dict, text: str):
        """从文本里的「@名字 / 名字」匹配本群成员 QQ（需 @ 段带 name）。未找到返回 None。"""
        targets = self._mp._extract_at_targets(raw)
        if not targets:
            return None
        # 优先匹配 @名字
        for name, qq in targets.items():
            if name and qq and name in text:
                return qq
        return None

    async def _resolve_quoted_target(self, event, raw: dict) -> str:
        """回复消息时取被回复消息发送者的 QQ；取不到返回空串。"""
        _name, qq = await self._resolve_quoted_sender(event, raw)
        return qq

    async def _resolve_quoted_sender(self, event, raw: dict) -> tuple:
        """取被回复消息发送者的 (昵称, QQ号)；取不到返回 ("", "")。

        #227：供 /举报 在未 @ 成员时，通过「引用被举报成员的消息」定位目标。
        """
        reply_id = self._mp._get_reply_id(event)
        if not reply_id:
            return "", ""
        try:
            ok, res = await self._api._call_action_fallback(event, ("get_msg",), message_id=int(reply_id))
        except Exception:
            return "", ""
        if not ok or not isinstance(res, dict):
            return "", ""
        data = res.get("data") if isinstance(res.get("data"), dict) else res
        if isinstance(data, dict):
            sender = data.get("sender") or {}
            qq = sender.get("user_id") or data.get("user_id")
            if qq:
                name = str(sender.get("card") or sender.get("nickname") or "")
                return name, str(qq)
        return "", ""

    async def _resolve_colloquial_target(self, event, raw: dict, text: str) -> str:
        """口语化指令的目标解析：@提及 > 回复消息 > 群内 QQ 号 > @名字。"""
        # 1) @ 提及（排除 bot 自身）
        self_id = self._api._get_self_id(event, raw)
        ats = [q for q in self._mp._extract_at_qqs(raw) if str(q) != str(self_id)]
        if ats:
            return str(ats[0])
        # 2) 回复消息
        quoted = await self._resolve_quoted_target(event, raw)
        if quoted:
            return quoted
        # 3) 文本中的 QQ 号（5-12 位）
        nums = _parse_qq_list(self._mp._strip_mention_tokens(text))
        if nums:
            return nums[0]
        # 4) @名字 文本
        by_name = self._resolve_text_target_by_name(raw, text)
        if by_name:
            return by_name
        return ""
