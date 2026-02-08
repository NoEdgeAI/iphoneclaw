from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class ConversationItem:
    role: str  # "system" | "user" | "assistant"
    text: str
    ts: float = 0.0
    meta: Dict[str, Any] = field(default_factory=dict)


class ConversationStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: List[ConversationItem] = []

    def add(self, role: str, text: str, **meta: Any) -> None:
        with self._lock:
            self._items.append(
                ConversationItem(role=role, text=text, ts=time.time(), meta=dict(meta))
            )

    def items(self) -> List[ConversationItem]:
        with self._lock:
            return list(self._items)

    def to_openai_messages(
        self, *, include_system: bool = True, tail_rounds: int = 5
    ) -> List[Dict[str, Any]]:
        """
        Convert to OpenAI-compatible message list (text only).
        'tail_rounds' counts assistant turns.
        """
        items = self._tail_items_by_rounds(tail_rounds)
        out: List[Dict[str, Any]] = []
        for it in items:
            if it.role == "system" and not include_system:
                continue
            out.append({"role": it.role, "content": it.text})
        return out

    def _tail_items_by_rounds(self, tail_rounds: int) -> List[ConversationItem]:
        with self._lock:
            items = list(self._items)

        assistant_seen = 0
        start_idx = 0
        for i in range(len(items) - 1, -1, -1):
            if items[i].role == "assistant":
                assistant_seen += 1
                if assistant_seen >= tail_rounds:
                    start_idx = i
                    break
        # Ensure we include the user prompt that triggered the first assistant turn.
        if start_idx > 0 and items[start_idx].role == "assistant":
            if items[start_idx - 1].role == "user":
                start_idx -= 1
        return items[start_idx:] if items else []

    def tail_rounds(self, tail_rounds: int) -> List[Dict[str, Any]]:
        sliced = self._tail_items_by_rounds(tail_rounds)
        return [
            {
                "role": it.role,
                "text": it.text,
                "ts": it.ts,
                "meta": it.meta,
            }
            for it in sliced
        ]

    def clear(self, *, keep_last_system: bool = True) -> int:
        """
        Clear conversation history.

        If keep_last_system is True, preserve the most recent system message so the worker
        still has its base rules when the next step builds messages.
        Returns the number of items removed.
        """
        with self._lock:
            before = len(self._items)
            if not self._items:
                return 0
            if not keep_last_system:
                self._items.clear()
                return before
            # Keep the last system message if present; otherwise clear all.
            last_sys = None
            for it in reversed(self._items):
                if it.role == "system":
                    last_sys = it
                    break
            self._items = [last_sys] if last_sys is not None else []
            return before - len(self._items)

    def trim_tail_rounds(self, drop_rounds: int) -> int:
        """
        Drop the most recent N assistant rounds (and the user message right before each, if present).
        Returns the number of items removed.
        """
        n = int(drop_rounds)
        if n <= 0:
            return 0
        with self._lock:
            before = len(self._items)
            if before == 0:
                return 0

            assistant_dropped = 0
            # Walk backwards and remove messages belonging to the last N assistant rounds.
            i = len(self._items) - 1
            while i >= 0 and assistant_dropped < n:
                if self._items[i].role == "assistant":
                    # Remove this assistant message.
                    del self._items[i]
                    # Also remove the triggering user message immediately before, if it exists.
                    if i - 1 >= 0 and self._items[i - 1].role == "user":
                        del self._items[i - 1]
                        i -= 1
                    assistant_dropped += 1
                i -= 1
            return before - len(self._items)
