from __future__ import annotations

import queue
import threading
import time
from collections import Counter, deque
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Callable
from urllib.parse import urlsplit

from .utils import sanitize_url_for_log


HEYBOX_HOSTS = {"xiaoheihe.cn", "www.xiaoheihe.cn", "api.xiaoheihe.cn"}
RATE_LIMIT_MARKERS = ("你的操作过于频繁", "请稍后再试")
CAPTCHA_MARKERS = ("验证码", "安全验证", "完成验证")
REQUEST_SOURCES = ("page_native", "exporter_fetch", "login_check", "captcha_check", "retry")


class RequestState(str, Enum):
    RUNNING = "RUNNING"
    CAPTCHA_REQUIRED = "CAPTCHA_REQUIRED"
    RATE_LIMITED = "RATE_LIMITED"


class UserAction(str, Enum):
    CAPTCHA_COMPLETED = "captcha_completed"
    RETRY = "retry"
    CANCEL = "cancel"


class InteractionRequiredError(RuntimeError):
    pass


@dataclass(frozen=True)
class RequestRecord:
    monotonic_time: float
    wall_time: datetime
    url: str
    endpoint: str
    source: str


def is_heybox_url(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return host in HEYBOX_HOSTS or host.endswith(".xiaoheihe.cn")


def rate_limit_message(*values: object) -> str:
    text = " ".join(str(value or "") for value in values)
    lowered = text.lower()
    if (
        any(value == 429 or str(value).strip() == "429" for value in values)
        or
        "你的操作过于频繁" in text
        or "操作过于频繁" in text
        or "操作频繁" in text
        or "请稍后再试" in text
        or "too many requests" in lowered
        or "rate_limit" in lowered
        or "rate limited" in lowered
    ):
        return text.strip()
    return ""


class RequestAudit:
    def __init__(self) -> None:
        self._records: deque[RequestRecord] = deque()
        self._counts: Counter[str] = Counter()
        self._lock = threading.Lock()
        self._captcha_total: int | None = None
        self._blocked_attempts: list[RequestRecord] = []

    def record(self, url: str, source: str) -> RequestRecord:
        now = time.monotonic()
        record = RequestRecord(
            monotonic_time=now,
            wall_time=datetime.now().astimezone(),
            url=sanitize_url_for_log(url),
            endpoint=urlsplit(url).path or "/",
            source=source if source in REQUEST_SOURCES else "page_native",
        )
        with self._lock:
            self._records.append(record)
            self._counts[record.source] += 1
        return record

    def record_blocked_attempt(self, url: str, source: str) -> None:
        record = RequestRecord(
            monotonic_time=time.monotonic(),
            wall_time=datetime.now().astimezone(),
            url=sanitize_url_for_log(url),
            endpoint=urlsplit(url).path or "/",
            source=source if source in REQUEST_SOURCES else "page_native",
        )
        with self._lock:
            self._blocked_attempts.append(record)

    def mark_captcha(self) -> None:
        with self._lock:
            if self._captcha_total is None:
                self._captcha_total = len(self._records)

    def snapshot(self) -> dict[str, object]:
        now = time.monotonic()
        with self._lock:
            records = list(self._records)
            counts = dict(self._counts)
            captcha_total = self._captcha_total
            blocked = list(self._blocked_attempts)
        recent = lambda seconds: sum(1 for item in records if now - item.monotonic_time <= seconds)
        return {
            "total_requests": len(records),
            "requests_last_10s": recent(10),
            "requests_last_30s": recent(30),
            "requests_last_60s": recent(60),
            "by_source": {source: counts.get(source, 0) for source in REQUEST_SOURCES},
            "last_request": records[-1] if records else None,
            "requests_after_captcha": 0 if captcha_total is None else len(records) - captcha_total,
            "records_after_captcha": [] if captcha_total is None else records[captcha_total:],
            "records": records,
            "blocked_attempts": blocked,
        }

    def as_dict(self) -> dict[str, object]:
        snapshot = self.snapshot()
        last = snapshot["last_request"]
        blocked = snapshot["blocked_attempts"]
        records = snapshot["records"]
        records_after = snapshot["records_after_captcha"]
        serialize = lambda item: {
            "time": item.wall_time.isoformat(timespec="milliseconds"),
            "url": item.url,
            "endpoint": item.endpoint,
            "source": item.source,
        }
        return {
            "total_requests": snapshot["total_requests"],
            "requests_last_10s": snapshot["requests_last_10s"],
            "requests_last_30s": snapshot["requests_last_30s"],
            "requests_last_60s": snapshot["requests_last_60s"],
            "by_source": snapshot["by_source"],
            "requests_after_captcha": snapshot["requests_after_captcha"],
            "last_request": None if last is None else serialize(last),
            "requests": [serialize(item) for item in records],
            "requests_after_captcha_records": [serialize(item) for item in records_after],
            "blocked_attempts": [serialize(item) for item in blocked],
        }


StateListener = Callable[[RequestState, str], None]


class RequestControl:
    """Thread-safe user-controlled gate. It has no timers or automatic wakeups."""

    def __init__(self, listener: StateListener | None = None) -> None:
        self.audit = RequestAudit()
        self._listener = listener
        self._state = RequestState.RUNNING
        self._message = ""
        self._lock = threading.Lock()
        self._actions: queue.Queue[UserAction] = queue.Queue()
        self._cancelled = threading.Event()

    @property
    def state(self) -> RequestState:
        with self._lock:
            return self._state

    @property
    def message(self) -> str:
        with self._lock:
            return self._message

    @property
    def is_blocked(self) -> bool:
        return self.state in {RequestState.CAPTCHA_REQUIRED, RequestState.RATE_LIMITED}

    @property
    def is_cancelled(self) -> bool:
        return self._cancelled.is_set()

    def transition(self, state: RequestState, message: str = "") -> bool:
        with self._lock:
            changed = state != self._state or (message and message != self._message)
            self._state = state
            self._message = message
        if state in {RequestState.CAPTCHA_REQUIRED, RequestState.RATE_LIMITED}:
            self.audit.mark_captcha()
        if changed and self._listener:
            self._listener(state, message)
        return changed

    def submit_captcha_completed(self) -> bool:
        if self.state != RequestState.CAPTCHA_REQUIRED:
            return False
        self._actions.put(UserAction.CAPTCHA_COMPLETED)
        return True

    def submit_retry(self) -> bool:
        if self.state != RequestState.RATE_LIMITED:
            return False
        self._actions.put(UserAction.RETRY)
        return True

    def cancel(self) -> None:
        self._cancelled.set()
        self._actions.put(UserAction.CANCEL)

    def wait_for_user_action(self) -> UserAction:
        return self._actions.get()
