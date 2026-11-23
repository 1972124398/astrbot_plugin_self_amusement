import importlib.util
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
SDK_AVAILABLE = importlib.util.find_spec("astrbot") is not None
from astrbot_plugin_self_amusement.web import Browser


@unittest.skipUnless(SDK_AVAILABLE, "Requires AstrBot SDK search context")
class SearchTests(unittest.IsolatedAsyncioTestCase):
    async def test_reuses_configured_astrbot_search_with_the_selected_session(self):
        tool = SimpleNamespace(call=AsyncMock(return_value=json.dumps({"results": [{"title": "一个实际结果", "url": "https://example.com/story", "snippet": "相关资料"}]})))
        class Context:
            def get_config(self, **kwargs):
                return {"provider_settings": {"web_search": True, "websearch_provider": "tavily"}}

            def get_llm_tool_manager(self):
                return SimpleNamespace(get_builtin_tool=lambda name: tool)

        # Pydantic's AstrAgentContext expects the framework Context type.
        from astrbot.api.star import Context as SDKContext

        context = object.__new__(SDKContext)
        context.get_config = Context().get_config
        context.get_llm_tool_manager = Context().get_llm_tool_manager
        from astrbot_plugin_self_amusement.runtime import LeisureEvent
        from astrbot.core.platform.message_session import MessageSession
        from astrbot.core.platform.platform_metadata import PlatformMetadata

        platform = SimpleNamespace(meta=lambda: PlatformMetadata("test", "test", "test"))
        event = LeisureEvent(MessageSession.from_str("test:FriendMessage:peer"), platform)
        results = await Browser(context).search(event, "好玩的事", [], timeout=2, max_age_hours=72)
        self.assertEqual(results["mode"], "search")
        self.assertEqual(results["results"][0]["url"], "https://example.com/story")
        wrapper = tool.call.call_args.args[0]
        self.assertEqual(wrapper.context.event.unified_msg_origin, "test:FriendMessage:peer")

    async def test_unavailable_web_with_no_subscriptions_returns_no_material(self):
        context = SimpleNamespace(get_config=lambda **_: {"provider_settings": {"web_search": False}})
        results = await Browser(context).search(SimpleNamespace(unified_msg_origin="test:FriendMessage:peer"), "新闻", [], timeout=2, max_age_hours=72)
        self.assertEqual(results, {"mode": "unavailable", "results": []})
