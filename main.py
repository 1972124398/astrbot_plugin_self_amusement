from __future__ import annotations

import asyncio
import json
import random
import time
from dataclasses import asdict
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.agent.message import TextPart

from .prompts import MESSAGE_SYSTEM, PLAN_SYSTEM, finish_text, parse_plan, DEFAULT_ACTIVITIES
from .runtime import Runtime, compact_history
from .social import REFLECTION_SYSTEM, parse_reflection
from .state import LeisureState, StateStore, TargetState, number, quiet_now
from .web import Browser
from .interop import CompanionLink, conversation_key, event_kind, human_dialogue, notice_ignored


class SelfAmusement(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.link = CompanionLink(context, "self_amusement", config)
        self.store = StateStore(Path(StarTools.get_data_dir("astrbot_plugin_self_amusement")))
        try:
            self.state = self.store.load()
        except (ValueError, TypeError, AttributeError):
            logger.warning("Self-amusement could not read its previous state, starting with a fresh idle period")
            self.state = LeisureState()
        self.runtime = Runtime(context, config)
        self.browser = Browser(context)
        self.cycle_lock = asyncio.Lock()
        self.save_lock = asyncio.Lock()
        self.wake = asyncio.Event()
        self.reflection_wake = asyncio.Event()
        self.save_wake = asyncio.Event()
        self.preview_wake = asyncio.Event()
        self.worker = None
        self.reflection_worker = None
        self.save_worker = None
        self.activity_task = None
        self.reflection_task = None
        self.active_reflection = None
        self.preview_worker = None
        self.preview_task = None
        self.active_turns = {}
        self.turn_serial = 0
        self.last_chat_activity = 0
        self.sending = False
        self.delivery_attempted = False
        self.closing = False
        self.dirty = False
        self.recent_inputs = {}
        self.reflection_jobs = {}
        self.outbound_hints = {}
        self.activity = "等待闲下来"
        self.last_error = ""

    def setting(self, key, default, minimum, maximum):
        return number(self.config.get(key, default), default, minimum, maximum)

    def origins(self):
        return list(dict.fromkeys(value.strip() for value in self.config.get("targets", [])
                                  if isinstance(value, str) and value.strip()))

    def idle_delay(self):
        lower = self.setting("idle_min_minutes", 15, 1, 1440)
        upper = max(lower, self.setting("idle_max_minutes", 45, 1, 1440))
        return random.uniform(lower, upper) * 60

    def companion_snapshot(self, origin=""):
        if not origin:
            return {}
        target = next((target for umo, target in self.state.targets.items() if conversation_key(umo) == conversation_key(origin)), None)
        if not target:
            return {}
        limit = int(self.setting("max_unanswered", 2, 0, 20))
        return {"impression": target.impression() if self.social_enabled() else {},
                "attraction": target.attraction(str(self.config.get("preference_strength", "natural"))) if self.social_enabled() else 1.0,
                "wants_space": target.wants_space or bool(limit and target.unanswered >= limit) or time.time() < target.available_after,
                "recent_own_messages": [row["text"] for row in self.state.recent
                    if conversation_key(row["origin"]) == conversation_key(origin) and row.get("status") == "sent"][-3:]}

    def companion_observe(self, packet):
        if self.closing or not self.link.enabled:
            return
        origin = next((umo for umo in self.state.targets if conversation_key(umo) == conversation_key(packet["origin"])), "")
        kind, data = packet["kind"], packet["data"]
        if kind == "human_activity":
            self.state.engage(origin, time.time(), self.idle_delay(), human=True)
            self.last_chat_activity = time.monotonic()
            if self.activity_task and not self.activity_task.done() and not self.sending:
                self.activity_task.cancel()
        elif origin and kind == "initiative":
            target = self.state.targets[origin]
            target.last_sent = time.time()
            target.unanswered += 1
            self.state.last_outbound = time.time()
            self.state.next_wake = time.time() + self.idle_delay()
        elif origin and kind == "game_turn" and not data.get("handled_by_chat"):
            self.state.engage(origin, time.time(), self.idle_delay(), human=False)
            history = compact_history([
                *self.recent_inputs.get(origin, []),
                {"role": "user", "content": data.get("human_text") or "（一起玩游戏）"},
                {"role": "assistant", "content": data.get("reply", "")},
            ])
            self.recent_inputs[origin] = history
            if self.social_enabled():
                self.reflection_jobs[origin] = {
                    "origin": origin, "version": self.state.targets[origin].interaction_version,
                    "persona": data.get("persona", ""), "history": history,
                    "answered_my_initiative": True,
                    "ready_at": time.monotonic() + self.setting("reflection_merge_seconds", 20, 0, 180),
                }
                self.reflection_wake.set()
        self.changed()
        self.wake.set()

    async def initialize(self):
        self.state.refresh_targets(self.origins())
        self.state.initialize(time.time(), self.idle_delay())
        await self.persist()
        self.refresh_outbound_hints()
        self.worker = asyncio.create_task(self.run(), name="self-amusement-leisure")
        self.reflection_worker = asyncio.create_task(self.run_reflections(), name="self-amusement-reflections")
        self.save_worker = asyncio.create_task(self.run_saves(), name="self-amusement-save")

    async def persist(self):
        async with self.save_lock:
            payload = asdict(self.state)
            self.dirty = False
            writing = asyncio.create_task(asyncio.to_thread(self.store.save, payload))
            try:
                await asyncio.shield(writing)
            except asyncio.CancelledError:
                await writing
                raise

    def changed(self):
        self.dirty = True
        self.save_wake.set()

    def refresh_targets(self):
        self.state.refresh_targets(self.origins())
        self.recent_inputs = {origin: value for origin, value in self.recent_inputs.items() if origin in self.state.targets}
        self.reflection_jobs = {origin: value for origin, value in self.reflection_jobs.items() if origin in self.state.targets}
        self.refresh_outbound_hints()

    def refresh_outbound_hints(self):
        hints = {}
        for item in self.state.recent:
            if item.get("status") == "sent" and item.get("text"):
                hints.setdefault(item["origin"], []).append(item["text"])
        self.outbound_hints = {
            origin: "你在这个会话最近主动说过的话，供接续当前交流：\n" + "\n".join(texts[-3:])
            for origin, texts in hints.items() if origin in self.state.targets
        }

    def record_activity(self, origin, action, text, now, status):
        self.state.record(origin, action, text, now, status)
        self.refresh_outbound_hints()

    def background_ready(self):
        if not self.config.get("chat_first", True):
            return True
        now = time.monotonic()
        # An aborted framework request may never emit its completion hook.
        self.active_turns = {key: started for key, started in self.active_turns.items() if now - started < 600}
        return not self.active_turns and now - self.last_chat_activity >= self.setting("background_after_chat_seconds", 10, 0, 120)

    def chat_started(self, event):
        token = event.get_extra("self_amusement_turn")
        if token is None:
            self.turn_serial += 1
            token = self.turn_serial
            event.set_extra("self_amusement_turn", token)
        self.active_turns[token] = time.monotonic()
        self.last_chat_activity = time.monotonic()
        if self.activity_task and not self.activity_task.done() and not self.sending:
            self.activity_task.cancel()
        if self.reflection_task and not self.reflection_task.done():
            if self.config.get("chat_first", True) or (self.active_reflection or {}).get("origin") == event.unified_msg_origin:
                self.reflection_task.cancel()
        if self.preview_task and not self.preview_task.done() and self.config.get("chat_first", True):
            self.preview_task.cancel()

    def chat_finished(self, event):
        self.active_turns.pop(event.get_extra("self_amusement_turn"), None)
        self.last_chat_activity = time.monotonic()
        self.reflection_wake.set()
        self.preview_wake.set()
        self.wake.set()

    async def run_saves(self):
        while True:
            await self.save_wake.wait()
            self.save_wake.clear()
            await asyncio.sleep(0.2)
            if self.dirty:
                try:
                    await self.persist()
                except Exception as error:
                    self.dirty = True
                    logger.warning("Self-amusement could not save its current state (%s)", type(error).__name__)
                    await asyncio.sleep(5)
                    self.save_wake.set()

    async def run_reflections(self):
        while True:
            self.reflection_wake.clear()
            ready = [(origin, job) for origin, job in self.reflection_jobs.items()
                     if job.get("ready_at", 0) <= time.monotonic()]
            if not self.background_ready() or not ready:
                if not self.reflection_jobs:
                    await self.reflection_wake.wait()
                    continue
                deadline = min(job.get("ready_at", 0) for job in self.reflection_jobs.values())
                if self.config.get("chat_first", True):
                    deadline = max(deadline, self.last_chat_activity + self.setting("background_after_chat_seconds", 10, 0, 120))
                delay = 30 if self.active_turns and self.config.get("chat_first", True) else max(0.05, deadline - time.monotonic())
                try:
                    await asyncio.wait_for(self.reflection_wake.wait(), timeout=delay)
                except TimeoutError:
                    pass
                continue
            origin, job = ready[0]
            self.reflection_jobs.pop(origin)
            self.active_reflection = job
            self.reflection_task = asyncio.create_task(self.reflect(job), name="self-amusement-reflect-one")
            try:
                await self.reflection_task
            except asyncio.CancelledError:
                if self.closing or asyncio.current_task().cancelling():
                    raise
                target = self.state.targets.get(origin)
                if target and target.interaction_version == job["version"] and origin not in self.reflection_jobs:
                    self.reflection_jobs[origin] = job
            except Exception as error:
                logger.warning("Self-amusement reflection worker could not finish this thought (%s)", type(error).__name__)
            finally:
                self.reflection_task = None
                self.active_reflection = None

    def eligible(self, now):
        self.refresh_targets()
        return self.state.eligible(
            now, global_gap=self.setting("global_gap_minutes", 20, 0, 1440) * 60,
            target_gap=self.setting("target_gap_minutes", 60, 0, 10080) * 60,
            unanswered_limit=int(self.setting("max_unanswered", 2, 0, 20)),
            backoff=self.setting("unanswered_backoff_minutes", 180, 1, 10080) * 60,
            daily_limit=int(self.setting("daily_limit", 12, 0, 1000)),
            unanswered_policy=str(self.config.get("unanswered_policy", "wait_for_reply")),
        )

    def social_enabled(self):
        return bool(self.config.get("enabled", True) and self.config.get("remember_chemistry", True))

    def attraction(self, origin, now):
        target = self.state.targets[origin]
        warmth = target.attraction(str(self.config.get("preference_strength", "natural"))) if self.social_enabled() else 1 / (1 + target.unanswered) ** 2
        elapsed = max(0, now - target.last_sent) if target.last_sent else 0
        return warmth * (1 + min(0.5, elapsed / 86400))

    async def reflect(self, job):
        origin = job["origin"]
        target = self.state.targets.get(origin)
        if not self.social_enabled() or not target or target.interaction_version != job["version"]:
            return
        payload = {
            "persona": job.get("persona") if "persona" in job else self.runtime.reflection_persona(job),
            "recent_dialogue": job["history"],
            "previous_impression": target.impression(),
            "answered_my_initiative": job["answered_my_initiative"],
            "recent_initiatives": [item.get("text", "") for item in self.state.recent
                                   if item.get("origin") == origin and item.get("status") == "sent"][-3:],
        }
        try:
            async with asyncio.timeout(self.setting("reflection_timeout_seconds", 30, 5, 120)):
                provider_id = str(self.config.get("reflection_provider_id", "") or self.config.get("provider_id", "") or "").strip()
                provider_id = provider_id or await self.context.get_current_chat_provider_id(origin)
                response = await self.context.llm_generate(
                    chat_provider_id=provider_id, system_prompt=REFLECTION_SYSTEM,
                    prompt=json.dumps(payload, ensure_ascii=False),
                )
            impression = parse_reflection(response.completion_text)
            if (self.closing or not self.social_enabled() or self.state.targets.get(origin) is not target
                    or target.interaction_version != job["version"]):
                return
            target.remember(impression, time.time(), self.setting("idle_max_minutes", 45, 1, 1440) * 60)
            self.changed()
        except Exception as error:
            logger.warning("Self-amusement could not reflect on this conversation (%s)", type(error).__name__)

    def permitted(self, origin):
        try:
            self.runtime.platform(origin)
            config = self.context.get_config(umo=origin)
            plugins = config.get("plugin_set", ["*"])
            return "*" in plugins or "astrbot_plugin_self_amusement" in plugins or getattr(self, "plugin_id", "") in plugins
        except (ValueError, TypeError):
            return False

    async def prepare(self, origin):
        target = self.state.targets.get(origin, TargetState())
        view = await self.runtime.view(origin, target, str(self.config.get("provider_id", "") or "").strip())
        if origin in self.recent_inputs:
            view.history = list(self.recent_inputs[origin])
        configured = self.config.get("activities", DEFAULT_ACTIVITIES)
        actions = [value for value in configured if value in {"chat", "mutter", "share", "browse", "wait"}]
        if not self.config.get("browse_enabled", True):
            actions = [value for value in actions if value not in {"share", "browse"}]
        actions = list(dict.fromkeys([*actions, "wait"]))
        play = self.link.snapshot("play_together", origin)
        if self.config.get("interop_play", True) and play.get("can_invite"):
            actions.append("play")
        codex = self.link.snapshot("codex_bridge", origin) if "codex" in configured else {}
        result = codex.get("leisure_result")
        if result:
            self.activity = "看看刚做好的小东西，想想怎么分享"
            plan = {
                "action": "codex_result",
                "idea": "闲时动手的结果",
                "query": "",
                "private_note": target.thought_for(view.persona),
                "wait_minutes": 20,
            }
            material = {
                "action": "codex_result",
                "codex_result": result,
                "current_time": datetime.now()
                .astimezone()
                .isoformat(timespec="minutes"),
                "preferences": str(self.config.get("behavior_preferences", "") or "")[
                    :2000
                ],
                "max_characters": int(self.setting("message_length", 220, 40, 1200)),
            }
            text = await self.runtime.draft(
                view, json.dumps(material, ensure_ascii=False), MESSAGE_SYSTEM
            )
            return (
                view,
                plan,
                finish_text(text, int(self.setting("message_length", 220, 40, 1200))),
                {"mode": "codex", "results": [], "codex_job_id": result["id"]},
            )
        if (
            codex.get("can_start_leisure")
            and random.random() < self.setting("codex_chance_percent", 5, 0, 100) / 100
        ):
            actions.append("codex")
        recent = [item for item in self.state.recent if item.get("origin") == origin][-5:]
        payload = {
            "persona": view.persona,
            "mood": self.link.snapshot("short_term_mood", origin),
            "recent_dialogue": view.history,
            "recent_actions": recent,
            "leisure_thought": target.thought_for(view.persona),
            "interests": self.config.get("interests", []),
            "idle_minutes": round(
                max(0, time.time() - self.state.last_engaged) / 60, 1
            ),
            "unanswered": target.unanswered,
            "allowed_actions": actions,
            "how_it_feels_with_you": target.impression()
            if self.social_enabled()
            else {},
            "current_time": datetime.now().astimezone().isoformat(timespec="minutes"),
            "preferences": str(self.config.get("behavior_preferences", "") or "")[:2000],
            "companion": self.link.context_for(origin),
        }
        self.activity = "琢磨做点什么"
        response = await self.context.llm_generate(
            chat_provider_id=view.provider_id, system_prompt=PLAN_SYSTEM,
            prompt=json.dumps(payload, ensure_ascii=False),
        )
        plan = parse_plan(response.completion_text, actions)
        sources = {"mode": "none", "results": []}
        if plan["action"] in {"browse", "share"}:
            self.activity = "在网上闲逛"
            query = plan["query"] or plan["idea"] or "有趣的新鲜事"
            sources = await self.browser.search(
                view.event, query, self.config.get("rss_feeds", []),
                timeout=self.setting("web_timeout_seconds", 15, 1, 90),
                max_age_hours=self.setting("feed_max_age_hours", 72, 1, 720),
            )
            if not sources["results"]:
                return view, plan, "", sources
            interests = [*target.shared_interests, *self.config.get("interests", [])]
            sources["results"].sort(
                key=lambda entry: sum(1 for topic in interests if isinstance(topic, str) and topic
                                      and topic.casefold() in (entry["title"] + " " + entry.get("snippet", "")).casefold()),
                reverse=True,
            )
            if plan["action"] == "browse":
                plan["private_note"] = "想接着看看：" + sources["results"][0]["title"][:100]
            else:
                since = time.time() - self.setting("repeat_window_hours", 24, 1, 168) * 3600
                used = "\n".join(item.get("text", "") for item in self.state.recent if item.get("time", 0) >= since)
                sources["results"] = [entry for entry in sources["results"] if entry["url"] not in used][:1]
                if not sources["results"]:
                    return view, plan, "", sources
        if plan["action"] in {"wait", "browse", "play", "codex"}:
            return view, plan, "", sources
        self.activity = "想好怎么开口"
        material = {"action": plan["action"], "idea": plan["idea"], "sources": sources,
                    "unanswered": target.unanswered, "companion": payload["companion"],
                    "recent_own_messages": [item.get("text", "") for item in recent],
                    "current_time": payload["current_time"],
                    "how_it_feels_with_you": payload["how_it_feels_with_you"],
                    "preferences": payload["preferences"], "max_characters": int(self.setting("message_length", 220, 40, 1200))}
        text = await self.runtime.draft(view, json.dumps(material, ensure_ascii=False), MESSAGE_SYSTEM)
        source = sources["results"][0]["url"] if plan["action"] == "share" and sources["results"] else ""
        text = finish_text(text, int(self.setting("message_length", 220, 40, 1200)), source)
        return view, plan, text, sources

    def finish_codex_result(self, origin, sources, status):
        job_id = sources.get("codex_job_id")
        if job_id:
            peer = self.link.peer("codex_bridge", origin)
            finish = getattr(peer, "finish_leisure_from_companion", None)
            if finish:
                return finish(origin, job_id, status)
            return False
        return True

    def repetitive(self, origin, text, now):
        since = now - self.setting("repeat_window_hours", 24, 1, 168) * 3600
        for item in self.state.recent:
            if item.get("origin") == origin and item.get("time", 0) >= since and item.get("status") in {"sent", "unconfirmed"}:
                if SequenceMatcher(None, item.get("text", ""), text).ratio() >= 0.88:
                    return True
        return False

    async def cycle(self):
        token = self.link.claim_idle()
        if token is None:
            return
        try:
            return await self._cycle(token)
        finally:
            self.link.release_idle(token)

    async def _cycle(self, idle_token):
        now = time.time()
        if self.closing or not self.config.get("enabled", True) or now < self.state.next_wake or not self.background_ready():
            return
        if quiet_now(str(self.config.get("quiet_hours", "00:00-08:00") or ""), datetime.fromtimestamp(now)):
            self.activity = "安静时段，自己待着"
            return
        candidates = []
        for origin in self.eligible(now):
            if (self.permitted(origin) and self.link.routine_allows_initiative(origin)
                    and await self.runtime.enabled(origin)):
                candidates.append(origin)
        if not candidates:
            self.activity = "暂时没有适合搭话的会话"
            return
        if self.cycle_lock.locked():
            return
        async with self.cycle_lock:
            self.delivery_attempted = False
            weights = [self.attraction(origin, now) for origin in candidates]
            origin = random.choices(candidates, weights=weights)[0]
            revision = self.state.revision
            self.state.next_wake = now + self.idle_delay()
            await self.persist()
            async with asyncio.timeout(self.setting("generation_timeout_seconds", 60, 5, 300)):
                view, plan, text, sources = await self.prepare(origin)
            if (self.closing or revision != self.state.revision or not self.config.get("enabled", True)
                    or not self.link.idle_current(idle_token)):
                return
            self.state.targets[origin].remember_thought(
                plan["private_note"], view.persona
            )
            if sources.get("results") and plan["action"] in {"browse", "share"}:
                self.link.publish("leisure_experience", origin, {
                    "action": plan["action"], "idea": plan["idea"], "private_note": plan["private_note"],
                    "materials": [{"title": row["title"], "snippet": row.get("snippet", "")[:500]} for row in sources["results"][:3]],
                    "persona": view.persona,
                })
            delay = min(plan["wait_minutes"] * 60, self.setting("idle_max_minutes", 45, 1, 1440) * 60)
            self.state.next_wake = time.time() + delay
            self.activity = {"wait": "继续发会儿呆", "browse": "逛了一会儿，先记在心里"}.get(plan["action"], "准备开口")
            if plan["action"] == "codex":
                peer = self.link.peer("codex_bridge", origin)
                start = getattr(peer, "start_leisure_from_companion", None)
                receipt = None
                if (start and origin in self.eligible(time.time()) and self.permitted(origin)
                        and await self.runtime.enabled(origin) and self.link.idle_current(idle_token)):
                    receipt = start(origin, plan["idea"], {
                        "persona": view.persona[:3000], "recent_dialogue": view.history,
                        "interests": self.config.get("interests", []),
                        "mood": self.link.snapshot("short_term_mood", origin),
                    }, idle_token)
                self.activity = "悄悄动手做点小东西，做好再来" if receipt else "先把这个小念头留着"
                self.record_activity(origin, "codex", plan["idea"], time.time(), "private")
                await self.persist()
                return
            if plan["action"] == "play":
                play = self.link.peer("play_together", origin)
                invite = getattr(play, "invite_from_companion", None)
                invited = await invite(origin, plan["idea"], idle_token) if invite else False
                self.activity = "想找对方玩一小局" if invited else "这会儿先把玩心留着"
                self.record_activity(origin, "play", "", time.time(), "private")
                self.changed()
                return
            if not text:
                self.finish_codex_result(origin, sources, "private")
                self.record_activity(origin, plan["action"], "", time.time(), "private")
                await self.persist()
                return
            current = time.time()
            if (origin not in self.eligible(current) or not self.permitted(origin)
                    or not await self.runtime.enabled(origin) or self.repetitive(origin, text, current)):
                return
            if quiet_now(str(self.config.get("quiet_hours", "00:00-08:00") or ""), datetime.fromtimestamp(current)):
                return
            self.state.reserve(origin, text, current)
            await self.persist()
            if (revision != self.state.revision or self.closing or origin not in self.origins()
                    or not self.config.get("enabled", True) or not self.link.idle_current(idle_token)):
                self.state.cancel_pending()
                await self.persist()
                return
            self.activity = "发出一点闲话"
            if not self.finish_codex_result(origin, sources, "sending"):
                self.state.cancel_pending()
                await self.persist()
                return
            self.sending = True
            self.delivery_attempted = True
            delivered = False
            try:
                async with asyncio.timeout(self.setting("send_timeout_seconds", 30, 3, 120)):
                    delivered = await self.runtime.deliver(view, text)
            finally:
                self.sending = False
                self.finish_codex_result(origin, sources, "sent" if delivered else "unconfirmed")
            self.record_activity(origin, plan["action"], text, time.time(), "sent" if delivered else "unconfirmed")
            if delivered:
                self.link.sent(origin, text)
            self.activity = "刚聊了一句，等一会儿"
            await self.persist()
            self.last_error = ""

    async def background_activity(self):
        try:
            await self.cycle()
        except asyncio.CancelledError:
            if self.state.pending and not self.delivery_attempted:
                self.state.cancel_pending()
                self.changed()
            if not self.closing:
                self.activity = "先陪人聊，闲下来再玩"
            raise
        except Exception as error:
            self.last_error = type(error).__name__
            self.activity = "这次没想好，稍后再试"
            if self.state.pending:
                pending = self.state.pending
                self.record_activity(pending["origin"], "attempt", pending["text"], pending["time"], "unconfirmed")
            self.state.next_wake = time.time() + self.setting("retry_minutes", 10, 1, 240) * 60
            self.changed()
            logger.warning("Self-amusement activity unavailable (%s)", type(error).__name__)

    async def run(self):
        while True:
            self.wake.clear()
            try:
                self.refresh_targets()
                preview_pending = self.preview_worker and not self.preview_worker.done()
                if self.background_ready() and not preview_pending and (self.activity_task is None or self.activity_task.done()):
                    self.activity_task = asyncio.create_task(self.background_activity(), name="self-amusement-activity")
            except Exception as error:
                self.last_error = type(error).__name__
                logger.warning("Self-amusement background scheduler unavailable (%s)", type(error).__name__)
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=self.setting("check_seconds", 30, 5, 300))
            except TimeoutError:
                pass

    @filter.on_llm_request(priority=-110)
    async def observe_request(self, event: AstrMessageEvent, request):
        if event.get_extra("self_amusement") or self.closing or not self.config.get("enabled", True):
            return
        origin = event.unified_msg_origin
        self.link.begin(event)
        self.link.inject(event, request)
        self.chat_started(event)
        proactive = event_kind(event) != "dialogue"
        label = event.get_sender_name()
        if event.get_group_id():
            label = getattr(getattr(event.message_obj, "group", None), "group_name", "") or event.get_group_id()
        target = self.state.targets.get(origin)
        answered = bool(target and target.unanswered > 0)
        if proactive:
            self.state.next_wake = time.time() + self.idle_delay()
            self.state.revision += 1
        else:
            self.state.engage(origin, time.time(), self.idle_delay(), label=label,
                              sender_id=event.get_sender_id(), human=True)
        if origin in self.state.targets and not proactive:
            temporal = event.get_extra("short_term_mood.time_context")
            history = temporal.get("recent_dialogue", []) if isinstance(temporal, dict) else (request.contexts or [])
            self.recent_inputs[origin] = compact_history([*human_dialogue(history)[-7:], {"role": "user", "content": request.prompt or event.get_message_str()}])
            if self.social_enabled():
                event.set_extra("self_amusement_reflection", {
                    "origin": origin, "version": self.state.targets[origin].interaction_version,
                    "persona_id": event.get_extra("_trace_persona_id"),
                    "system_prompt": request.system_prompt or "",
                    "history": list(self.recent_inputs[origin]), "answered_my_initiative": answered,
                })
            prior = self.outbound_hints.get(origin)
            if prior and not event.get_extra("self_amusement_history"):
                request.extra_user_content_parts.append(TextPart(text=prior).mark_as_temp())
                event.set_extra("self_amusement_history", True)
        self.changed()
        self.wake.set()

    @filter.on_llm_response(priority=-90)
    async def observe_response(self, event: AstrMessageEvent, response):
        if event.get_extra("self_amusement") or self.closing:
            return
        if notice_ignored(event) or (event_kind(event) != "dialogue" and not getattr(response, "completion_text", "")):
            self.link.end(event)
            self.chat_finished(event)
            return
        origin = event.unified_msg_origin
        self.link.end(event, response)
        if getattr(response, "tools_call_args", None):
            return
        self.chat_finished(event)
        self.state.engage(origin, time.time(), self.idle_delay(), human=False)
        if origin in self.recent_inputs:
            self.recent_inputs[origin] = compact_history([*self.recent_inputs[origin], {"role": "assistant", "content": response.completion_text or ""}])
        job = event.get_extra("self_amusement_reflection")
        if job and self.social_enabled() and response.completion_text:
            self.reflection_jobs[origin] = {
                **job, "history": compact_history([*job["history"], {"role": "assistant", "content": response.completion_text}]),
                "ready_at": time.monotonic() + self.setting("reflection_merge_seconds", 20, 0, 180),
            }
            self.reflection_wake.set()
        self.changed()
        self.wake.set()

    @filter.on_agent_done(priority=-90)
    async def agent_finished(self, event: AstrMessageEvent, run_context, response):
        self.link.end(event)
        if not event.get_extra("self_amusement") and not self.closing:
            self.chat_finished(event)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("amuse_status")
    async def status(self, event: AstrMessageEvent):
        self.state.refresh_day(time.time())
        delay = max(0, int((self.state.next_wake - time.time()) / 60))
        favorites = sorted(self.state.targets, key=lambda origin: self.attraction(origin, time.time()), reverse=True)
        feelings = []
        for origin in favorites[:3]:
            target = self.state.targets[origin]
            feelings.append(f"{target.label or origin}　{target.social_description()}")
        yield event.plain_result(
            f"自娱自乐　{self.activity}\n约 {delay} 分钟后再看看"
            + ("\n\n最近相处的感觉\n" + "\n".join(feelings) if feelings else "")
            + (f"\n最近异常　{self.last_error}" if self.last_error else "")
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("amuse_social")
    async def social_status(self, event: AstrMessageEvent):
        target = self.state.targets.get(event.unified_msg_origin)
        if not target:
            yield event.plain_result("这个会话还没有加入自娱自乐。可以用 /amuse_here 加入。")
            return
        lines = [target.social_description()]
        if target.shared_interests:
            lines.append("想一起聊的　" + "、".join(target.shared_interests))
        if target.next_topic:
            lines.append("还惦记着　" + target.next_topic)
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("amuse_forget")
    async def forget_social(self, event: AstrMessageEvent):
        target = self.state.targets.get(event.unified_msg_origin)
        if target:
            target.forget()
            self.reflection_jobs.pop(event.unified_msg_origin, None)
            self.state.revision += 1
            await self.persist()
        yield event.plain_result("已放下这个会话先前的相处印象，之后再慢慢认识。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("amuse_here")
    async def add_here(self, event: AstrMessageEvent):
        self.runtime.platform(event.unified_msg_origin)
        self.config["targets"] = list(dict.fromkeys([*self.origins(), event.unified_msg_origin]))
        self.config.save_config()
        self.state.refresh_targets(self.origins())
        self.state.engage(event.unified_msg_origin, time.time(), self.idle_delay(), label=event.get_sender_name(), sender_id=event.get_sender_id())
        self.changed()
        self.wake.set()
        yield event.plain_result("已把当前会话加入自娱自乐，和其他群聊、私聊共用一套配置。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("amuse_remove")
    async def remove_here(self, event: AstrMessageEvent):
        self.config["targets"] = [origin for origin in self.origins() if origin != event.unified_msg_origin]
        self.config.save_config()
        self.state.refresh_targets(self.origins())
        self.state.revision += 1
        self.changed()
        self.wake.set()
        yield event.plain_result("已将当前会话移出自娱自乐。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("amuse_preview")
    async def preview(self, event: AstrMessageEvent):
        if self.preview_worker and not self.preview_worker.done():
            yield event.plain_result("已经有一份自娱预览在后台琢磨，想好后会回到发起它的会话。")
            return
        self.runtime.platform(event.unified_msg_origin)
        if self.activity_task and not self.activity_task.done() and not self.sending:
            self.activity_task.cancel()
        self.preview_worker = asyncio.create_task(self.run_preview(event.unified_msg_origin), name="self-amusement-preview")
        yield event.plain_result("自娱预览已在后台琢磨，想好后会在这里告诉你。可以先继续聊。")

    async def preview_ready(self):
        while not self.background_ready():
            self.preview_wake.clear()
            try:
                await asyncio.wait_for(self.preview_wake.wait(), timeout=1)
            except TimeoutError:
                pass

    async def prepare_preview(self, origin):
        async with self.cycle_lock:
            async with asyncio.timeout(self.setting("generation_timeout_seconds", 60, 5, 300)):
                return await self.prepare(origin)

    async def run_preview(self, origin):
        try:
            while True:
                await self.preview_ready()
                self.preview_task = asyncio.create_task(self.prepare_preview(origin), name="self-amusement-preview-draft")
                try:
                    view, plan, text, _ = await self.preview_task
                    break
                except asyncio.CancelledError:
                    if self.closing or asyncio.current_task().cancelling():
                        raise
                finally:
                    self.preview_task = None
            await self.preview_ready()
            message = f"自娱预览 · {plan['action']}\n{text or plan['private_note'] or '这会儿更想自己待一会儿。'}"
            async with asyncio.timeout(self.setting("send_timeout_seconds", 30, 3, 120)):
                delivered = await self.runtime.deliver(view, message)
            if not delivered:
                self.last_error = "预览发送未确认"
        except Exception as error:
            self.last_error = type(error).__name__
            logger.warning("Self-amusement preview unavailable (%s)", type(error).__name__)
            try:
                async with asyncio.timeout(self.setting("send_timeout_seconds", 30, 3, 120)):
                    await self.context.send_message(origin, MessageChain().message("这次自娱预览没想好，可以稍后再试，或用 /amuse_status 查看状态。"))
            except Exception:
                logger.warning("Self-amusement could not report its preview result")
        finally:
            self.activity = "等待闲下来"

    async def terminate(self):
        if self.closing:
            return
        self.closing = True
        self.link.close()
        tasks = [task for task in (self.worker, self.reflection_worker, self.save_worker, self.activity_task,
                                  self.reflection_task, self.preview_worker, self.preview_task) if task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.persist()
