import re
import json
import time
import asyncio
import os
from collections import deque
from typing import Dict, Optional, List

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.api import logger
from astrbot.api import message_components as Comp
# FunctionTool 使用官方 API 导入
from astrbot.api import FunctionTool

from .utils import (
    parse_at_content, parse_leaked_tool_call, call_onebot,
    has_reply_markers, normalize_message_id
)

from .tools.get_user_info import GetUserInfoTool
from .tools.get_recent_messages import GetRecentMessagesTool
from .tools.delete_message import DeleteMessageTool
from .tools.refresh_messages import RefreshMessagesTool
from .tools.stop_conversation import StopConversationTool
from .tools.poke import PokeTool
from .tools.change_group_card import ChangeGroupCardTool
from .tools.ban_user import BanUserTool
from .tools.group_ban import GroupBanTool
from .tools.group_mute_all import GroupMuteAllTool
from .tools.kick_user import KickUserTool
from .tools.get_group_member_list import GetGroupMemberListTool
from .tools.send_group_notice import SendGroupNoticeTool
from .tools.view_avatar import ViewAvatarTool
from .tools.set_essence_message import SetEssenceMessageTool
from .tools.set_special_title import SetSpecialTitleTool
from .tools.repeat_message import RepeatMessageTool
from .tools.get_message_detail import GetMessageDetailTool

class QQToolsPlugin(Star):
    
    @staticmethod
    def _do_migrate_config(config) -> bool:
        """一次性配置迁移：从旧结构迁移到新结构
        
        检测旧的配置 key 是否存在，如果存在则将值迁移到新结构，然后删除旧 key。
        迁移映射：
          - tools -> basic_tools
          - general.show_message_id/skip_msg_id_prefixes/enable_auto_at_conversion -> context_enhance.*
          - general.delay_append_msg_id -> advanced.compatibility.delay_append_msg_id
          - general.reply_quote -> context_enhance.reply_quote
          - general.file_info -> context_enhance.file_info
          - general.poke_return_info -> advanced.poke_return_info
          - general.message_filter_patterns -> advanced.message_filter_patterns
          - general.message_cache -> advanced.message_cache
          - compatibility -> advanced.compatibility
          
        Returns:
            bool: True 如果执行了迁移操作
        """
        migrated = False
        
        # 1. tools -> basic_tools
        if "tools" in config and "basic_tools" not in config:
            config["basic_tools"] = config.pop("tools")
            migrated = True
            logger.info("[QQTools] 配置迁移: tools -> basic_tools")
        elif "tools" in config:
            del config["tools"]
            migrated = True
        
        # 2. general -> context_enhance + advanced
        old_general = config.get("general")
        if isinstance(old_general, dict):
            # 初始化目标
            if "context_enhance" not in config:
                config["context_enhance"] = {}
            if "advanced" not in config:
                config["advanced"] = {}
            
            ce = config["context_enhance"]
            adv = config["advanced"]
            
            # 迁移到 context_enhance 的字段
            ce_keys = [
                "show_message_id", "skip_msg_id_prefixes",
                "enable_auto_at_conversion"
            ]
            
            # delay_append_msg_id 迁移到 advanced.compatibility
            if "delay_append_msg_id" in old_general:
                if "compatibility" not in adv:
                    adv["compatibility"] = {}
                if "delay_append_msg_id" not in adv["compatibility"]:
                    adv["compatibility"]["delay_append_msg_id"] = old_general["delay_append_msg_id"]
            for k in ce_keys:
                if k in old_general and k not in ce:
                    ce[k] = old_general[k]
            
            # 迁移子对象到 context_enhance
            for sub_key in ("reply_quote", "file_info"):
                if sub_key in old_general and sub_key not in ce:
                    ce[sub_key] = old_general[sub_key]
            
            # 迁移到 advanced 的字段
            adv_keys = ["poke_return_info", "message_filter_patterns"]
            for k in adv_keys:
                if k in old_general and k not in adv:
                    adv[k] = old_general[k]
            
            # 迁移 message_cache 到 advanced
            if "message_cache" in old_general and "message_cache" not in adv:
                adv["message_cache"] = old_general["message_cache"]
            
            # 删除旧 key
            del config["general"]
            migrated = True
            logger.info("[QQTools] 配置迁移: general -> context_enhance + advanced")
        
        # 3. compatibility -> advanced.compatibility
        old_compat = config.get("compatibility")
        if isinstance(old_compat, dict):
            if "advanced" not in config:
                config["advanced"] = {}
            if "compatibility" not in config["advanced"]:
                config["advanced"]["compatibility"] = old_compat
            del config["compatibility"]
            migrated = True
            logger.info("[QQTools] 配置迁移: compatibility -> advanced.compatibility")
        
        return migrated
    
    def _migrate_config_if_needed(self):
        """检查并执行配置迁移，如果发生迁移则保存配置"""
        try:
            if self._do_migrate_config(self.config):
                if hasattr(self.config, 'save_config'):
                    self.config.save_config()
                    logger.info("[QQTools] 配置迁移完成并已保存")
        except Exception as e:
            logger.warning(f"[QQTools] 配置迁移失败（将使用默认值）: {e}")
    
    def __init__(self, context: Context, config: Dict):
        super().__init__(context)
        self.config = config
        
        # 一次性配置迁移：从旧结构迁移到新结构
        self._migrate_config_if_needed()
        
        self.tool_config = self.config.get("basic_tools", {})
        self.context_enhance_config = self.config.get("context_enhance", {})
        self.advanced_config = self.config.get("advanced", {})
        self.reply_adapter_config = self.config.get("reply_adapter", {})
        self.message_detail_config = self.config.get("message_detail_config", {})
        self.view_avatar_config = self.config.get("view_avatar_config", {})
        
        # context_enhance 子分组配置
        self.reply_quote_config = self.context_enhance_config.get("reply_quote", {})
        self.file_info_config = self.context_enhance_config.get("file_info", {})
        
        # advanced 子分组配置
        self.message_cache_config = self.advanced_config.get("message_cache", {})
        self.compatibility_config = self.advanced_config.get("compatibility", {})
        
        # 工具名称前缀配置
        self.add_tool_prefix = self.compatibility_config.get("add_tool_prefix", False)
        self.tool_prefix = "qts_" if self.add_tool_prefix else ""
        
        # delay_append_msg_id 配置将在 _on_message_internal 中处理
        # 不再尝试修改 handler priority（这种方式不稳定且容易找错 handler）
        
        self.cache_size = self.message_cache_config.get("cache_size", 50)
        
        # 消息缓存: {session_id: deque([message_info])}
        # session_id 通常是 group_id 或 user_id
        # 使用普通 dict 而非 defaultdict，以便更好地控制和清理
        self.message_cache: Dict[str, deque] = {}
        
        # 缓存最后活跃时间: {session_id: timestamp}
        # 用于定期清理不活跃的会话缓存，防止内存泄漏
        self.cache_last_active: Dict[str, float] = {}
        
        # 缓存清理配置
        self.cache_inactive_timeout = self.message_cache_config.get("cache_inactive_timeout", 3600)  # 默认 1 小时
        self.cache_cleanup_interval = self.message_cache_config.get("cache_cleanup_interval", 300)  # 默认 5 分钟
        
        # Poke notice 缓存：存储最近的 poke notice 事件，用于 PokeTool 获取戳一戳文案
        # 使用全局缓存而非 session 级别，因为 poke notice 的 session_id 可能与触发工具的 session_id 不同
        self.poke_notice_cache: deque = deque(maxlen=20)  # 只保留最近 20 条
        
        logger.info(f"QQToolsPlugin loaded. Cache size: {self.cache_size}, inactive timeout: {self.cache_inactive_timeout}s.")

        # 注册 FunctionTool
        self._manage_tool("user_info", GetUserInfoTool())
        self._manage_tool("search", GetRecentMessagesTool(self))
        self._manage_tool("delete", DeleteMessageTool(self))
        self._manage_tool("refresh", RefreshMessagesTool(self))
        self._manage_tool("stop", StopConversationTool())
        self._manage_tool("poke", PokeTool(self))
        self._manage_tool("change_card", ChangeGroupCardTool(self))
        self._manage_tool("ban", BanUserTool(self), default=False)
        self._manage_tool("group_ban", GroupBanTool(self), default=False)
        self._manage_tool("group_mute_all", GroupMuteAllTool(self), default=False)
        self._manage_tool("kick_user", KickUserTool(self), default=False)
        self._manage_tool("get_member_list", GetGroupMemberListTool())
        self._manage_tool("send_notice", SendGroupNoticeTool(self), default=False)
        self._manage_tool(
            "view_avatar",
            ViewAvatarTool(self),
            default=self.view_avatar_config.get("view_avatar", True)
        )
        self._manage_tool("set_essence", SetEssenceMessageTool(self))
        self._manage_tool("set_title", SetSpecialTitleTool(self))
        self._manage_tool("repeat", RepeatMessageTool())
        self._manage_tool(
            "message_detail",
            GetMessageDetailTool(self),
            default=self.message_detail_config.get("message_detail", True)
        )

        self.check_ban_task = asyncio.create_task(self.check_ban_expiration())
        self.cache_cleanup_task = asyncio.create_task(self._cleanup_inactive_caches_loop())

    def _manage_tool(self, key: str, tool_instance: FunctionTool, default: bool = True):
        # 获取原始工具名称
        original_name = tool_instance.name
        
        # 计算当前名称和相反前缀的名称（用于清理残余）
        if self.add_tool_prefix:
            current_name = f"{self.tool_prefix}{original_name}"
            legacy_name = original_name  # 无前缀版本是残余
        else:
            current_name = original_name
            legacy_name = f"qts_{original_name}"  # 带前缀版本是残余
        
        # 修改工具实例的名称为当前配置
        tool_instance.name = current_name
        
        if self.tool_config.get(key, default):
            # 注册当前版本的工具
            self.context.add_llm_tools(tool_instance)
            
            # 清理残余工具（相反前缀版本）
            if not self.compatibility_config.get("disable_auto_uninstall", False):
                self.context.unregister_llm_tool(legacy_name)
        elif not self.compatibility_config.get("disable_auto_uninstall", False):
            # 工具被禁用时，卸载当前版本和残余版本
            self.context.unregister_llm_tool(current_name)
            self.context.unregister_llm_tool(legacy_name)

    async def terminate(self):
        if hasattr(self, "check_ban_task"):
            self.check_ban_task.cancel()
        
        if hasattr(self, "cache_cleanup_task"):
            self.cache_cleanup_task.cancel()

    def _get_session_cache(self, session_id: str) -> deque:
        """获取或创建会话缓存，同时更新最后活跃时间
        
        Args:
            session_id: 会话ID
            
        Returns:
            deque: 该会话的消息缓存队列
        """
        current_time = time.time()
        self.cache_last_active[session_id] = current_time
        
        if session_id not in self.message_cache:
            self.message_cache[session_id] = deque(maxlen=self.cache_size)
        
        return self.message_cache[session_id]
    
    async def _cleanup_inactive_caches_loop(self):
        """后台任务：定期清理不活跃的会话缓存
        
        如果 cache_inactive_timeout 为 0 或负数，则禁用自动清理。
        """
        # 如果禁用了自动清理，直接退出
        if self.cache_inactive_timeout <= 0:
            logger.debug("Cache auto-cleanup disabled (cache_inactive_timeout <= 0).")
            return
        
        # 确保清理间隔有效
        cleanup_interval = max(self.cache_cleanup_interval, 60)  # 最小 60 秒
        
        while True:
            try:
                await asyncio.sleep(cleanup_interval)
                await self._cleanup_inactive_caches()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in cache cleanup loop: {e}")
                await asyncio.sleep(cleanup_interval)
    
    async def _cleanup_inactive_caches(self):
        """清理不活跃的会话缓存
        
        遍历所有缓存的会话，删除超过 cache_inactive_timeout 秒没有活动的会话缓存。
        这可以防止长期运行后不活跃会话占用过多内存。
        
        如果 cache_inactive_timeout <= 0，此方法不会执行任何清理。
        """
        if not self.message_cache:
            return
        
        timeout = self.cache_inactive_timeout
        if timeout <= 0:
            return  # 禁用自动清理
        
        current_time = time.time()
        
        # 找出所有不活跃的会话
        inactive_sessions = [
            sid for sid, last_active in self.cache_last_active.items()
            if current_time - last_active > timeout
        ]
        
        if not inactive_sessions:
            return
        
        # 清理不活跃的会话缓存
        for sid in inactive_sessions:
            if sid in self.message_cache:
                del self.message_cache[sid]
            if sid in self.cache_last_active:
                del self.cache_last_active[sid]
        
        if inactive_sessions:
            logger.debug(f"Cleaned up {len(inactive_sessions)} inactive session caches.")
    
    async def check_ban_expiration(self):
        """定期检查黑名单过期"""
        while True:
            try:
                await asyncio.sleep(5)
                ban_list = self.config.get("ban_list", [])
                if not ban_list:
                    continue

                new_list = []
                changed = False
                
                for ban_info in ban_list:
                    user_id = ban_info.get("user_id")
                    duration = ban_info.get("duration", -1)
                    start_time = ban_info.get("ban_time", 0)
                    
                    if duration != -1 and time.time() > start_time + duration:
                        # Expired
                        logger.info(f"Ban expired for user {user_id}.")
                        changed = True
                    else:
                        new_list.append(ban_info)
                
                if changed:
                    self.config["ban_list"] = new_list
                    # 使用 run_in_executor 避免同步 IO 阻塞事件循环
                    loop = asyncio.get_running_loop()
                    await loop.run_in_executor(None, self.config.save_config)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in check_ban_expiration: {e}")
                await asyncio.sleep(5)

    @filter.event_message_type(filter.EventMessageType.ALL, priority=100)
    async def on_all_events(self, event: AstrMessageEvent):
        """监听所有事件，处理 poke notice 缓存和消息缓存"""
        is_notice_event = False
        
        try:
            # 检查是否是 poke notice 事件
            # 使用延迟导入避免硬编码依赖
            try:
                from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
            except ImportError:
                AiocqhttpMessageEvent = None
            
            if AiocqhttpMessageEvent and isinstance(event, AiocqhttpMessageEvent):
                raw_message = getattr(event.message_obj, 'raw_message', None)
                
                # raw_message 可能是 dict 或 Event 对象
                raw_dict = None
                if isinstance(raw_message, dict):
                    raw_dict = raw_message
                elif hasattr(raw_message, '__getitem__'):
                    # Event 对象可能支持 dict-like 访问
                    try:
                        raw_dict = dict(raw_message)
                    except (TypeError, ValueError):
                        pass
                
                if raw_dict:
                    post_type = raw_dict.get('post_type', '')
                    
                    # 检查是否是 notice 事件
                    if post_type == 'notice':
                        is_notice_event = True
                        
                        # 检查是否是 poke notice
                        if (raw_dict.get('notice_type') == 'notify' and
                            raw_dict.get('sub_type') == 'poke'):
                            # 缓存 poke notice 事件
                            poke_info = {
                                'timestamp': time.time(),
                                'user_id': raw_dict.get('user_id'),  # 发起者
                                'target_id': raw_dict.get('target_id'),  # 被戳者
                                'group_id': raw_dict.get('group_id'),  # 群号（私聊时无）
                                'raw_info': raw_dict.get('raw_info'),  # 动作文案信息
                                'raw_message': raw_dict.get('raw_message'),  # 兼容旧字段
                                'raw_event': raw_dict,  # 保留完整事件
                            }
                            self.poke_notice_cache.append(poke_info)
                            logger.debug(f"Cached poke notice: user_id={poke_info['user_id']}, target_id={poke_info['target_id']}, raw_info={poke_info['raw_info']}")
        except Exception as e:
            logger.debug(f"Error processing poke notice: {e}")
        
        # 仅对消息事件执行消息处理逻辑（notice 事件跳过）
        if not is_notice_event:
            await self._on_message_internal(event)

    async def _on_message_internal(self, event: AstrMessageEvent):
        """监听所有消息并缓存，同时处理忙碌会话的消息排队
        
        priority=100 确保此处理器在 LongTermMemory 之前执行，
        这样 LTM 记录的群聊历史中也会包含 [MSG_ID:xxx] 和文件信息。
        """
        try:
            # 引用消息增强：
            # AstrBot 的 OneBot(V11) 适配器默认只把「文本段」拼进 message_str，
            # 因此被引用消息如果只有图片/文件/卡片等非文本内容，Reply.message_str 会是空，
            # 进而在日志与上下文里只能看到 [引用消息]。
            if self.reply_quote_config.get("enhance_reply_quote", True):
                self._enhance_reply_quote(event)

            # Check Ban
            sender_id = event.get_sender_id()
            ban_list = self.config.get("ban_list", [])
            for ban_info in ban_list:
                if ban_info.get("user_id") == sender_id:
                    # Check expiration (double check)
                    duration = ban_info.get("duration", -1)
                    start_time = ban_info.get("ban_time", 0)
                    if duration != -1 and time.time() > start_time + duration:
                        continue # Let background task handle removal
                    
                    event.stop_event()
                    return

            # 获取 session_id (群号或私聊用户ID)
            session_id = event.get_session_id()
            if not session_id:
                return

            # 1. 获取消息ID
            message_id = event.message_obj.message_id
            
            # 2. 提取文件信息并添加到消息中
            file_info_parts = []
            if self.file_info_config.get("show_file_info", False):
                file_info_parts = self._extract_file_info(event)
            
            # 3. 如果有文件信息，追加到 message_str 和 message chain
            if file_info_parts:
                file_info_str = " " + " ".join(file_info_parts)
                
                # 防止重复添加（检查第一个文件标记是否已存在）
                if file_info_parts[0] not in event.message_str:
                    event.message_str += file_info_str
                    
                    # 同步 event.message_obj.message_str
                    if hasattr(event.message_obj, "message_str") and isinstance(event.message_obj.message_str, str):
                        event.message_obj.message_str += file_info_str
                    
                    # 追加到 message chain
                    if hasattr(event.message_obj, "message") and isinstance(event.message_obj.message, list):
                        event.message_obj.message.append(Comp.Plain(file_info_str))
            
            # 5. 处理 MSG_ID 追加逻辑
            # - show_message_id: 控制是否在消息中显示 MSG_ID
            # - delay_append_msg_id: 如果启用，不将 MSG_ID 注入到 event（避免污染 LTM），
            #                        但仍在缓存中保留 MSG_ID 供工具使用
            # - skip_msg_id_for_commands: 如果列表非空，检测到指令消息时跳过 MSG_ID 注入
            show_msg_id = self.context_enhance_config.get("show_message_id", True)
            delay_msg_id = self.compatibility_config.get("delay_append_msg_id", False)
            
            # 检测是否是指令消息（如果配置了指令前缀列表）
            command_prefixes = self.context_enhance_config.get("skip_msg_id_prefixes", [])
            # 确保是列表类型
            if not isinstance(command_prefixes, list):
                command_prefixes = []
            skip_for_command = False
            if command_prefixes:
                # 调试日志：显示实际检查的消息内容
                logger.debug(f"[QQTools] Checking message for command prefixes: '{event.message_str[:80]}...' (prefixes: {command_prefixes})")
                skip_for_command = self._is_likely_command(event.message_str, command_prefixes)
                if skip_for_command:
                    logger.info(f"[QQTools] Skipping MSG_ID injection for command message (matched one of {command_prefixes})")
                else:
                    logger.debug(f"[QQTools] Message does not match any command prefix")
            
            # 如果检测到是指令消息，强制使用 delay 模式（不注入到 event，但缓存中仍有）
            effective_delay = delay_msg_id or skip_for_command
            
            id_suffix = f" [MSG_ID:{message_id}]" if show_msg_id else ""
            
            # 只有在 show_message_id 启用且 delay_append_msg_id 未启用且非指令消息时，才注入到 event
            if show_msg_id and not effective_delay:
                # 防止重复添加
                if id_suffix not in event.message_str:
                    event.message_str += id_suffix
                
                # 同步 event.message_obj.message_str（某些地方会读取这个属性）
                if hasattr(event.message_obj, "message_str") and isinstance(event.message_obj.message_str, str):
                    if id_suffix not in event.message_obj.message_str:
                        event.message_obj.message_str += id_suffix
                
                # 追加 ID 到 message chain (Plain Text)，确保多模态下也能看到
                # 注意：某些 Adapter 可能没有 .message 属性或者结构不同，需防御性编程
                if hasattr(event.message_obj, "message") and isinstance(event.message_obj.message, list):
                    event.message_obj.message.append(Comp.Plain(id_suffix))

            # 提取消息基本信息
            # 注意：缓存中的 content 始终包含 MSG_ID（如果 show_message_id 启用），
            # 即使 delay_append_msg_id 或 skip_msg_id_for_commands 启用（不注入到 event），
            # 工具仍能从缓存获取 MSG_ID
            cache_content = event.message_str
            if show_msg_id and effective_delay:
                # delay 模式：event 中没有 MSG_ID，但缓存中要有
                if id_suffix not in cache_content:
                    cache_content += id_suffix
            
            # 优先使用消息真实时间戳（从 raw_message 中获取）
            # OneBot 事件中的 time 字段是消息的真实发送时间
            # 如果获取失败，回退到 message_obj.timestamp，最后使用当前时间
            real_timestamp = self._get_real_message_timestamp(event)
            
            msg_info = {
                "message_id": message_id,
                "sender_id": event.get_sender_id(),
                "sender_name": event.get_sender_name(),
                "content": cache_content,  # 缓存内容始终包含 MSG_ID（供工具使用）
                "timestamp": real_timestamp,
                "raw_message": event.message_obj.raw_message  # 保存原始消息对象以备不时之需
            }
            
            # 存入缓存（使用 _get_session_cache 确保更新活跃时间）
            self._get_session_cache(session_id).append(msg_info)
            
        except Exception as e:
            logger.error(f"Error processing message in QQToolsPlugin: {e}")

    def _is_likely_command(self, message_str: str, command_prefixes: list) -> bool:
        """检测消息是否可能是指令
        
        通过检查消息是否以给定的指令前缀开头来判断。
        
        Args:
            message_str: 消息字符串
            command_prefixes: 指令前缀列表
            
        Returns:
            bool: 如果消息看起来像指令则返回 True
        """
        if not message_str or not command_prefixes:
            return False
        
        stripped = message_str.strip()
        if not stripped:
            return False
        
        return any(stripped.startswith(p) for p in command_prefixes)

    def _get_real_message_timestamp(self, event: AstrMessageEvent) -> int:
        """获取消息的真实时间戳
        
        优先级：
        1. raw_message 中的 time 字段（OneBot 事件的真实消息时间）
        2. message_obj.timestamp（AstrBot 设置的时间戳，通常也是收到时间）
        3. 当前时间（兜底）
        
        Args:
            event: 消息事件
            
        Returns:
            int: Unix 时间戳（秒）
        """
        try:
            # 尝试从 raw_message 获取真实时间戳
            raw_message = getattr(event.message_obj, 'raw_message', None)
            if raw_message is not None:
                # raw_message 可能是 Event 对象或 dict
                raw_time = None
                
                # 尝试作为 dict 访问
                if isinstance(raw_message, dict):
                    raw_time = raw_message.get('time')
                elif hasattr(raw_message, 'time'):
                    # Event 对象通常有 time 属性
                    raw_time = raw_message.time
                elif hasattr(raw_message, '__getitem__'):
                    # 支持 dict-like 访问
                    try:
                        raw_time = raw_message['time']
                    except (KeyError, TypeError):
                        pass
                
                if raw_time is not None:
                    # 确保是整数
                    ts = int(raw_time)
                    # 基本合理性检查：时间戳应该是正数且不超过未来 1 年
                    if 0 < ts < int(time.time()) + 31536000:
                        return ts
            
            # 尝试使用 message_obj.timestamp
            msg_timestamp = getattr(event.message_obj, 'timestamp', None)
            if msg_timestamp is not None:
                ts = int(msg_timestamp)
                if 0 < ts < int(time.time()) + 31536000:
                    return ts
        except Exception as e:
            logger.debug(f"Error getting real message timestamp: {e}")
        
        # 兜底：使用当前时间
        return int(time.time())

    def _extract_file_info(self, event: AstrMessageEvent) -> list:
        """从消息中提取文件信息，返回格式化的文件信息列表
        
        格式: [File:name=xxx,type=video,id=xxx,size=xxx]
        """
        file_info_parts = []
        
        # 获取消息组件列表
        messages = event.get_messages() if hasattr(event, 'get_messages') else []
        if not messages and hasattr(event.message_obj, 'message'):
            messages = event.message_obj.message
        
        # 获取原始消息数据以提取更多信息
        raw_message = getattr(event.message_obj, 'raw_message', None)
        raw_segments = []
        if raw_message and hasattr(raw_message, 'message') and isinstance(raw_message.message, list):
            raw_segments = raw_message.message
        
        # 创建一个从组件类型到原始数据的映射
        raw_data_by_type = {}
        for seg in raw_segments:
            if isinstance(seg, dict) and 'type' in seg:
                seg_type = seg['type']
                if seg_type not in raw_data_by_type:
                    raw_data_by_type[seg_type] = []
                raw_data_by_type[seg_type].append(seg.get('data', {}))
        
        # 用于跟踪每种类型处理的索引
        type_indices = {}
        
        for comp in messages:
            info_parts = []
            comp_type = None
            
            if isinstance(comp, Comp.File):
                comp_type = 'file'
                # 文件类型
                info_parts.append(f"name={comp.name or 'unknown'}")
                info_parts.append("type=file")
                
                # 尝试从原始数据获取更多信息
                idx = type_indices.get('file', 0)
                type_indices['file'] = idx + 1
                if 'file' in raw_data_by_type and idx < len(raw_data_by_type['file']):
                    raw_data = raw_data_by_type['file'][idx]
                    if 'file_id' in raw_data:
                        info_parts.append(f"id={raw_data['file_id']}")
                    if 'file_size' in raw_data:
                        info_parts.append(f"size={raw_data['file_size']}")
                
            elif isinstance(comp, Comp.Video):
                comp_type = 'video'
                # 视频类型
                file_name = getattr(comp, 'path', '') or getattr(comp, 'file', '') or 'video'
                if '/' in file_name:
                    file_name = file_name.split('/')[-1]
                if '\\' in file_name:
                    file_name = file_name.split('\\')[-1]
                info_parts.append(f"name={file_name}")
                info_parts.append("type=video")
                
                # 尝试从原始数据获取更多信息
                idx = type_indices.get('video', 0)
                type_indices['video'] = idx + 1
                if 'video' in raw_data_by_type and idx < len(raw_data_by_type['video']):
                    raw_data = raw_data_by_type['video'][idx]
                    if 'file_id' in raw_data:
                        info_parts.append(f"id={raw_data['file_id']}")
                    elif 'file' in raw_data:
                        # 有时候 file 字段包含 ID
                        file_val = raw_data['file']
                        if file_val and not file_val.startswith('http'):
                            info_parts.append(f"id={file_val[:16]}")
                    if 'file_size' in raw_data:
                        info_parts.append(f"size={raw_data['file_size']}")
                
            elif isinstance(comp, Comp.Record):
                comp_type = 'record'
                # 音频类型
                file_name = getattr(comp, 'path', '') or getattr(comp, 'file', '') or 'audio'
                if '/' in file_name:
                    file_name = file_name.split('/')[-1]
                if '\\' in file_name:
                    file_name = file_name.split('\\')[-1]
                info_parts.append(f"name={file_name}")
                info_parts.append("type=audio")
                
                # 尝试从原始数据获取更多信息
                idx = type_indices.get('record', 0)
                type_indices['record'] = idx + 1
                if 'record' in raw_data_by_type and idx < len(raw_data_by_type['record']):
                    raw_data = raw_data_by_type['record'][idx]
                    if 'file_id' in raw_data:
                        info_parts.append(f"id={raw_data['file_id']}")
                    elif 'file' in raw_data:
                        file_val = raw_data['file']
                        if file_val and not file_val.startswith('http'):
                            info_parts.append(f"id={file_val[:16]}")
                    if 'file_size' in raw_data:
                        info_parts.append(f"size={raw_data['file_size']}")
                
            elif isinstance(comp, Comp.Image):
                comp_type = 'image'
                # 图片类型 - 可选，因为图片通常已经有 [Image] 标记
                if self.file_info_config.get("show_image_as_file", False):
                    file_name = getattr(comp, 'file_unique', '') or 'image'
                    info_parts.append(f"name={file_name}")
                    info_parts.append("type=image")
                    
                    idx = type_indices.get('image', 0)
                    type_indices['image'] = idx + 1
                    if 'image' in raw_data_by_type and idx < len(raw_data_by_type['image']):
                        raw_data = raw_data_by_type['image'][idx]
                        if 'file_id' in raw_data:
                            info_parts.append(f"id={raw_data['file_id']}")
                        elif 'file' in raw_data:
                            file_val = raw_data['file']
                            if file_val and not file_val.startswith('http') and not file_val.startswith('base64'):
                                info_parts.append(f"id={file_val[:16]}")
                        if 'file_size' in raw_data:
                            info_parts.append(f"size={raw_data['file_size']}")
            
            # 如果有信息要添加
            if info_parts:
                file_info_parts.append(f"[File:{','.join(info_parts)}]")
        
        return file_info_parts

    # -----------------------------
    # Reply / Quote enhancement
    # -----------------------------
    def _outline_component_for_quote(self, comp: object) -> str:
        """将消息段转换为适合放进引用消息概要的短文本。"""
        try:
            if isinstance(comp, Comp.Plain):
                text = (comp.text or "").strip()
                return re.sub(r"\s+", " ", text)
            if isinstance(comp, Comp.Image):
                return "图片"
            if hasattr(Comp, "Video") and isinstance(comp, Comp.Video):
                return "视频"
            if hasattr(Comp, "Record") and isinstance(comp, Comp.Record):
                return "语音"
            if hasattr(Comp, "File") and isinstance(comp, Comp.File):
                name = getattr(comp, "name", "") or ""
                name = str(name).strip()
                return f"文件:{name}" if name else "文件"
            if hasattr(Comp, "Json") and isinstance(comp, Comp.Json):
                return "卡片"
            if hasattr(Comp, "Forward") and isinstance(comp, Comp.Forward):
                return "转发消息"
            if hasattr(Comp, "Nodes") and isinstance(comp, Comp.Nodes):
                return "转发消息"
            if hasattr(Comp, "Node") and isinstance(comp, Comp.Node):
                return "转发消息"
            if hasattr(Comp, "Face") and isinstance(comp, Comp.Face):
                fid = getattr(comp, "id", "")
                return f"表情:{fid}" if fid else "表情"
            if hasattr(Comp, "At") and isinstance(comp, Comp.At):
                # 不要太长，优先显示 name
                name = getattr(comp, "name", "") or ""
                qq = getattr(comp, "qq", "") or ""
                name = str(name).strip()
                qq = str(qq).strip()
                if name and qq and qq != "all":
                    return f"@{name}({qq})"
                if name:
                    return f"@{name}"
                if qq:
                    return "@全体成员" if qq == "all" else f"@{qq}"
                return "@"
        except Exception:
            pass

        # 兜底
        t = getattr(comp, "type", None)
        if isinstance(t, str) and t:
            return t
        return comp.__class__.__name__

    def _build_quote_summary_from_chain(self, chain: Optional[list]) -> str:
        """从被引用消息链生成概要文本（兼容图片/文件/卡片等非文本）。"""
        if not chain:
            return ""

        parts: list[str] = []
        for seg in chain:
            s = self._outline_component_for_quote(seg)
            if not s:
                continue
            parts.append(s)

        # 去掉重复的空格并拼接
        summary = " ".join(p for p in parts if p)
        summary = re.sub(r"\s+", " ", summary).strip()

        max_len = int(self.reply_quote_config.get("reply_quote_max_len", 80))
        if max_len > 0 and len(summary) > max_len:
            summary = summary[: max_len - 1].rstrip() + "…"
        return summary

    def _enhance_reply_quote(self, event: AstrMessageEvent):
        """增强 Reply 段的 message_str，使日志/上下文能显示图片/文件等被引用内容。"""
        # 仅对 aiocqhttp 平台生效（其他平台的 Reply 结构可能不同）
        # 使用延迟导入避免硬编码依赖
        try:
            from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
        except ImportError:
            return  # 如果无法导入，跳过此功能
        
        if not isinstance(event, AiocqhttpMessageEvent):
            return

        msgs = []
        if hasattr(event.message_obj, "message") and isinstance(event.message_obj.message, list):
            msgs = event.message_obj.message
        elif hasattr(event, "get_messages"):
            msgs = event.get_messages()

        if not msgs:
            return

        include_msg_id = bool(self.reply_quote_config.get("reply_quote_include_msg_id", True))
        inject_into_message_str = bool(self.reply_quote_config.get("inject_reply_quote_into_message_str", True))
        enrich_even_if_text = bool(self.reply_quote_config.get("reply_quote_enrich_even_if_text", True))

        quote_prefixes: list[str] = []

        for seg in msgs:
            if not isinstance(seg, Comp.Reply):
                continue

            chain = getattr(seg, "chain", None)
            summary = self._build_quote_summary_from_chain(chain)

            # 如果 chain 无法构造摘要，回退到现有 message_str
            existing = (getattr(seg, "message_str", "") or "").strip()
            if not summary:
                summary = existing

            # chain 能构造摘要时，可选择覆盖/增强已有文本（比如“文字+图片”）
            if summary and (enrich_even_if_text or not existing):
                seg.message_str = summary
                # 兼容字段
                if hasattr(seg, "text"):
                    seg.text = summary

            # 需要在摘要里补充被引用消息的 msg_id
            if include_msg_id:
                mid = str(getattr(seg, "id", "") or "").strip()
                if mid and mid not in summary:
                    # 放在末尾更接近“内容摘要 + id”的阅读习惯
                    summary_with_id = f"{summary} MSG_ID:{mid}" if summary else f"MSG_ID:{mid}"
                    seg.message_str = summary_with_id
                    if hasattr(seg, "text"):
                        seg.text = summary_with_id
                    summary = summary_with_id

            # 生成可注入 message_str 的前缀（让 LLM 也能看到引用内容）
            if inject_into_message_str:
                nickname = (getattr(seg, "sender_nickname", "") or "").strip() or "N/A"
                if summary:
                    quote_prefixes.append(f"[引用消息({nickname}: {summary})]")
                else:
                    quote_prefixes.append("[引用消息]")

        if inject_into_message_str and quote_prefixes:
            prefix = " ".join(quote_prefixes).strip() + " "

            # 防止重复注入
            if not (event.message_str or "").startswith(prefix.strip()):
                event.message_str = prefix + (event.message_str or "")

            # 同步 message_obj.message_str
            if hasattr(event.message_obj, "message_str") and isinstance(event.message_obj.message_str, str):
                if not event.message_obj.message_str.startswith(prefix.strip()):
                    event.message_obj.message_str = prefix + event.message_obj.message_str

    @filter.on_decorating_result()
    async def on_decorating_result(self, event: AstrMessageEvent):
        """
        在消息发送前进行处理：
        1. 引用回复转换：检测 [REPLY:...] 标记并转换为 Reply 组件
        2. [At:123456] 转换为真实的 At 消息组件
        3. 消息内容过滤
        
        重要：此方法只负责转换消息组件，不接管发送流程。
        让 AstrBot 的 RespondStage 正常处理分段发送等逻辑。
        """
        # 仅针对 QQ 平台 (Aiocqhttp)
        # 使用延迟导入避免硬编码依赖
        try:
            from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
        except ImportError:
            return  # 如果无法导入，跳过此功能
        
        if not isinstance(event, AiocqhttpMessageEvent):
            return

        result = event.get_result()
        if not result or not result.chain:
            return

        # =============================================
        # 引用回复转换逻辑（只转换，不接管发送）
        # =============================================
        enable_reply_adapter = self.reply_adapter_config.get("enable", False)
        enable_at_conversion = self.context_enhance_config.get("enable_auto_at_conversion", False)
        msg_filter_patterns = self.advanced_config.get("message_filter_patterns", [])

        # 如果没有任何功能启用，直接返回
        if not enable_reply_adapter and not enable_at_conversion and not msg_filter_patterns:
            return

        new_chain = []
        
        for component in result.chain:
            if isinstance(component, Comp.Plain) and component.text:
                current_text = component.text

                # 0. 消息内容过滤 (Regex)
                if msg_filter_patterns:
                    for pattern in msg_filter_patterns:
                        try:
                            current_text = re.sub(pattern, "", current_text)
                        except Exception:
                            pass
                
                if not current_text:
                    continue
                
                # 1. 引用回复标签转换
                # 格式：[REPLY:message_id]内容
                # 同一消息内换行使用 \n（字面量）
                if enable_reply_adapter and has_reply_markers(current_text):
                    # 先将字面量 \n（LLM 可能输出的转义序列）转换为真实换行，
                    # 但只转换位于 [REPLY:...] 标签前的字面量 \n，
                    # 避免误转换消息内容中的 \n。
                    # 使用正则：匹配 字面量\n 后紧跟 [REPLY: 的模式
                    current_text = re.sub(r'\\n(?=\[REPLY:)', '\n', current_text)
                    
                    # 按行处理，每行可能是一个 [REPLY:...] 标签
                    lines = current_text.split('\n')
                    for line in lines:
                        line = line.strip()
                        if not line:
                            continue
                        
                        # 匹配 [REPLY:message_id]内容 格式
                        reply_match = re.match(r'\[REPLY:([^\]]+)\](.*)', line)
                        if reply_match:
                            msg_id = reply_match.group(1).strip()
                            content = reply_match.group(2)
                            
                            # 规范化 message_id
                            msg_id = normalize_message_id(msg_id)
                            
                            # 将 \n（字面量两个字符）转换为真实换行
                            content = content.replace('\\n', '\n')
                            
                            # 添加 Reply 组件
                            new_chain.append(Comp.Reply(id=msg_id))
                            
                            # 添加内容（支持 At 转换）
                            if content.strip():
                                if enable_at_conversion:
                                    new_chain.extend(parse_at_content(content))
                                else:
                                    new_chain.append(Comp.Plain(content))
                        else:
                            # 普通行（不带引用标签）
                            if enable_at_conversion:
                                new_chain.extend(parse_at_content(line))
                            else:
                                new_chain.append(Comp.Plain(line))
                else:
                    # 2. 尝试解析泄露的工具调用
                    is_leaked_tool = False
                    if self.compatibility_config.get("fix_tool_leak", True):
                        filter_patterns = self.compatibility_config.get("filter_patterns", ["&&.*?&&"])
                        content, message_id = parse_leaked_tool_call(current_text, filter_patterns=filter_patterns)
                        
                        if content is not None and message_id is not None:
                            # 解析成功，构造 Reply 和 Content
                            logger.warning(f"Detected leaked tool call in text. Fixing... ID: {message_id}, Content: {content}")
                            new_chain.append(Comp.Reply(id=message_id))
                            # 对提取出的内容再进行 At 解析
                            if enable_at_conversion:
                                new_chain.extend(parse_at_content(content))
                            else:
                                new_chain.append(Comp.Plain(content))
                            is_leaked_tool = True

                    if not is_leaked_tool:
                        # 3. 常规解析 At
                        if enable_at_conversion:
                            new_chain.extend(parse_at_content(current_text))
                        else:
                            new_chain.append(Comp.Plain(current_text))
            else:
                new_chain.append(component)
        
        # ========== 多 Reply 拆分发送 ==========
        # OneBot 协议中，一条消息只能包含一个 reply 段。
        # 当 LLM 输出包含多个 [REPLY:message_id] 时，new_chain 中会有多个 Reply 组件，
        # 如果作为一条消息发送，协议端只会识别第一个 Reply，后续的会丢失或格式错乱。
        # 因此需要按 Reply 组件拆分成多组，前 N-1 组直接发送，最后一组留给 AstrBot 正常流程。
        reply_count = sum(1 for c in new_chain if isinstance(c, Comp.Reply))

        if reply_count > 1:
            # 将 chain 按 Reply 组件拆分成多组
            groups = []
            current_group = []

            for comp in new_chain:
                if isinstance(comp, Comp.Reply) and current_group:
                    # 遇到新的 Reply，将之前的组保存
                    groups.append(current_group)
                    current_group = [comp]
                else:
                    current_group.append(comp)

            if current_group:
                groups.append(current_group)

            # 前 N-1 组直接发送
            from astrbot.core.message.message_event_result import MessageChain
            for group in groups[:-1]:
                try:
                    await event.send(MessageChain(chain=group))
                except Exception as e:
                    logger.error(f"Failed to send split reply message: {e}")

            # 最后一组留给 AstrBot 正常流程发送
            result.chain = groups[-1]
        else:
            result.chain = new_chain

        if not result.chain:
            event.stop_event()
            logger.debug("Message chain is empty after filtering. Event stopped.")

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req):
        """在 LLM 请求时注入引用回复引导提示词
        
        仅当以下条件同时满足时才注入：
        1. 引用回复适配器已启用 (enable=true)
        2. 提示词配置非空 (prompt 不为空字符串)
        
        Args:
            event: 消息事件
            req: ProviderRequest 对象
        """
        try:
            # 检查是否启用引用回复适配器
            enable_adapter = self.reply_adapter_config.get("enable", False)
            if not enable_adapter:
                return
            
            # 获取提示词配置，为空则不注入
            prompt = self.reply_adapter_config.get("prompt", "")
            if not prompt or not prompt.strip():
                return
            
            prompt = prompt.strip()
            
            # 注入到 system_prompt
            if hasattr(req, 'system_prompt'):
                current_prompt = getattr(req, 'system_prompt', '') or ''
                if current_prompt.strip():
                    # 在现有 system_prompt 后追加
                    req.system_prompt = f"{current_prompt}\n\n{prompt}"
                else:
                    # system_prompt 为空，直接设置
                    req.system_prompt = prompt
            else:
                logger.warning("ProviderRequest has no 'system_prompt' attribute, cannot inject prompt.")
            
        except Exception as e:
            logger.error(f"Error injecting reply adapter prompt: {e}")
            import traceback
            traceback.print_exc()

    @filter.after_message_sent()
    async def on_after_message_sent(self, event: AstrMessageEvent):
        """在消息发送后，获取并缓存 BOT 发送的消息
        
        这样可以确保 get_recent_messages 工具能够查询到 BOT 自己发送的消息 ID
        """
        try:
            # 检查是否启用了缓存 BOT 消息功能
            if not self.message_cache_config.get("cache_bot_messages", True):
                return
            
            # 仅针对 QQ 平台 (Aiocqhttp)
            # 使用延迟导入避免硬编码依赖
            try:
                from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
            except ImportError:
                return  # 如果无法导入，跳过此功能
            
            if not isinstance(event, AiocqhttpMessageEvent):
                return
                
            session_id = event.get_session_id()
            if not session_id:
                return
                
            # 调用 API 获取最新消息并缓存 BOT 发送的消息
            await self._cache_bot_sent_messages(event)
            
        except Exception as e:
            logger.error(f"Error in on_after_message_sent: {e}")

    async def _cache_bot_sent_messages(self, event: AstrMessageEvent):
        """获取最近的历史消息，缓存 BOT 发送的消息
        
        注意：此方法内部会延迟导入 AiocqhttpMessageEvent 进行类型检查，
        因此参数类型注解使用通用的 AstrMessageEvent 以避免导入错误。
        """
        client = event.bot
        session_id = event.get_session_id()
        self_id = str(event.get_self_id())
        
        try:
            api_history_count = self.message_cache_config.get("api_history_count", 10)
            
            if event.get_group_id():
                # 群聊：获取群历史消息
                group_id = int(event.get_group_id())
                resp = await call_onebot(
                    client,
                    'get_group_msg_history',
                    group_id=group_id,
                    count=api_history_count
                )
            else:
                # 私聊：获取好友历史消息
                user_id = int(event.get_sender_id())
                resp = await call_onebot(
                    client,
                    'get_friend_msg_history',
                    user_id=user_id,
                    count=api_history_count
                )
            
            if not resp or 'messages' not in resp:
                return
                
            messages = resp.get('messages', [])
            
            for msg in messages:
                sender_id = str(msg.get('sender', {}).get('user_id', ''))
                
                # 只缓存 BOT 自己发送的消息
                if sender_id != self_id:
                    continue
                    
                message_id = str(msg.get('message_id', ''))
                
                # 检查是否已在缓存中
                if self._is_message_cached(session_id, message_id):
                    continue
                    
                # 构建消息信息
                msg_info = self._build_msg_info_from_api(msg, self_id)
                
                # 存入缓存（使用 _get_session_cache 确保更新活跃时间）
                self._get_session_cache(session_id).append(msg_info)
                logger.debug(f"Cached BOT message: {message_id}")
                
        except Exception as e:
            logger.warning(f"Failed to cache BOT messages: {e}")

    def _is_message_cached(self, session_id: str, message_id: str) -> bool:
        """检查消息是否已在缓存中
        
        注意：此方法只检查不修改，不会更新活跃时间
        """
        if session_id not in self.message_cache:
            return False
        for msg in self.message_cache.get(session_id, []):
            if str(msg.get('message_id', '')) == str(message_id):
                return True
        return False

    def _build_msg_info_from_api(self, msg: dict, self_id: str) -> dict:
        """从 API 响应构建消息信息"""
        sender = msg.get('sender', {})
        sender_id = str(sender.get('user_id', ''))
        sender_name = sender.get('card', '') or sender.get('nickname', '') or 'Unknown'
        
        # 如果是 BOT 自己的消息，标记发送者名称
        if sender_id == self_id:
            sender_name = f"[BOT]{sender_name}"
        
        # 提取消息内容
        content = self._extract_message_content(msg.get('message', []))
        message_id = str(msg.get('message_id', ''))
        
        # 添加 MSG_ID 标记
        if self.context_enhance_config.get("show_message_id", True):
            content += f" [MSG_ID:{message_id}]"
        
        return {
            "message_id": message_id,
            "sender_id": sender_id,
            "sender_name": sender_name,
            "content": content,
            "timestamp": msg.get('time', int(time.time())),
            "raw_message": msg,
            "is_bot_message": sender_id == self_id  # 标记是否为 BOT 消息
        }

    def _extract_message_content(self, message_segments: list) -> str:
        """从消息段列表提取文本内容"""
        parts = []
        for seg in message_segments:
            if isinstance(seg, dict):
                seg_type = seg.get('type', '')
                data = seg.get('data', {})
            else:
                # 可能是其他类型的消息段对象
                continue
            
            if seg_type == 'text':
                parts.append(data.get('text', ''))
            elif seg_type == 'image':
                parts.append('[图片]')
            elif seg_type == 'at':
                qq = data.get('qq', '')
                parts.append(f'@{qq}')
            elif seg_type == 'face':
                parts.append('[表情]')
            elif seg_type == 'record':
                parts.append('[语音]')
            elif seg_type == 'video':
                parts.append('[视频]')
            elif seg_type == 'file':
                parts.append(f"[文件:{data.get('name', 'file')}]")
            elif seg_type == 'reply':
                parts.append(f"[回复:{data.get('id', '')}]")
            else:
                parts.append(f'[{seg_type}]')
        
        return ''.join(parts)

    async def fetch_history_from_api(self, event: AstrMessageEvent, count: int = 50) -> list:
        """从 Napcat API 获取历史消息（供工具调用使用）
        
        Args:
            event: 消息事件
            count: 获取的消息数量
            
        Returns:
            消息信息列表
        """
        # 使用延迟导入避免硬编码依赖
        try:
            from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
        except ImportError:
            return []  # 如果无法导入，返回空列表
        
        if not isinstance(event, AiocqhttpMessageEvent):
            return []
        
        client = event.bot
        self_id = str(event.get_self_id())
        
        try:
            if event.get_group_id():
                group_id = int(event.get_group_id())
                resp = await call_onebot(
                    client,
                    'get_group_msg_history',
                    group_id=group_id,
                    count=count
                )
            else:
                user_id = int(event.get_sender_id())
                resp = await call_onebot(
                    client,
                    'get_friend_msg_history',
                    user_id=user_id,
                    count=count
                )
            
            if not resp or 'messages' not in resp:
                return []
            
            messages = []
            for msg in resp.get('messages', []):
                msg_info = self._build_msg_info_from_api(msg, self_id)
                messages.append(msg_info)
            
            return messages
            
        except Exception as e:
            logger.warning(f"Failed to fetch history from API: {e}")
            return []
