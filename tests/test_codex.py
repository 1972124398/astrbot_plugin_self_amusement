"""Hobby work must stay rare, private until real completion, and respect social pacing."""
import asyncio
import json
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import test_plugin as fixtures


@unittest.skipUnless(fixtures.SDK_AVAILABLE, "Requires AstrBot SDK")
class CodexLeisureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixtures.PluginTests.asyncSetUp(self)
        self.snapshot = {"can_start_leisure": True}
        self.peer = SimpleNamespace(
            config={}, companion_snapshot=lambda _: self.snapshot,
            start_leisure_from_companion=Mock(return_value={"id": "hobby"}),
            finish_leisure_from_companion=Mock(),
        )
        self.context.get_registered_star = lambda name: (
            SimpleNamespace(activated=True, star_cls=self.peer, plugin_id=name)
            if name == "astrbot_plugin_codex_bridge" else None)

    async def asyncTearDown(self):
        await fixtures.PluginTests.asyncTearDown(self)

    def result(self, state="completed"):
        self.snapshot.update(can_start_leisure=False, leisure_result={
            "id": "hobby", "state": state, "result": "做了猫猫小游戏，验证通过，成果 /work/cat.html"})

    async def test_probability_gates_the_option_instead_of_forcing_model_choice(self):
        for chance, draw, expected in ((5, 0.049, True), (5, 0.05, False), (0, 0, False), (100, 0.99, True)):
            self.plugin.config["codex_chance_percent"] = chance
            with patch("astrbot_plugin_self_amusement.main.random.random", return_value=draw):
                await self.plugin.prepare(self.origin)
            payload = json.loads(self.context.llm_generate.call_args.kwargs["prompt"])
            self.assertEqual("codex" in payload["allowed_actions"], expected)
        self.peer.start_leisure_from_companion.assert_not_called()

    async def test_disabled_missing_or_busy_bridge_and_unselected_activity_hide_option(self):
        self.plugin.config["codex_chance_percent"] = 100
        for unavailable in ("busy", "disabled", "missing", "unselected"):
            with self.subTest(unavailable=unavailable):
                self.snapshot["can_start_leisure"] = unavailable != "busy"
                self.peer.config["interop_enabled"] = unavailable != "disabled"
                self.plugin.config["activities"] = ["chat"] if unavailable == "unselected" else ["chat", "codex"]
                registry = self.context.get_registered_star
                if unavailable == "missing":
                    self.context.get_registered_star = lambda _: None
                await self.plugin.prepare(self.origin)
                self.context.get_registered_star = registry
                payload = json.loads(self.context.llm_generate.call_args.kwargs["prompt"])
                self.assertNotIn("codex", payload["allowed_actions"])

    async def test_start_is_private_and_preview_never_starts_real_work(self):
        self.plugin.config["codex_chance_percent"] = 100
        self.plan.update(action="codex", idea="做一个猫猫计时器，有开始和重置按钮")
        await self.plugin.prepare_preview(self.origin)
        self.peer.start_leisure_from_companion.assert_not_called()
        await self.plugin.cycle()
        self.peer.start_leisure_from_companion.assert_called_once()
        self.plugin.runtime.draft.assert_not_awaited()
        self.plugin.runtime.deliver.assert_not_awaited()
        self.assertEqual(self.plugin.state.sent_today, 0)
        self.assertEqual(self.plugin.state.recent[-1]["action"], "codex")

    async def test_chat_arriving_during_planning_prevents_start(self):
        self.plugin.config["codex_chance_percent"] = 100
        self.plan["action"] = "codex"

        async def think(**_):
            self.plugin.state.engage(self.origin, 100, 20)
            return SimpleNamespace(completion_text=json.dumps(self.plan))

        self.context.llm_generate.side_effect = think
        await self.plugin.cycle()
        self.peer.start_leisure_from_companion.assert_not_called()

    async def test_results_use_real_state_and_normal_delivery_budget(self):
        self.result("failed")
        self.plugin.config["codex_chance_percent"] = 0
        await self.plugin.cycle()
        self.context.llm_generate.assert_not_awaited()
        payload = json.loads(self.plugin.runtime.draft.call_args.args[1])
        self.assertEqual(payload["codex_result"]["state"], "failed")
        self.assertEqual(self.plugin.state.sent_today, 1)
        self.assertEqual([call.args[-1] for call in self.peer.finish_leisure_from_companion.call_args_list],
                         ["sending", "sent"])

    async def test_quiet_hours_and_daily_limit_defer_results_without_acknowledging(self):
        self.result()
        with patch("astrbot_plugin_self_amusement.main.quiet_now", return_value=True):
            await self.plugin.cycle()
        self.plugin.config["daily_limit"] = 1
        self.plugin.state.refresh_day(time.time())
        self.plugin.state.sent_today = 1
        await self.plugin.cycle()
        self.plugin.runtime.deliver.assert_not_awaited()
        self.peer.finish_leisure_from_companion.assert_not_called()

    async def test_result_can_stay_private_or_be_unconfirmed_without_repeated_delivery(self):
        self.result()
        self.plugin.runtime.draft.return_value = "[不发送]"
        await self.plugin.cycle()
        self.peer.finish_leisure_from_companion.assert_called_with(self.origin, "hobby", "private")
        self.assertEqual(self.plugin.state.sent_today, 0)
        self.plugin.state.next_wake = 0
        self.plugin.runtime.draft.return_value = "给你做了个猫猫小游戏"
        self.plugin.runtime.deliver.side_effect = TimeoutError()
        with self.assertRaises(TimeoutError):
            await self.plugin.cycle()
        self.peer.finish_leisure_from_companion.assert_called_with(self.origin, "hobby", "unconfirmed")

    async def test_result_interrupted_before_send_remains_pending(self):
        self.result()

        async def draft(*_):
            raise asyncio.CancelledError()

        self.plugin.runtime.draft.side_effect = draft
        with self.assertRaises(asyncio.CancelledError):
            await self.plugin.cycle()
        self.peer.finish_leisure_from_companion.assert_not_called()
