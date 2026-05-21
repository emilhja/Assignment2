"""Rate-limit + token cap for Part 3 LLM calls.

`Budget.permit(estimated_tokens)` is called before each outbound LLM request;
it raises `BudgetExceeded` if any cap would be crossed or if `paused`.
`Budget.record(actual_tokens)` is called after the request returns.

Limits are mutated at runtime by `console_control` via `set_limit(name, value)`
and `pause()`/`resume()`. State is JSON-persisted so the lifetime token counter
survives restarts (the sliding-window state is not — a fresh process starts a
fresh minute).
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


LIMIT_NAMES = frozenset({"tpm", "rpm", "total"})


class BudgetExceeded(RuntimeError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass
class Budget:
    tokens_per_minute: int = 20_000
    requests_per_minute: int = 30
    lifetime_tokens: int = 200_000
    lifetime_tokens_used: int = 0
    paused: bool = False
    persist_path: Optional[Path] = None
    _events: deque = field(default_factory=deque)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @classmethod
    def load(cls, path: Path | str, **defaults) -> "Budget":
        path = Path(path)
        budget = cls(persist_path=path, **defaults)
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return budget
            budget.tokens_per_minute = int(data.get("tokens_per_minute", budget.tokens_per_minute))
            budget.requests_per_minute = int(data.get("requests_per_minute", budget.requests_per_minute))
            budget.lifetime_tokens = int(data.get("lifetime_tokens", budget.lifetime_tokens))
            budget.lifetime_tokens_used = int(data.get("lifetime_tokens_used", 0))
            budget.paused = bool(data.get("paused", False))
        return budget

    def save(self) -> None:
        if self.persist_path is None:
            return
        with self._lock:
            payload = {
                "tokens_per_minute": self.tokens_per_minute,
                "requests_per_minute": self.requests_per_minute,
                "lifetime_tokens": self.lifetime_tokens,
                "lifetime_tokens_used": self.lifetime_tokens_used,
                "paused": self.paused,
            }
        self.persist_path.parent.mkdir(parents=True, exist_ok=True)
        self.persist_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _evict_old(self, now: float) -> None:
        cutoff = now - 60.0
        while self._events and self._events[0][0] < cutoff:
            self._events.popleft()

    def _window_totals(self) -> tuple[int, int]:
        tokens = sum(e[1] for e in self._events)
        requests = sum(e[2] for e in self._events)
        return tokens, requests

    def permit(self, estimated_tokens: int, *, now: Optional[float] = None) -> None:
        if estimated_tokens < 0:
            raise ValueError("estimated_tokens must be non-negative")
        now = time.time() if now is None else now
        with self._lock:
            if self.paused:
                raise BudgetExceeded("budget is paused")
            self._evict_old(now)
            tokens_used, requests_used = self._window_totals()
            if tokens_used + estimated_tokens > self.tokens_per_minute:
                raise BudgetExceeded(
                    f"would exceed tokens-per-minute "
                    f"({tokens_used} + {estimated_tokens} > {self.tokens_per_minute})"
                )
            if requests_used + 1 > self.requests_per_minute:
                raise BudgetExceeded(
                    f"would exceed requests-per-minute ({requests_used + 1} > {self.requests_per_minute})"
                )
            if self.lifetime_tokens_used + estimated_tokens > self.lifetime_tokens:
                raise BudgetExceeded(
                    f"would exceed lifetime token cap "
                    f"({self.lifetime_tokens_used} + {estimated_tokens} > {self.lifetime_tokens})"
                )

    def record(self, actual_tokens: int, *, now: Optional[float] = None) -> None:
        if actual_tokens < 0:
            raise ValueError("actual_tokens must be non-negative")
        now = time.time() if now is None else now
        with self._lock:
            self._evict_old(now)
            self._events.append((now, actual_tokens, 1))
            self.lifetime_tokens_used += actual_tokens

    def set_limit(self, name: str, value: int) -> None:
        if name not in LIMIT_NAMES:
            raise ValueError(f"unknown limit name: {name} (use one of {sorted(LIMIT_NAMES)})")
        if value < 0:
            raise ValueError("limit must be non-negative")
        with self._lock:
            if name == "tpm":
                self.tokens_per_minute = value
            elif name == "rpm":
                self.requests_per_minute = value
            elif name == "total":
                self.lifetime_tokens = value

    def pause(self) -> None:
        with self._lock:
            self.paused = True

    def resume(self) -> None:
        with self._lock:
            self.paused = False

    def snapshot(self) -> dict:
        now = time.time()
        with self._lock:
            self._evict_old(now)
            tokens_used, requests_used = self._window_totals()
            return {
                "paused": self.paused,
                "tokens_per_minute": self.tokens_per_minute,
                "requests_per_minute": self.requests_per_minute,
                "lifetime_tokens": self.lifetime_tokens,
                "tokens_used_last_minute": tokens_used,
                "requests_used_last_minute": requests_used,
                "lifetime_tokens_used": self.lifetime_tokens_used,
            }


def estimate_tokens(text: str) -> int:
    """Crude character/4 estimate. Good enough for budget gating."""

    return max(1, len(text) // 4)
