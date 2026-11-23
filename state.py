"""One shared leisure rhythm for all configured conversations."""

from __future__ import annotations

import json
import math
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


def number(value, default, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return default
    return min(maximum, max(minimum, float(value)))


def quiet_now(spec: str, current: datetime) -> bool:
    if not spec.strip():
        return False
    for window in spec.split(","):
        start, end = window.strip().split("-")
        sh, sm = (int(value) for value in start.split(":"))
        eh, em = (int(value) for value in end.split(":"))
        if not (0 <= sh < 24 and 0 <= eh < 24 and 0 <= sm < 60 and 0 <= em < 60):
            raise ValueError("Invalid quiet time")
        minute = current.hour * 60 + current.minute
        begin, finish = sh * 60 + sm, eh * 60 + em
        if begin == finish or (begin <= minute < finish if begin < finish else minute >= begin or minute < finish):
            return True
    return False


@dataclass
class TargetState:
    last_received: float = 0
    last_sent: float = 0
    unanswered: int = 0
    label: str = ""
    sender_id: str = ""


@dataclass
class LeisureState:
    last_engaged: float = 0
    next_wake: float = 0
    last_outbound: float = 0
    revision: int = 0
    day: str = ""
    sent_today: int = 0
    thought: str = ""
    recent: list[dict] = field(default_factory=list)
    targets: dict[str, TargetState] = field(default_factory=dict)
    pending: dict | None = None

    @classmethod
    def from_dict(cls, data: dict):
        state = cls(
            last_engaged=number(data.get("last_engaged"), 0, 0, 1e12),
            next_wake=number(data.get("next_wake"), 0, 0, 1e12),
            last_outbound=number(data.get("last_outbound"), 0, 0, 1e12),
            revision=int(number(data.get("revision"), 0, 0, 1e12)),
            day=str(data.get("day", "")),
            sent_today=int(number(data.get("sent_today"), 0, 0, 1e6)),
            thought=str(data.get("thought", ""))[:300],
            recent=[item for item in data.get("recent", [])[-12:] if isinstance(item, dict)],
            pending=data.get("pending") if isinstance(data.get("pending"), dict) else None,
        )
        for origin, target in data.get("targets", {}).items():
            if isinstance(target, dict):
                state.targets[origin] = TargetState(
                    last_received=number(target.get("last_received"), 0, 0, 1e12),
                    last_sent=number(target.get("last_sent"), 0, 0, 1e12),
                    unanswered=int(number(target.get("unanswered"), 0, 0, 1e6)),
                    label=str(target.get("label", ""))[:100],
                    sender_id=str(target.get("sender_id", ""))[:200],
                )
        return state

    def initialize(self, now: float, delay: float):
        if not self.last_engaged:
            self.last_engaged = now
        if not self.next_wake:
            self.next_wake = now + delay
        if self.pending:
            self.last_outbound = max(self.last_outbound, self.pending.get("time", now))
            self.pending = None
            self.next_wake = max(self.next_wake, now + delay)

    def engage(self, origin: str, now: float, delay: float, *, label="", sender_id="", human=True):
        self.last_engaged = now
        self.next_wake = now + delay
        self.revision += 1
        if origin in self.targets:
            target = self.targets[origin]
            if human:
                target.last_received = now
                target.unanswered = 0
                target.label = label or target.label
                target.sender_id = sender_id or target.sender_id
            else:
                target.last_sent = now
        if not human:
            self.last_outbound = now

    def refresh_targets(self, origins: list[str]):
        self.targets = {origin: self.targets.get(origin, TargetState()) for origin in origins}

    def refresh_day(self, now: float):
        today = datetime.fromtimestamp(now).date().isoformat()
        if self.day != today:
            self.day = today
            self.sent_today = 0

    def eligible(self, now: float, *, global_gap: float, target_gap: float, unanswered_limit: int, backoff: float, daily_limit: int):
        self.refresh_day(now)
        if now - self.last_outbound < global_gap or (daily_limit and self.sent_today >= daily_limit):
            return []
        return [origin for origin, target in self.targets.items()
                if now - target.last_sent >= target_gap
                and (not unanswered_limit or target.unanswered < unanswered_limit or now - target.last_sent >= backoff)]

    def reserve(self, origin: str, text: str, now: float):
        self.refresh_day(now)
        target = self.targets[origin]
        target.last_sent = now
        target.unanswered += 1
        self.last_outbound = now
        self.sent_today += 1
        self.pending = {"origin": origin, "text": text, "time": now}

    def record(self, origin: str, action: str, text: str, now: float, status: str):
        self.pending = None
        self.recent.append({"origin": origin, "action": action, "text": text[:800], "time": now, "status": status})
        self.recent = self.recent[-12:]


class StateStore:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "leisure.json"

    def load(self) -> LeisureState:
        if not self.path.exists():
            return LeisureState()
        return LeisureState.from_dict(json.loads(self.path.read_text(encoding="utf-8")))

    def save(self, payload: dict):
        descriptor, temporary = tempfile.mkstemp(prefix="leisure-", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
