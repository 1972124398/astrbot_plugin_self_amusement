import asyncio
import importlib.util
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
SDK_AVAILABLE = importlib.util.find_spec("astrbot") is not None
if SDK_AVAILABLE:
    from astrbot.api.provider import ProviderRequest
    from astrbot_plugin_self_amusement.main import SelfAmusement
    from astrbot_plugin_self_amusement.state import TargetState


class FakeEvent:
    unified_msg_origin = "test:FriendMessage:person"
    message_obj = SimpleNamespace(message_id="incoming", group=None)

    def get_extra(self, key):
        return getattr(self, "extras", {}).get(key)

    def set_extra(self, key, value):
        if not hasattr(self, "extras"):
            self.extras = {}
        self.extras[key] = value

    def get_sender_name(self):
        return "朋友"

    def get_sender_id(self):
        return "person"

    def get_group_id(self):
        return ""

    def get_message_str(self):
        return "你好"

    def plain_result(self, text):
        return text


@unittest.skipUnless(SDK_AVAILABLE, "Requires AstrBot SDK")
class PluginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.origin = FakeEvent.unified_msg_origin
        self.plan = {"action": "chat", "idea": "想聊聊最近的趣事", "query": "", "private_note": "想听点新鲜的小故事", "wait_minutes": 20}
        self.context = SimpleNamespace(
            get_config=lambda **_: {"plugin_set": ["*"]},
            llm_generate=AsyncMock(side_effect=lambda **_: SimpleNamespace(completion_text=json.dumps(self.plan))),
        )
        with patch("astrbot_plugin_self_amusement.main.StarTools.get_data_dir", return_value=Path(self.directory.name)):
            self.plugin = SelfAmusement(self.context, {"targets": [self.origin], "quiet_hours": ""})
        self.view = SimpleNamespace(persona="自然的朋友", history=[], provider_id="model", event=FakeEvent(), origin=self.origin)
        self.plugin.runtime = SimpleNamespace(
            platform=lambda _: (None, None), view=AsyncMock(return_value=self.view),
            mood=lambda: {"inner_state": "有点闲，想找人聊聊"},
            enabled=AsyncMock(return_value=True),
            draft=AsyncMock(return_value="突然想问，你最近有什么觉得特别好玩的事吗？"),
            deliver=AsyncMock(return_value=True),
        )
        self.plugin.state.refresh_targets([self.origin])
        self.plugin.state.last_engaged = time.time() - 4000

    async def asyncTearDown(self):
        await self.plugin.terminate()
        self.directory.cleanup()

    async def test_bored_agent_sends_one_natural_message_then_waits(self):
        await self.plugin.cycle()
        self.plugin.runtime.deliver.assert_awaited_once()
        self.assertEqual(self.plugin.state.sent_today, 1)
        await self.plugin.cycle()
        self.plugin.runtime.deliver.assert_awaited_once()

    async def test_no_targets_means_no_model_call_or_delivery(self):
        self.plugin.config["targets"] = []
        await self.plugin.cycle()
        self.context.llm_generate.assert_not_awaited()
        self.plugin.runtime.deliver.assert_not_awaited()

    async def test_disabled_session_cannot_trigger_a_model_or_delivery(self):
        self.plugin.runtime.enabled.return_value = False
        await self.plugin.cycle()
        self.context.llm_generate.assert_not_awaited()
        self.plugin.runtime.deliver.assert_not_awaited()

    async def test_wait_and_private_browsing_do_not_send(self):
        self.plan["action"] = "wait"
        await self.plugin.cycle()
        self.plugin.runtime.deliver.assert_not_awaited()
        self.plugin.state.next_wake = 0
        self.plan["action"] = "browse"
        self.plugin.browser.search = AsyncMock(return_value={"mode": "rss", "results": [{"title": "一则有趣内容", "url": "https://example.com/story", "snippet": "真实资料"}]})
        await self.plugin.cycle()
        self.plugin.browser.search.assert_awaited_once()
        self.plugin.runtime.deliver.assert_not_awaited()
        self.assertIn("一则有趣内容", self.plugin.state.thought)

    async def test_share_uses_actual_source_and_unavailable_web_sends_nothing(self):
        self.plan["action"] = "share"
        self.plugin.browser.search = AsyncMock(return_value={"mode": "rss", "results": []})
        await self.plugin.cycle()
        self.plugin.runtime.draft.assert_not_awaited()
        self.plugin.state.next_wake = 0
        self.plugin.browser.search.return_value = {"mode": "search", "results": [{"title": "真实新闻", "url": "https://example.com/real", "snippet": "实际内容"}]}
        await self.plugin.cycle()
        sent = self.plugin.runtime.deliver.call_args.args[1]
        self.assertTrue(sent.endswith("https://example.com/real"))

    async def test_real_interaction_while_thinking_cancels_outbound(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def think(**kwargs):
            started.set()
            await release.wait()
            return SimpleNamespace(completion_text=json.dumps(self.plan))

        self.context.llm_generate.side_effect = think
        task = asyncio.create_task(self.plugin.cycle())
        await started.wait()
        self.plugin.state.engage(self.origin, time.time(), 1000)
        release.set()
        await task
        self.plugin.runtime.deliver.assert_not_awaited()

    async def test_preview_has_no_delivery_and_does_not_spend_the_budget(self):
        responses = [text async for text in self.plugin.preview(FakeEvent())]
        self.assertIn("自娱预览", responses[0])
        self.plugin.runtime.deliver.assert_not_awaited()
        self.assertEqual(self.plugin.state.sent_today, 0)

    async def test_own_recent_outbound_is_available_to_the_normal_reply(self):
        self.plugin.state.record(self.origin, "chat", "你刚才想起来的小脑洞", time.time(), "sent")
        request = ProviderRequest(prompt="哈哈", system_prompt="原人设")
        await self.plugin.observe_request(FakeEvent(), request)
        self.assertEqual(request.prompt, "哈哈")
        self.assertEqual(request.system_prompt, "原人设")
        self.assertIn("小脑洞", request.extra_user_content_parts[0].text)
        self.assertTrue(request.extra_user_content_parts[0].model_dump_for_context()["_no_save"])

    async def test_delivery_exception_is_reserved_before_the_network_call(self):
        self.plugin.runtime.deliver.side_effect = RuntimeError("unconfirmed")
        with self.assertRaises(RuntimeError):
            await self.plugin.cycle()
        self.assertIsNotNone(self.plugin.store.load().pending)

    async def test_unified_configuration_uses_one_native_conversation_picker(self):
        from astrbot.api import AstrBotConfig

        schema = json.loads((Path(__file__).resolve().parents[1] / "_conf_schema.json").read_text(encoding="utf-8"))
        config = AstrBotConfig(str(Path(self.directory.name) / "config.json"), schema=schema)
        self.assertEqual(schema["targets"]["_special"], "umo")
        self.assertEqual(config["targets"], [])
        self.assertNotIn("private_targets", config)
        self.assertNotIn("group_targets", config)

    async def test_targets_removed_during_planning_cannot_receive_a_message(self):
        async def draft(*args):
            self.plugin.config["targets"] = []
            return "想聊两句"

        self.plugin.runtime.draft.side_effect = draft
        await self.plugin.cycle()
        self.plugin.runtime.deliver.assert_not_awaited()
