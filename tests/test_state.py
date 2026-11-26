import json
import sys
import tempfile
import unittest
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_self_amusement.prompts import finish_text, parse_plan
from astrbot_plugin_self_amusement.state import LeisureState, StateStore, TargetState, quiet_now
from astrbot_plugin_self_amusement.web import parse_feed


class StateTests(unittest.TestCase):
    def test_empty_model_topic_drops_previous_unanswered_curiosity(self):
        target = TargetState(next_topic="未读消息涨到多少了")
        target.remember({"feeling": "neutral", "desire": "open", "contact": "welcome",
                         "note": "这话头不用继续", "shared_interests": [], "next_topic": ""}, 100, 300)
        self.assertEqual(target.next_topic, "")

    def test_unanswered_message_cannot_reuse_old_topic_or_private_thought_after_reload(self):
        state = LeisureState(targets={"friend": TargetState(next_topic="饭吃没")})
        state.targets["friend"].remember_thought("还想问饭吃没", "persona")
        state.reserve("friend", "饭吃没？", 1000)
        self.assertEqual(state.targets["friend"].impression()["next_topic"], "")
        self.assertEqual(state.targets["friend"].thought_for("persona"), "")
        restored = LeisureState.from_dict(asdict(state))
        self.assertEqual(restored.targets["friend"].thought_for("persona"), "")
        state.cancel_pending()
        self.assertEqual(state.targets["friend"].next_topic, "饭吃没")

    def test_one_rhythm_and_target_pool_for_private_and_group_conversations(self):
        state = LeisureState()
        state.refresh_targets(["qq:GroupMessage:g", "wx:FriendMessage:p"])
        state.initialize(1000, 300)
        state.engage("qq:GroupMessage:g", 1200, 400, human=True)
        self.assertEqual(state.next_wake, 1600)
        self.assertEqual(state.last_engaged, 1200)
        self.assertEqual(len(state.targets), 2)

    def test_cooldown_backoff_and_day_budget_are_shared(self):
        state = LeisureState()
        state.refresh_targets(["private", "group"])
        state.reserve("private", "hello", 100000)
        options = dict(global_gap=100, target_gap=200, unanswered_limit=1, backoff=1000, daily_limit=10)
        self.assertEqual(state.eligible(100050, **options), [])
        self.assertEqual(state.eligible(100150, **options), ["group"])
        self.assertEqual(state.eligible(100300, **options), ["group"])
        self.assertEqual(state.eligible(101100, **options), ["private", "group"])
        state.engage("private", 100400, 200)
        self.assertEqual(state.targets["private"].unanswered, 0)
        self.assertEqual(state.eligible(100500, **{**options, "daily_limit": 1}), [])

    def test_restart_does_not_replay_a_pending_delivery(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory))
            state = LeisureState()
            state.refresh_targets(["target"])
            state.reserve("target", "hello", 1000)
            from dataclasses import asdict

            store.save(asdict(state))
            reloaded = store.load()
            reloaded.initialize(1100, 500)
            self.assertIsNone(reloaded.pending)
            self.assertGreaterEqual(reloaded.next_wake, 1600)
            self.assertEqual(reloaded.targets["target"].unanswered, 1)

    def test_quiet_windows_cross_midnight_and_can_be_disabled(self):
        self.assertTrue(quiet_now("23:00-08:00", datetime(2026, 11, 23, 1)))
        self.assertTrue(quiet_now("23:00-08:00,12:00-13:00", datetime(2026, 11, 23, 12, 30)))
        self.assertFalse(quiet_now("23:00-08:00", datetime(2026, 11, 23, 8)))
        self.assertFalse(quiet_now("", datetime(2026, 11, 23, 1)))

    def test_plans_allow_private_browsing_and_reject_unknown_actions(self):
        plan = {"action": "browse", "idea": "猫为什么爱看窗外", "query": "猫 看窗外", "private_note": "想多看点猫的趣事", "wait_minutes": 20}
        self.assertEqual(parse_plan(json.dumps(plan), ["browse", "wait"])["action"], "browse")
        with self.assertRaises(ValueError):
            parse_plan(json.dumps({**plan, "action": "send_to_everyone"}), ["browse", "wait"])
        with self.assertRaises(ValueError):
            parse_plan(json.dumps({**plan, "wait_minutes": float("nan")}), ["browse", "wait"])

    def test_real_source_link_remains_whole_after_body_truncation(self):
        source = "https://example.com/verified-story"
        text = finish_text("有意思的小发现" * 30 + " [来源](" + source + ")", 80, source)
        self.assertTrue(text.endswith(source))
        self.assertEqual(text.count(source), 1)
        self.assertLessEqual(len(text.splitlines()[0]), 80)
        self.assertEqual(finish_text("[不发送]", 100, source), "")

    def test_feed_parser_accepts_rss_and_atom_and_filters_old_news(self):
        now = datetime(2026, 11, 23, 12).timestamp()
        rss = b'<rss><channel><item><title>A</title><link>https://example.com/a</link><pubDate>Mon, 23 Nov 2026 10:00:00 +0800</pubDate><description>Some &lt;b&gt;news&lt;/b&gt;</description></item><item><title>Old</title><link>https://example.com/old</link><pubDate>Mon, 23 Nov 2020 10:00:00 +0800</pubDate></item></channel></rss>'
        results = parse_feed(rss, now=now, max_age_hours=72)
        self.assertEqual([entry["title"] for entry in results], ["A"])
        self.assertEqual(results[0]["snippet"], "Some news")
        atom = b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>B</title><link href="https://example.com/b"/><summary>Hello</summary></entry></feed>'
        self.assertEqual(parse_feed(atom, now=now, max_age_hours=72)[0]["url"], "https://example.com/b")

    def test_native_umo_ids_are_kept_intact(self):
        origin = "w1:FriendMessage:account-x|private|peer%3Aid"
        state = LeisureState()
        state.refresh_targets([origin])
        self.assertIn(origin, LeisureState.from_dict({"targets": {origin: {}}}).targets)

    def test_happy_company_is_more_attractive_than_perfunctory_or_hurtful_contact(self):
        warm, cold, hurt = TargetState(), TargetState(), TargetState()
        for target, feeling, desire in [(warm, "delighted", "eager"), (cold, "neutral", "hesitant"), (hurt, "hurt", "avoid")]:
            target.remember({"feeling": feeling, "desire": desire, "contact": "welcome",
                             "note": "这段相处留下的感觉", "shared_interests": [], "next_topic": ""}, 100, 300)
        self.assertGreater(warm.attraction(), cold.attraction() * 5)
        self.assertGreater(cold.attraction(), hurt.attraction())

    def test_silence_is_not_rewarded_by_waiting_longer_and_stops_chasing(self):
        state = LeisureState()
        state.refresh_targets(["warm", "silent"])
        for now in (100000, 100100):
            state.reserve("silent", "hello", now)
        options = dict(global_gap=0, target_gap=0, unanswered_limit=2, backoff=1000,
                       daily_limit=0, unanswered_policy="wait_for_reply")
        self.assertEqual(state.eligible(900000, **options), ["warm"])
        state.engage("silent", 900001, 100)
        self.assertIn("silent", state.eligible(900002, **options))

    def test_sincere_new_interactions_can_repair_an_old_bad_impression(self):
        target = TargetState()
        for _ in range(3):
            target.remember({"feeling": "hurt", "desire": "avoid", "contact": "welcome",
                             "note": "聊着不太舒服", "shared_interests": [], "next_topic": ""}, 100, 300)
        before = target.attraction()
        target.remember({"feeling": "enjoyed", "desire": "open", "contact": "welcome",
                         "note": "这次认真聊开了，有一点回暖", "shared_interests": ["猫"], "next_topic": "想接着聊猫"}, 200, 300)
        self.assertGreater(target.attraction(), before)
        self.assertEqual(target.shared_interests, ["猫"])

    def test_old_installation_gets_neutral_social_memory_and_new_memory_survives_restart(self):
        state = LeisureState.from_dict({"targets": {"friend": {"last_sent": 100, "unanswered": 1}}})
        self.assertEqual(state.targets["friend"].impressions, [])
        state.targets["friend"].remember({"feeling": "enjoyed", "desire": "eager", "contact": "welcome",
                                         "note": "聊完还想接着玩", "shared_interests": ["音乐"], "next_topic": "想听那首歌"}, 100, 300)
        from dataclasses import asdict

        restored = LeisureState.from_dict(asdict(state))
        self.assertEqual(restored.targets["friend"].next_topic, "想听那首歌")
