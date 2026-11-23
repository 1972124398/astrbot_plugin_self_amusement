import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_self_amusement.prompts import finish_text, parse_plan
from astrbot_plugin_self_amusement.state import LeisureState, StateStore, TargetState, quiet_now
from astrbot_plugin_self_amusement.web import parse_feed


class StateTests(unittest.TestCase):
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
