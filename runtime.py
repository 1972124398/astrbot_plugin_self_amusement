"""Resolve ordinary AstrBot personas, histories, mood hints and delivery."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass

from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.platform import AstrBotMessage, MessageMember, MessageType
from astrbot.api.provider import ProviderRequest
from astrbot.core.platform.astrbot_message import Group
from astrbot.core.platform.message_session import MessageSession
from .interop import CompanionLink


class LeisureEvent(AstrMessageEvent):
    def __init__(self, session, platform, sender_id="", label=""):
        message = AstrBotMessage()
        message.type = session.message_type
        message.session_id = session.session_id
        message.message_id = "proactive:self_amusement:" + uuid.uuid4().hex
        message.message = []
        message.message_str = ""
        message.self_id = getattr(platform, "bot_self_id", "")
        message.sender = MessageMember(user_id=sender_id, nickname=label or None)
        if session.message_type == MessageType.GROUP_MESSAGE:
            message.group = Group(group_id=session.session_id)
        super().__init__("", message, platform.meta(), session.session_id)
        self.is_wake = True
        self.is_at_or_wake_command = True
        self.plugins_name = None
        self.set_extra("self_amusement", True)


@dataclass
class ConversationView:
    origin: str
    event: LeisureEvent
    persona: str
    persona_id: str | None
    history: list[dict]
    conversation: object | None
    provider_id: str


def compact_history(messages: list, limit=8) -> list[dict]:
    result = []
    for message in messages[-limit:]:
        if not isinstance(message, dict) or message.get("role") not in {"user", "assistant"}:
            continue
        content = message.get("content", "")
        if isinstance(content, list):
            content = "\n".join(str(part.get("text", ""))[-1200:] for part in content[-16:]
                    if isinstance(part, dict) and part.get("type") == "text" and not part.get("_no_save"))
        if isinstance(content, str):
            content = content[-1200:]
            if content.strip():
                result.append({"role": message["role"], "content": content})
    return result


class Runtime:
    def __init__(self, context, config=None):
        self.context = context
        self.link = CompanionLink(context, "self_amusement", config)

    def reflection_persona(self, snapshot):
        persona_id = snapshot.get("persona_id")
        if persona_id == "[%None]":
            return ""
        if persona_id and persona_id != "_chatui_default_":
            persona = self.context.persona_manager.get_persona_v3_by_id(persona_id)
            if persona and persona.get("prompt", "") in snapshot.get("system_prompt", ""):
                return str(persona.get("prompt", ""))
        if persona_id == "_chatui_default_":
            from astrbot.core.astr_main_agent_resources import CHATUI_SPECIAL_DEFAULT_PERSONA_PROMPT

            return CHATUI_SPECIAL_DEFAULT_PERSONA_PROMPT
        return snapshot.get("system_prompt", "")

    def platform(self, origin):
        session = MessageSession.from_str(origin)
        if session.message_type not in {MessageType.FRIEND_MESSAGE, MessageType.GROUP_MESSAGE}:
            raise ValueError("Choose a private or group conversation")
        platform = self.context.get_platform_inst(session.platform_id)
        if platform is None or not getattr(platform.meta(), "support_proactive_message", True):
            raise ValueError("Platform does not support proactive messages")
        return session, platform

    async def enabled(self, origin):
        from astrbot.core.star.session_llm_manager import SessionServiceManager

        return (await SessionServiceManager.is_session_enabled(origin)
                and await SessionServiceManager.is_llm_enabled_for_session(origin))

    async def view(self, origin, target, configured_provider=""):
        session, platform = self.platform(origin)
        event = LeisureEvent(session, platform, target.sender_id, target.label)
        manager = self.context.conversation_manager
        cid = await manager.get_curr_conversation_id(origin)
        conversation = await manager.get_conversation(origin, cid) if cid else None
        config = self.context.get_config(umo=origin)
        persona_id, persona, _, special_default = await self.context.persona_manager.resolve_selected_persona(
            umo=origin, conversation_persona_id=getattr(conversation, "persona_id", None),
            platform_name=platform.meta().name, provider_settings=config.get("provider_settings", {}),
        )
        prompt = str(persona.get("prompt", "")) if persona else ""
        if special_default:
            from astrbot.core.astr_main_agent_resources import CHATUI_SPECIAL_DEFAULT_PERSONA_PROMPT

            prompt = CHATUI_SPECIAL_DEFAULT_PERSONA_PROMPT
        history = []
        if conversation and conversation.history:
            try:
                history = compact_history(json.loads(conversation.history))
            except (ValueError, TypeError):
                pass
        if platform.meta().name == "webchat":
            rows = await self.context.message_history_manager.get(session.platform_id, session.session_id, page_size=12)
            history = compact_history([
                {"role": "assistant" if row.content.get("type") == "bot" else "user",
                 "content": "\n".join(str(item.get("text", "")) for item in row.content.get("message", []) if isinstance(item, dict))}
                for row in rows if isinstance(row.content, dict)
            ])
        event.set_extra("_trace_persona_id", persona_id)
        provider_id = configured_provider or await self.context.get_current_chat_provider_id(origin)
        return ConversationView(origin, event, prompt, persona_id, history, conversation, provider_id)

    def mood(self):
        return self.link.snapshot("short_term_mood")

    async def draft(self, view, prompt, system):
        request = ProviderRequest(prompt=prompt, system_prompt=view.persona + "\n\n" + system,
                                  contexts=list(view.history), conversation=view.conversation)
        star = self.link.peer("short_term_mood", view.origin)
        if star:
            await star.apply_mood(view.event, request)
        self.link.inject(view.event, request)
        response = await self.context.llm_generate(
            chat_provider_id=view.provider_id, prompt=request.prompt, system_prompt=request.system_prompt,
            contexts=request.contexts, extra_user_content_parts=request.extra_user_content_parts,
        )
        return response.completion_text

    async def deliver(self, view, text):
        session, platform = self.platform(view.origin)
        chain = MessageChain().message(text)
        # This adapter exposes confirmation through send_chain; the generic SDK only confirms routing.
        if platform.meta().name == "wechat_native":
            from ..astrbot_plugin_wechat_native.event import session_target

            thread = session_target(session.session_id, platform.native_config.account_id)
            return bool(await platform.send_chain(thread, chain))
        return await self.context.send_message(session, chain)
