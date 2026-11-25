import json
import unittest
from dataclasses import asdict
from unittest.mock import AsyncMock

import test_plugin as fixtures

from astrbot_plugin_self_amusement.state import LeisureState


@unittest.skipUnless(fixtures.SDK_AVAILABLE, "Requires AstrBot SDK")
class IsolationTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.PluginTests.asyncSetUp
    asyncTearDown = fixtures.PluginTests.asyncTearDown

    async def test_private_thought_stays_with_origin_and_persona_after_reload(self):
        other = "test:FriendMessage:bob"
        self.plugin.state.refresh_targets([self.origin, other])
        target = self.plugin.state.targets[self.origin]
        target.remember_thought("A_PRIVATE_7391", self.view.persona)
        self.plugin.state = LeisureState.from_dict(asdict(self.plugin.state))
        await self.plugin.prepare(self.origin)
        payload = json.loads(self.context.llm_generate.call_args.kwargs["prompt"])
        self.assertEqual(payload["leisure_thought"], "A_PRIVATE_7391")
        self.plugin.runtime.view = AsyncMock(return_value=self.view)
        await self.plugin.prepare(other)
        payload = json.loads(self.context.llm_generate.call_args.kwargs["prompt"])
        self.assertNotIn("7391", json.dumps(payload))
        self.view.persona = "另一个人格"
        await self.plugin.prepare(self.origin)
        payload = json.loads(self.context.llm_generate.call_args.kwargs["prompt"])
        self.assertNotIn("7391", json.dumps(payload))
        target = self.plugin.state.targets[self.origin]
        target.forget()
        self.assertEqual(target.thought_for("自然的朋友"), "")

    async def test_unowned_legacy_thought_is_discarded(self):
        self.plugin.state = LeisureState.from_dict(
            {"thought": "OLD_PRIVATE_7391", "targets": {self.origin: {}}}
        )
        await self.plugin.prepare(self.origin)
        payload = json.loads(self.context.llm_generate.call_args.kwargs["prompt"])
        self.assertNotIn("7391", json.dumps(payload))
