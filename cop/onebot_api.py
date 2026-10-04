"""L2 OneBot API 封装层：调用各协议端 action、消息收发、撤回、管理操作等。

迁出自 main.py 的 OneBot API 封装方法。依赖：
- L0 compat：MessageChain（_send）
- L1 ConfigStore（构造注入）：get_group_setting（_notify_admins）
- astrbot Context（构造注入）：send_private_msg / send_group_msg / get_stranger_info 等
- AstrBot 消息组件 Plain（_send_group_text 回退）

依赖规则：不 import 同层模块。_recall_user_recent_msgs 属 L3，本阶段不迁移（留待阶段 3）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import Plain

from .compat import MessageChain

if TYPE_CHECKING:
    from .config_store import ConfigStore


class OneBotApi:
    """OneBot action 调用与消息收发封装。"""

    def __init__(self, *, context, config_store: "ConfigStore"):
        self.context = context
        self._store = config_store

    # ---------- 基础发送 ----------

    async def _send(self, event: AstrMessageEvent, message_list):
        try:
            if hasattr(event, "send"):
                if MessageChain is not None:
                    await event.send(MessageChain(message_list))
                else:
                    # 理论上不会走到（依赖缺失），保留兜底并告警便于排查 #246
                    logger.warning("MessageChain 不可用，尝试以原始列表发送消息")
                    await event.send(message_list)
                return True
        except Exception as e:
            logger.error(f"发送消息失败: {e}")
        return False

    # ---------- 结果判定 ----------

    def _action_result_success(self, result) -> bool:
        """把 OneBot 调用返回值规整为布尔成功/失败。"""
        if result is None:
            return True
        if isinstance(result, bool):
            return result
        if isinstance(result, dict):
            status = str(result.get("status", "")).lower()
            if status in {"failed", "error"}:
                return False
            retcode = result.get("retcode")
            if retcode is not None:
                try:
                    return int(retcode) == 0
                except (TypeError, ValueError):
                    return False
            if status in {"ok", "async"}:
                return True
        return bool(result)

    def _describe_action_failure(self, result, action: str) -> str:
        """把 OneBot 失败响应解析为可读原因，区分未实现/未知 action/预置限制/权限不足/其他。"""
        if result is None:
            return f"当前 OneBot 实现可能不支持此 API（{action} 无可用调用通路）"
        if isinstance(result, str):
            return (result.strip() or f"{action} 调用失败")[:100]
        if isinstance(result, dict):
            retcode = result.get("retcode")
            message = str(result.get("message") or result.get("msg") or
                          result.get("wording") or "").strip()
            low = message.lower()
            # 无 bot 直调通路且其它调用方式都失败：等价于当前实现不支持此 API
            if "无可用调用通路" in message:
                return f"当前 OneBot 实现可能不支持此 API（{action} 无可用调用通路）"
            # #245：协议端没有这个 action（如 NapCat/Lagrange 返回 "unknown action"）
            if any(k in low for k in ("unknown action", "unknown method", "no such action",
                                      "action not found", "unimplemented")):
                return f"当前 OneBot 实现不支持 {action} 这个接口"
            if retcode == 10002 or any(
                    k in low for k in ("not support", "unsupported", "not implement",
                                       "未实现", "不支持")):
                return f"当前 OneBot 实现可能不支持此 API（{action}）"
            # 群标签类失败（协议端仅允许预置列表中的标签）
            if any(k in low for k in ("group tag", "tag not", "label")) or "标签" in message:
                return "当前 OneBot 实现仅允许添加预置列表中的群标签"
            if any(k in low for k in ("permission", "denied", "forbidden", "不允许")) \
                    or "权限" in message or retcode in (1200,):
                return f"权限不足（{action} 需要更高权限，如群主/管理员身份）"
            if message:
                return f"后端返回：{message[:80]}"
            return f"调用失败(retcode={retcode})"
        return f"{action} 调用失败"

    # ---------- 通用 action 调用 ----------

    async def _execute_action(self, event: AstrMessageEvent, action: str, return_raw: bool = False, **params):
        """调用 OneBot API。
        优先尝试 event.bot.call_action（AstrBot 推荐方式），
        其次 fallback 到 self.context.{action} 和 event.{action}。
        默认返回 True/False；return_raw=True 时返回 API 原始结果（用于查询类 API）。
        """
        # 参数转换：group_id / user_id / message_id 转为 int（OneBot 要求）
        for k in ("group_id", "user_id", "message_id"):
            if k in params and isinstance(params[k], str) and params[k].isdigit():
                params[k] = int(params[k])

        bot = getattr(event, "bot", None)
        if bot is not None:
            call = getattr(bot, "call_action", None)
            if callable(call):
                try:
                    result = await call(action, **params)
                    if return_raw:
                        return result
                    return self._action_result_success(result)
                except Exception as e:
                    logger.error(f"bot.call_action({action}) 失败: {e}")
            api = getattr(bot, "api", None)
            if api is not None:
                call = getattr(api, "call_action", None)
                if callable(call):
                    try:
                        result = await call(action, **params)
                        if return_raw:
                            return result
                        return self._action_result_success(result)
                    except Exception as e:
                        logger.error(f"bot.api.call_action({action}) 失败: {e}")
        handler = getattr(self.context, action, None)
        if callable(handler):
            try:
                result = await handler(**params)
                if return_raw:
                    return result
                return self._action_result_success(result)
            except Exception as e:
                logger.error(f"调用 {action} 失败: {e}")
        if hasattr(event, action):
            handler = getattr(event, action)
            if callable(handler):
                try:
                    result = await handler(**params)
                    if return_raw:
                        return result
                    return self._action_result_success(result)
                except Exception as e:
                    logger.error(f"调用 event.{action} 失败: {e}")
        return None if return_raw else False

    async def _call_onebot_raw(self, event: AstrMessageEvent, action: str, **params):
        """#242: 以原始响应调用 OneBot action，区分"调用失败"与"无可用通路"。
        aiocqhttp 的 call_action 成功时只返回 data 字段（set 类 action 的 data
        多为 null，即 None），失败时抛 ActionFailed（e.result 含 retcode/wording）；
        而 _execute_action 会吞掉异常并返回 None，失败与成功无法区分。这里直调
        bot.call_action 把失败响应收进字典；无 bot 直调通路时回退 _execute_action，
        此时返回 None 视为无可用调用通路。"""
        # 参数转换：group_id / user_id / message_id 转为 int（OneBot 要求）
        for k in ("group_id", "user_id", "message_id"):
            if k in params and isinstance(params[k], str) and params[k].isdigit():
                params[k] = int(params[k])

        bot = getattr(event, "bot", None)
        call = getattr(bot, "call_action", None)
        if callable(call):
            try:
                return await call(action, **params)
            except Exception as e:
                result = getattr(e, "result", None)
                if isinstance(result, dict):
                    return result
                return {"status": "failed", "retcode": -1, "wording": str(e)}
        # 无 bot 直调通路：回退 _execute_action，None 视为无可用调用通路
        result = await self._execute_action(event, action, return_raw=True, **params)
        if result is None:
            return {"status": "failed", "retcode": -1, "wording": "无可用调用通路"}
        return result

    async def _call_action_fallback(self, event: AstrMessageEvent, actions, **params):
        """按顺序尝试多个候选 action，返回 (是否成功, 最后一个原始响应)。

        #232：旧实现用「返回 None 才回退」，而 _execute_action 失败返回的是 False，
        回退分支永远不可达，导致始终提示「不支持此 API」。这里统一用原始响应判定。
        用于各家 OneBot 实现 action 名不一致的场景（set_group_todo / kick / 加群审核等）。
        """
        last = None
        for action in actions:
            last = await self._call_onebot_raw(event, action, **params)
            if self._action_result_success(last):
                return True, last
        return False, last

    # ---------- 具体 action 封装 ----------

    async def _recall_message(self, event: AstrMessageEvent, message_id: str):
        """撤回消息。OneBot 标准 API 名为 delete_msg。"""
        return await self._execute_action(event, "delete_msg", message_id=message_id)

    async def _set_group_admin(self, event: AstrMessageEvent, group_id: str, qq: str, enable: bool):
        return await self._execute_action(event, "set_group_admin", group_id=group_id, user_id=qq, enable=enable)

    async def _set_group_title(self, event: AstrMessageEvent, group_id: str, qq: str, title: str):
        """设置群头衔。OneBot v11 set_group_special_title 接口。
        注意：不传 duration 参数（属于 set_group_ban 的参数，传了会导致 NapCatQQ 等静默失败）。
        """
        return await self._execute_action(event, "set_group_special_title",
                                          group_id=group_id, user_id=qq, special_title=title)

    async def _clear_group_title(self, event: AstrMessageEvent, group_id: str, qq: str) -> bool:
        """清空群头衔。每步调用后用 get_group_member_info 读回 title 字段校验
        是否真的清空，避免 OneBot 实现返回成功但实际未清空（#111 #119）。

        校验严格判断 title 是否为空字符串 / 字段缺失，不能 strip 后判空——
        否则单空格 " " 会被误判为已清空，导致实际仍存在空格头衔（#119）。
        """
        async def _verify() -> bool:
            info = await self._execute_action(
                event, "get_group_member_info", return_raw=True,
                group_id=group_id, user_id=qq, no_cache=True,
            )
            if isinstance(info, dict):
                data = info.get("data") or info
                after = data.get("title")
                if after is None:
                    after = data.get("special_title")
                # 严格判空：必须是空字符串或字段缺失；空格、不可见字符均视为未清空
                return after is None or after == ""
            return False

        # 1) duration=-1（部分实现要求的清空语义）
        ok1 = await self._execute_action(
            event, "set_group_special_title",
            group_id=group_id, user_id=qq,
            special_title="", duration=-1,
        )
        if ok1 and await _verify():
            return True
        # 2) 空字符串（不带 duration）
        ok2 = await self._execute_action(
            event, "set_group_special_title",
            group_id=group_id, user_id=qq, special_title="",
        )
        if ok2 and await _verify():
            return True
        # 3) 单空格兼容兜底：旧版 OneBot 拒绝空字符串时设置 " "。
        #    但需要校验：若 OneBot 实际把 " " 写回去了，#119 报告就是这种场景，
        #    此时不能视为成功；只有真正被解释为空（接口忽略空白）才算清空。
        ok3 = await self._execute_action(
            event, "set_group_special_title",
            group_id=group_id, user_id=qq, special_title=" ",
        )
        if ok3 and await _verify():
            return True
        return False

    def _get_self_id(self, event, raw=None) -> str:
        """获取 bot 自身 QQ 号，优先 event.get_self_id()，回退 raw 字段。"""
        try:
            sid = event.get_self_id()
            if sid:
                return str(sid)
        except Exception:
            pass
        if raw:
            sid = raw.get("self_id")
            if sid:
                return str(sid)
        return ""

    async def _do_recall(self, event, message_id) -> tuple:
        """撤回一条消息，返回 (成功, 错误信息)。识别 retcode=1200（消息已撤回或超时）。"""
        mid = str(message_id)
        mid_num = int(mid) if mid.isdigit() else mid
        bot = getattr(event, "bot", None)
        call = getattr(bot, "call_action", None)
        if callable(call):
            try:
                result = await call("delete_msg", message_id=mid_num)
                if isinstance(result, dict):
                    retcode = result.get("retcode")
                    if retcode is not None:
                        try:
                            if int(retcode) == 1200:
                                return False, "消息已撤回或超时"
                            if int(retcode) != 0:
                                return False, f"撤回失败(retcode={retcode})"
                        except (TypeError, ValueError):
                            pass
                return True, ""
            except Exception as e:
                retcode = getattr(e, "retcode", None)
                if retcode == 1200:
                    return False, "消息已撤回或超时"
                return False, f"撤回失败: {e}"
        ok = await self._recall_message(event, message_id)
        return (True, "") if ok else (False, "撤回失败")

    async def _set_group_card(self, event: AstrMessageEvent, group_id: str, qq: str, card: str):
        return await self._execute_action(event, "set_group_card",
                                          group_id=group_id, user_id=qq, card=card)

    async def _set_essence(self, event: AstrMessageEvent, message_id: str, group_id: str = None):
        """OneBot 标准 API 名为 set_essence_msg，部分实现也支持 set_essence。"""
        kwargs = {"message_id": message_id}
        if group_id is not None:
            kwargs["group_id"] = group_id
        # 优先尝试标准名 set_essence_msg，再回退 set_essence
        result = await self._execute_action(event, "set_essence_msg", **kwargs)
        if not result:
            result = await self._execute_action(event, "set_essence", **kwargs)
        return result

    async def _delete_essence(self, event: AstrMessageEvent, message_id: str, group_id: str = None):
        """取消精华消息。OneBot 标准 API 名为 delete_essence_msg。"""
        kwargs = {"message_id": message_id}
        if group_id is not None:
            kwargs["group_id"] = group_id
        result = await self._execute_action(event, "delete_essence_msg", **kwargs)
        if not result:
            result = await self._execute_action(event, "delete_essence", **kwargs)
        return result

    async def _mute_member(self, event: AstrMessageEvent, group_id: str, qq: str, duration_seconds: int):
        return await self._execute_action(event, "set_group_ban",
                                          group_id=group_id, user_id=qq, duration=duration_seconds)

    async def _unmute_member(self, event: AstrMessageEvent, group_id: str, qq: str):
        return await self._execute_action(event, "set_group_ban",
                                          group_id=group_id, user_id=qq, duration=0)

    async def _kick_member(self, event: AstrMessageEvent, group_id: str, qq: str):
        """踢出群成员。OneBot v11 标准接口为 set_group_kick（#211：
        旧实现只调用的 kick 并非标准 action，协议端返回 unknown action 导致"踢出全部失败"）。"""
        ok, res = await self._call_action_fallback(
            event, ("set_group_kick", "kick"), group_id=group_id, user_id=qq)
        if not ok:
            logger.warning(f"[踢人] 失败 group={group_id} user={qq}: "
                           f"{self._describe_action_failure(res, 'set_group_kick')}")
        return ok

    async def _set_group_avatar(self, event: AstrMessageEvent, group_id: str, file: str):
        """修改群头像，file 可以是 URL 或本地路径或 base64。"""
        return await self._execute_action(event, "set_group_portrait",
                                          group_id=group_id, file=file)

    async def _handle_group_request(self, event: AstrMessageEvent, flag: str, approve: bool, reason: str = ""):
        """同意/拒绝加群申请，返回是否真正成功（#228）。

        OneBot v11 标准接口为 set_group_add_request（需 flag + sub_type + approve），
        旧实现只调用的 handle_group_request 并非标准 action；且调用方从不校验返回值，
        导致协议端拒绝后仍回复"已同意"。这里按顺序回退并返回真实结果。
        """
        params = {"flag": flag, "approve": approve}
        if not approve and reason:
            params["reason"] = reason
        ok, res = await self._call_action_fallback(
            event, ("set_group_add_request", "handle_group_request"),
            sub_type="add", **params)
        if not ok:
            logger.warning(f"[加群审核] 处理失败 flag={flag} approve={approve}: "
                           f"{self._describe_action_failure(res, 'set_group_add_request')}")
        return ok

    # ---------- 消息收发辅助 ----------

    async def _send_private_msg(self, user_id: str, content: str):
        """向指定QQ号发送私聊消息（通过 context）。"""
        if hasattr(self.context, "send_private_msg"):
            try:
                return await self.context.send_private_msg(user_id=user_id, message=content)
            except Exception as e:
                logger.error(f"发送私聊失败: {e}")
        return False

    async def _find_group_owner(self, event: AstrMessageEvent, group_id: str) -> str:
        """查找群主 QQ 号，用于 #140 举报分级路由。返回 QQ 号字符串，找不到返回空串。"""
        member_list = await self._execute_action(event, "get_group_member_list",
                                                 group_id=group_id, return_raw=True)
        if isinstance(member_list, dict):
            data = member_list.get("data") or member_list
            if isinstance(data, list):
                for m in data:
                    if isinstance(m, dict) and m.get("role") == "owner":
                        return str(m.get("user_id", ""))
        return ""

    async def _send_group_text(self, event: AstrMessageEvent, group_id: str, text: str):
        """向指定群发送纯文本消息，返回 message_id（用于后续引用回复关联）。"""
        try:
            if hasattr(self.context, "send_group_msg"):
                # AstrBot 标准方法：send_group_msg(group_id=, message=)
                result = await self.context.send_group_msg(group_id=int(group_id), message=text)
                # 返回值可能直接是 message_id，也可能是含 message_id 的 dict
                if isinstance(result, dict):
                    return str(result.get("message_id") or result.get("data", {}).get("message_id", ""))
                return str(result) if result else ""
            # 回退：使用 _send 但拿不到 message_id
            await self._send(event, [Plain(text)])
        except Exception as e:
            logger.error(f"发送群消息失败: {e}")
        return ""

    async def _get_user_nickname(self, event: AstrMessageEvent, user_id: str) -> str:
        """获取用户昵称（通过 OneBot get_stranger_info API）。"""
        try:
            handler = getattr(self.context, "get_stranger_info", None)
            if callable(handler):
                info = await handler(user_id=int(user_id))
                if isinstance(info, dict):
                    return info.get("nickname") or info.get("data", {}).get("nickname", user_id)
                if hasattr(info, "nickname"):
                    return info.nickname
        except Exception as e:
            logger.error(f"获取昵称失败: {e}")
        return user_id

    async def _notify_admins(self, text: str, group_id: str = ""):
        """向 join_notify_admins 配置的管理员发送私聊通知。"""
        for admin_id in self._store.get_group_setting(group_id, "join_notify_admins", []) or []:
            await self._send_private_msg(str(admin_id), text)
