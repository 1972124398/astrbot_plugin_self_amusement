import asyncio
import importlib.util
import json
import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

SDK_AVAILABLE = importlib.util.find_spec("astrbot") is not None
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

if SDK_AVAILABLE:
    from astrbot.api.provider import ProviderRequest
    from astrbot_plugin_self_amusement.social import REFLECTION_SYSTEM
    import test_plugin as fixtures
    from test_plugin import FakeEvent


@unittest.skipUnless(SDK_AVAILABLE, "Requires AstrBot SDK")
class BackgroundTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixtures.PluginTests.asyncSetUp(self)

    async def asyncTearDown(self):
        await fixtures.PluginTests.asyncTearDown(self)

    def reflection_job(self, origin=None):
        origin = origin or self.origin
        return {"origin": origin, "version": self.plugin.state.targets[origin].interaction_version,
                "persona": "自然的朋友", "history": [{"role": "user", "content": "这个梗有意思"}],
                "answered_my_initiative": False, "ready_at": 0}

    def impression(self):
        return SimpleNamespace(completion_text=json.dumps({
            "feeling": "enjoyed", "desire": "eager", "contact": "welcome",
            "note": "刚才接梗很开心", "shared_interests": ["猫"], "next_topic": "接着讲猫的故事",
        }))

    async def test_frontend_only_captures_snapshots_and_never_resolves_persona_or_writes(self):
        self.plugin.runtime.reflection_persona = lambda _: (_ for _ in ()).throw(AssertionError("Foreground resolved persona"))
        event = FakeEvent()
        with patch.object(self.plugin.store, "save", side_effect=AssertionError("Foreground wrote storage")):
            await asyncio.wait_for(self.plugin.observe_request(event, ProviderRequest(prompt="你好", system_prompt="原人设")), 0.1)
            await asyncio.wait_for(self.plugin.observe_response(event, SimpleNamespace(completion_text="来啦")), 0.1)
        self.context.llm_generate.assert_not_awaited()
        self.assertEqual(self.plugin.reflection_jobs[self.origin]["system_prompt"], "原人设")

    async def test_slow_reflection_does_not_hold_up_a_due_idle_activity(self):
        started, release, delivered = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def generate(**kwargs):
            if kwargs["system_prompt"] == REFLECTION_SYSTEM:
                started.set()
                await release.wait()
                return self.impression()
            return SimpleNamespace(completion_text=json.dumps(self.plan))

        async def deliver(*args):
            delivered.set()
            return True

        self.context.llm_generate.side_effect = generate
        self.plugin.runtime.deliver.side_effect = deliver
        self.plugin.reflection_jobs[self.origin] = self.reflection_job()
        await self.plugin.initialize()
        self.plugin.state.next_wake = 0
        self.plugin.wake.set()
        self.plugin.reflection_wake.set()
        await asyncio.wait_for(started.wait(), 1)
        await asyncio.wait_for(delivered.wait(), 1)
        self.assertFalse(release.is_set())
        release.set()

    async def test_incoming_chat_cancels_unfinished_idle_generation_immediately(self):
        started, cancelled = asyncio.Event(), asyncio.Event()

        async def slow(**kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        self.context.llm_generate.side_effect = slow
        await self.plugin.initialize()
        self.plugin.state.next_wake = 0
        self.plugin.wake.set()
        await asyncio.wait_for(started.wait(), 1)
        await asyncio.wait_for(self.plugin.observe_request(FakeEvent(), ProviderRequest(prompt="回来啦")), 0.1)
        await asyncio.wait_for(cancelled.wait(), 1)
        self.plugin.runtime.deliver.assert_not_awaited()
        self.assertTrue(self.plugin.active_turns)
        self.assertEqual(self.plugin.state.sent_today, 0)

    async def test_reflections_merge_while_a_conversation_is_continuing(self):
        self.plugin.config.update({"chat_first": False, "reflection_merge_seconds": 60})
        for text in ("先讲个小脑洞", "还有这个也好玩"):
            event = FakeEvent()
            await self.plugin.observe_request(event, ProviderRequest(prompt=text))
            await self.plugin.observe_response(event, SimpleNamespace(completion_text="接住这个梗了"))
        self.assertEqual(len(self.plugin.reflection_jobs), 1)
        self.context.llm_generate.return_value = self.impression()
        self.context.llm_generate.side_effect = None
        self.plugin.reflection_worker = asyncio.create_task(self.plugin.run_reflections())
        await asyncio.sleep(0.02)
        self.context.llm_generate.assert_not_awaited()
        self.plugin.reflection_jobs[self.origin]["ready_at"] = 0
        self.plugin.reflection_wake.set()
        await asyncio.wait_for(self.wait_for_impression(), 1)
        self.context.llm_generate.assert_awaited_once()
        payload = json.loads(self.context.llm_generate.call_args.kwargs["prompt"])
        self.assertIn("还有这个也好玩", str(payload["recent_dialogue"]))

    async def wait_for_impression(self):
        while not self.plugin.state.targets[self.origin].impressions:
            await asyncio.sleep(0)

    async def test_cancelled_other_session_reflection_resumes_after_foreground_reply(self):
        other = "test:GroupMessage:group"
        self.plugin.config.update({"targets": [self.origin, other], "background_after_chat_seconds": 0})
        self.plugin.refresh_targets()
        started, cancelled = asyncio.Event(), asyncio.Event()

        async def slow(**kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        self.context.llm_generate.side_effect = slow
        self.plugin.reflection_jobs[self.origin] = self.reflection_job()
        await self.plugin.initialize()
        await asyncio.wait_for(started.wait(), 1)
        event = FakeEvent()
        event.unified_msg_origin = other
        await self.plugin.observe_request(event, ProviderRequest(prompt="有人在吗"))
        await asyncio.wait_for(cancelled.wait(), 1)
        await asyncio.sleep(0)
        self.assertIn(self.origin, self.plugin.reflection_jobs)
        self.context.llm_generate.side_effect = None
        self.context.llm_generate.return_value = self.impression()
        await self.plugin.observe_response(event, SimpleNamespace(completion_text=""))
        await asyncio.wait_for(self.wait_for_impression(), 1)

    async def test_slow_storage_keeps_foreground_hooks_responsive(self):
        started = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()
        await self.plugin.initialize()
        original = self.plugin.store.save

        def slow(payload):
            loop.call_soon_threadsafe(started.set)
            if not release.wait(timeout=2):
                raise TimeoutError("Test did not release storage")
            original(payload)

        with patch.object(self.plugin.store, "save", side_effect=slow):
            try:
                self.plugin.changed()
                await asyncio.wait_for(started.wait(), 1)
                event = FakeEvent()
                await asyncio.wait_for(self.plugin.observe_request(event, ProviderRequest(prompt="随便聊聊")), 0.1)
                await asyncio.wait_for(self.plugin.observe_response(event, SimpleNamespace(completion_text="好啊")), 0.1)
            finally:
                release.set()

    async def test_cancelling_a_plan_before_send_does_not_count_as_ignoring_the_ai(self):
        started = asyncio.Event()

        async def reserved_plan():
            self.plugin.delivery_attempted = False
            self.plugin.state.reserve(self.origin, "还没真正发出去的话", time.time())
            started.set()
            await asyncio.Event().wait()

        self.plugin.cycle = reserved_plan
        self.plugin.activity_task = asyncio.create_task(self.plugin.background_activity())
        await started.wait()
        await self.plugin.observe_request(FakeEvent(), ProviderRequest(prompt="我回来了"))
        await asyncio.gather(self.plugin.activity_task, return_exceptions=True)
        self.assertEqual(self.plugin.state.sent_today, 0)
        self.assertEqual(self.plugin.state.targets[self.origin].unanswered, 0)
        self.assertIsNone(self.plugin.state.pending)

    async def test_preview_acknowledges_first_then_returns_its_result_from_background(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def slow(**kwargs):
            started.set()
            await release.wait()
            return SimpleNamespace(completion_text=json.dumps(self.plan))

        self.context.llm_generate.side_effect = slow
        receipt = [text async for text in self.plugin.preview(FakeEvent())]
        self.assertIn("后台", receipt[0])
        self.plugin.runtime.deliver.assert_not_awaited()
        await asyncio.wait_for(started.wait(), 1)
        self.assertFalse(release.is_set())
        release.set()
        await asyncio.wait_for(self.plugin.preview_worker, 1)
        self.plugin.runtime.deliver.assert_awaited_once()
        self.assertIn("自娱预览", self.plugin.runtime.deliver.call_args.args[1])
        self.assertEqual(self.plugin.state.sent_today, 0)

    async def test_preview_pauses_for_chat_and_resumes_without_a_second_request(self):
        started, cancelled = asyncio.Event(), asyncio.Event()
        self.plugin.config["background_after_chat_seconds"] = 0
        attempts = 0

        async def generate(**kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()
            return SimpleNamespace(completion_text=json.dumps(self.plan))

        self.context.llm_generate.side_effect = generate
        [text async for text in self.plugin.preview(FakeEvent())]
        await asyncio.wait_for(started.wait(), 1)
        event = FakeEvent()
        await asyncio.wait_for(self.plugin.observe_request(event, ProviderRequest(prompt="我先问一句")), 0.1)
        await asyncio.wait_for(cancelled.wait(), 1)
        self.plugin.runtime.deliver.assert_not_awaited()
        await self.plugin.observe_response(event, SimpleNamespace(completion_text=""))
        await asyncio.wait_for(self.plugin.preview_worker, 1)
        self.plugin.runtime.deliver.assert_awaited_once()
        self.assertEqual(attempts, 2)

    async def test_reflection_model_can_use_an_independent_provider(self):
        self.plugin.config["reflection_provider_id"] = "small-background-model"
        self.context.llm_generate.side_effect = None
        self.context.llm_generate.return_value = self.impression()
        await self.plugin.reflect(self.reflection_job())
        self.assertEqual(self.context.llm_generate.call_args.kwargs["chat_provider_id"], "small-background-model")

    async def test_a_delivery_already_started_is_allowed_to_finish_once(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def delivering(*args):
            started.set()
            await release.wait()
            return True

        self.plugin.runtime.deliver.side_effect = delivering
        self.plugin.activity_task = asyncio.create_task(self.plugin.background_activity())
        await asyncio.wait_for(started.wait(), 1)
        await asyncio.wait_for(self.plugin.observe_request(FakeEvent(), ProviderRequest(prompt="正好想找你")), 0.1)
        self.assertFalse(self.plugin.activity_task.done())
        release.set()
        await asyncio.wait_for(self.plugin.activity_task, 1)
        self.plugin.runtime.deliver.assert_awaited_once()
        self.assertIsNone(self.plugin.state.pending)
