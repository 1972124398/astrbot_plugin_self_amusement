import importlib.util
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
SDK_AVAILABLE = importlib.util.find_spec("astrbot") is not None

if SDK_AVAILABLE:
    from astrbot.core.platform.message_session import MessageSession
    from astrbot.core.platform.platform_metadata import PlatformMetadata
    from astrbot_plugin_self_amusement.runtime import LeisureEvent, Runtime
    from astrbot_plugin_self_amusement.state import TargetState


@unittest.skipUnless(SDK_AVAILABLE, "Requires AstrBot SDK")
class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.platform = SimpleNamespace(meta=lambda: PlatformMetadata("test", "test", "test"), bot_self_id="bot")
        self.persona = {"prompt": "你是喜欢闲聊和冷知识的朋友。"}
        self.context = SimpleNamespace(
            get_platform_inst=lambda _: self.platform,
            get_config=lambda **_: {"plugin_set": ["*"]},
            conversation_manager=SimpleNamespace(
                get_curr_conversation_id=AsyncMock(return_value="cid"),
                get_conversation=AsyncMock(return_value=SimpleNamespace(cid="cid", persona_id="friend", history=json.dumps([{"role": "user", "content": "我喜欢猫"}]))),
            ),
            persona_manager=SimpleNamespace(resolve_selected_persona=AsyncMock(side_effect=lambda **_: ("friend", self.persona, None, False))),
            get_current_chat_provider_id=AsyncMock(return_value="test-model"),
            get_registered_star=lambda _: None,
            llm_generate=AsyncMock(return_value=SimpleNamespace(completion_text="猫盯着窗外的时候都在想什么呢？")),
            send_message=AsyncMock(return_value=True),
        )
        self.runtime = Runtime(self.context)

    async def test_group_and_private_events_use_their_exact_session_and_persona(self):
        for origin in ["test:FriendMessage:peer", "test:GroupMessage:group"]:
            view = await self.runtime.view(origin, TargetState())
            self.assertEqual(view.event.unified_msg_origin, origin)
            self.assertTrue(view.event.message_obj.message_id.startswith("proactive:"))
            self.assertEqual(view.event.get_extra("_trace_persona_id"), "friend")
            self.assertEqual(view.persona, self.persona["prompt"])
            self.assertEqual(view.history[0]["content"], "我喜欢猫")
            if "GroupMessage" in origin:
                self.assertEqual(view.event.get_group_id(), "group")

    async def test_draft_uses_sdk_request_and_delivery_routes_once(self):
        view = await self.runtime.view("test:FriendMessage:peer", TargetState())
        text = await self.runtime.draft(view, "一个小念头", "说点自然的闲话")
        self.assertIn("窗外", text)
        call = self.context.llm_generate.call_args.kwargs
        self.assertIn(self.persona["prompt"], call["system_prompt"])
        self.assertEqual(call["contexts"], view.history)
        self.assertTrue(await self.runtime.deliver(view, text))
        self.context.send_message.assert_awaited_once()

    async def test_short_term_mood_is_reused_without_generic_hook_dispatch(self):
        mood = SimpleNamespace(config={"enabled": True},
                               read_state=lambda: SimpleNamespace(levels={"joy": .6}, inner_state="有点高兴", response_tendency="想找人接梗"),
                               apply_mood=AsyncMock())
        star = SimpleNamespace(activated=True, name="astrbot_plugin_short_term_mood", plugin_id="W1/astrbot_plugin_short_term_mood", star_cls=mood)
        self.context.get_registered_star = lambda _: star
        self.assertEqual(self.runtime.mood()["levels"]["joy"], .6)
        view = await self.runtime.view("test:FriendMessage:peer", TargetState())
        await self.runtime.draft(view, "搭话", "自然说话")
        mood.apply_mood.assert_awaited_once()
