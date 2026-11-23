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
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.agent.message import TextPart

from .prompts import MESSAGE_SYSTEM, PLAN_SYSTEM, finish_text, parse_plan
from .runtime import Runtime, compact_history
from .state import LeisureState, StateStore, TargetState, number, quiet_now
from .web import Browser


class SelfAmusement(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.store = StateStore(Path(StarTools.get_data_dir("astrbot_plugin_self_amusement")))
        try:
            self.state = self.store.load()
        except (ValueError, TypeError, AttributeError):
            logger.warning("Self-amusement could not read its previous state, starting with a fresh idle period")
            self.state = LeisureState()
        self.runtime = Runtime(context)
        self.browser = Browser(context)
        self.cycle_lock = asyncio.Lock()
        self.save_lock = asyncio.Lock()
        self.wake = asyncio.Event()
        self.worker = None
        self.closing = False
        self.dirty = False
        self.recent_inputs = {}
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

    async def initialize(self):
        self.state.refresh_targets(self.origins())
        self.state.initialize(time.time(), self.idle_delay())
        await self.persist()
        self.worker = asyncio.create_task(self.run(), name="self-amusement-leisure")

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

    def eligible(self, now):
        self.state.refresh_targets(self.origins())
        return self.state.eligible(
            now, global_gap=self.setting("global_gap_minutes", 20, 0, 1440) * 60,
            target_gap=self.setting("target_gap_minutes", 60, 0, 10080) * 60,
            unanswered_limit=int(self.setting("max_unanswered", 2, 0, 20)),
            backoff=self.setting("unanswered_backoff_minutes", 180, 1, 10080) * 60,
            daily_limit=int(self.setting("daily_limit", 12, 0, 1000)),
        )

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
        actions = [value for value in self.config.get("activities", ["chat", "mutter", "share", "browse", "wait"])
                   if value in {"chat", "mutter", "share", "browse", "wait"}]
        if not self.config.get("browse_enabled", True):
            actions = [value for value in actions if value not in {"share", "browse"}]
        actions = list(dict.fromkeys([*actions, "wait"]))
        recent = [item for item in self.state.recent if item.get("origin") == origin][-5:]
        payload = {
            "persona": view.persona, "mood": self.runtime.mood(),
            "recent_dialogue": view.history, "recent_actions": recent,
            "leisure_thought": self.state.thought, "interests": self.config.get("interests", []),
            "idle_minutes": round(max(0, time.time() - self.state.last_engaged) / 60, 1),
            "unanswered": target.unanswered, "allowed_actions": actions,
            "current_time": datetime.now().astimezone().isoformat(timespec="minutes"),
            "preferences": str(self.config.get("behavior_preferences", "") or "")[:2000],
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
            if plan["action"] == "browse":
                plan["private_note"] = "想接着看看：" + sources["results"][0]["title"][:100]
            else:
                since = time.time() - self.setting("repeat_window_hours", 24, 1, 168) * 3600
                used = "\n".join(item.get("text", "") for item in self.state.recent if item.get("time", 0) >= since)
                sources["results"] = [entry for entry in sources["results"] if entry["url"] not in used][:1]
                if not sources["results"]:
                    return view, plan, "", sources
        if plan["action"] in {"wait", "browse"}:
            return view, plan, "", sources
        self.activity = "想好怎么开口"
        material = {"action": plan["action"], "idea": plan["idea"], "sources": sources,
                    "recent_own_messages": [item.get("text", "") for item in recent],
                    "current_time": payload["current_time"],
                    "preferences": payload["preferences"], "max_characters": int(self.setting("message_length", 220, 40, 1200))}
        text = await self.runtime.draft(view, json.dumps(material, ensure_ascii=False), MESSAGE_SYSTEM)
        source = sources["results"][0]["url"] if plan["action"] == "share" and sources["results"] else ""
        text = finish_text(text, int(self.setting("message_length", 220, 40, 1200)), source)
        return view, plan, text, sources

    def repetitive(self, origin, text, now):
        since = now - self.setting("repeat_window_hours", 24, 1, 168) * 3600
        for item in self.state.recent:
            if item.get("origin") == origin and item.get("time", 0) >= since and item.get("status") in {"sent", "unconfirmed"}:
                if SequenceMatcher(None, item.get("text", ""), text).ratio() >= 0.88:
                    return True
        return False

    async def cycle(self):
        now = time.time()
        if self.closing or not self.config.get("enabled", True) or now < self.state.next_wake:
            return
        if quiet_now(str(self.config.get("quiet_hours", "00:00-08:00") or ""), datetime.fromtimestamp(now)):
            self.activity = "安静时段，自己待着"
            return
        candidates = []
        for origin in self.eligible(now):
            if self.permitted(origin) and await self.runtime.enabled(origin):
                candidates.append(origin)
        if not candidates:
            self.activity = "暂时没有适合搭话的会话"
            return
        if self.cycle_lock.locked():
            return
        async with self.cycle_lock:
            weights = [min(5, 1 + (now - self.state.targets[origin].last_sent) / 3600) /
                       (1 + self.state.targets[origin].unanswered) for origin in candidates]
            origin = random.choices(candidates, weights=weights)[0]
            revision = self.state.revision
            self.state.next_wake = now + self.idle_delay()
            await self.persist()
            async with asyncio.timeout(self.setting("generation_timeout_seconds", 60, 5, 300)):
                view, plan, text, sources = await self.prepare(origin)
            if self.closing or revision != self.state.revision or not self.config.get("enabled", True):
                return
            self.state.thought = plan["private_note"]
            delay = min(plan["wait_minutes"] * 60, self.setting("idle_max_minutes", 45, 1, 1440) * 60)
            self.state.next_wake = time.time() + delay
            self.activity = {"wait": "继续发会儿呆", "browse": "逛了一会儿，先记在心里"}.get(plan["action"], "准备开口")
            if not text:
                self.state.record(origin, plan["action"], "", time.time(), "private")
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
            if revision != self.state.revision or self.closing or origin not in self.origins() or not self.config.get("enabled", True):
                self.state.pending = None
                await self.persist()
                return
            self.activity = "发出一点闲话"
            async with asyncio.timeout(self.setting("send_timeout_seconds", 30, 3, 120)):
                delivered = await self.runtime.deliver(view, text)
            self.state.record(origin, plan["action"], text, time.time(), "sent" if delivered else "unconfirmed")
            self.activity = "刚聊了一句，等一会儿"
            await self.persist()
            self.last_error = ""

    async def run(self):
        while True:
            try:
                if self.dirty:
                    await self.persist()
                await self.cycle()
            except Exception as error:
                self.last_error = type(error).__name__
                self.activity = "这次没想好，稍后再试"
                if self.state.pending:
                    pending = self.state.pending
                    self.state.record(pending["origin"], "attempt", pending["text"], pending["time"], "unconfirmed")
                self.state.next_wake = time.time() + self.setting("retry_minutes", 10, 1, 240) * 60
                logger.warning("Self-amusement activity unavailable (%s)", type(error).__name__)
                try:
                    await self.persist()
                except Exception:
                    logger.warning("Self-amusement could not save its current state")
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=self.setting("check_seconds", 30, 5, 300))
            except TimeoutError:
                pass
            self.wake.clear()

    @filter.on_llm_request(priority=90)
    async def observe_request(self, event: AstrMessageEvent, request):
        if event.get_extra("self_amusement") or self.closing:
            return
        origin = event.unified_msg_origin
        self.state.refresh_targets(self.origins())
        proactive = str(getattr(event.message_obj, "message_id", "")).startswith("proactive:")
        label = event.get_sender_name()
        if event.get_group_id():
            label = getattr(getattr(event.message_obj, "group", None), "group_name", "") or event.get_group_id()
        self.state.engage(origin, time.time(), self.idle_delay(), label=label,
                          sender_id=event.get_sender_id(), human=not proactive)
        if origin in self.state.targets and not proactive:
            self.recent_inputs[origin] = compact_history([*(request.contexts or []), {"role": "user", "content": request.prompt or event.get_message_str()}])
            prior = [item for item in self.state.recent if item.get("origin") == origin and item.get("status") == "sent" and item.get("text")][-3:]
            if prior and not event.get_extra("self_amusement_history"):
                request.extra_user_content_parts.append(TextPart(text="你在这个会话最近主动说过的话，供接续当前交流：\n" + "\n".join(item["text"] for item in prior)).mark_as_temp())
                event.set_extra("self_amusement_history", True)
        self.dirty = True
        self.wake.set()

    @filter.on_llm_response(priority=-90)
    async def observe_response(self, event: AstrMessageEvent, response):
        if event.get_extra("self_amusement") or self.closing:
            return
        origin = event.unified_msg_origin
        self.state.engage(origin, time.time(), self.idle_delay(), human=False)
        if origin in self.recent_inputs:
            self.recent_inputs[origin] = compact_history([*self.recent_inputs[origin], {"role": "assistant", "content": response.completion_text or ""}])
        self.dirty = True
        self.wake.set()

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("amuse_status")
    async def status(self, event: AstrMessageEvent):
        self.state.refresh_day(time.time())
        delay = max(0, int((self.state.next_wake - time.time()) / 60))
        yield event.plain_result(f"自娱自乐　{self.activity}\n统一目标 {len(self.origins())} 个，今天主动发起 {self.state.sent_today} 次\n约 {delay} 分钟后再看看\n闲念　{self.state.thought or '还没想好玩什么'}" + (f"\n最近异常　{self.last_error}" if self.last_error else ""))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("amuse_here")
    async def add_here(self, event: AstrMessageEvent):
        self.runtime.platform(event.unified_msg_origin)
        self.config["targets"] = list(dict.fromkeys([*self.origins(), event.unified_msg_origin]))
        self.config.save_config()
        self.state.refresh_targets(self.origins())
        self.state.engage(event.unified_msg_origin, time.time(), self.idle_delay(), label=event.get_sender_name(), sender_id=event.get_sender_id())
        self.dirty = True
        self.wake.set()
        yield event.plain_result("已把当前会话加入自娱自乐，和其他群聊、私聊共用一套配置。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("amuse_remove")
    async def remove_here(self, event: AstrMessageEvent):
        self.config["targets"] = [origin for origin in self.origins() if origin != event.unified_msg_origin]
        self.config.save_config()
        self.state.refresh_targets(self.origins())
        self.state.revision += 1
        self.dirty = True
        self.wake.set()
        yield event.plain_result("已将当前会话移出自娱自乐。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("amuse_preview")
    async def preview(self, event: AstrMessageEvent):
        if self.cycle_lock.locked():
            yield event.plain_result("正在琢磨另一点事情，过一会儿再预览。")
            return
        async with self.cycle_lock:
            try:
                async with asyncio.timeout(self.setting("generation_timeout_seconds", 60, 5, 300)):
                    _, plan, text, sources = await self.prepare(event.unified_msg_origin)
                yield event.plain_result(f"自娱预览 · {plan['action']}\n{text or plan['private_note'] or '这会儿更想自己待一会儿。'}")
            finally:
                self.activity = "等待闲下来"

    async def terminate(self):
        if self.closing:
            return
        self.closing = True
        if self.worker:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
        await self.persist()
