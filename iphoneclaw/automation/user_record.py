from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

import Quartz

from iphoneclaw.macos.user_input_monitor import IPHONECLAW_EVENT_TAG
from iphoneclaw.types import Rect


_KEYCODE_MAP = {
    0: "a",
    1: "s",
    2: "d",
    3: "f",
    4: "h",
    5: "g",
    6: "z",
    7: "x",
    8: "c",
    9: "v",
    11: "b",
    12: "q",
    13: "w",
    14: "e",
    15: "r",
    16: "y",
    17: "t",
    18: "1",
    19: "2",
    20: "3",
    21: "4",
    22: "6",
    23: "5",
    24: "=",
    25: "9",
    26: "7",
    27: "-",
    28: "8",
    29: "0",
    30: "]",
    31: "o",
    32: "u",
    33: "[",
    34: "i",
    35: "p",
    36: "enter",
    37: "l",
    38: "j",
    39: "'",
    40: "k",
    41: ";",
    42: "\\",
    43: ",",
    44: "/",
    45: "n",
    46: "m",
    47: ".",
    48: "tab",
    49: "space",
    51: "backspace",
    53: "esc",
    123: "left",
    124: "right",
    125: "down",
    126: "up",
}


def _clamp(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, v))


def _event_int(event, field_names: Sequence[str]) -> int:
    for nm in field_names:
        f = getattr(Quartz, nm, None)
        if f is None:
            continue
        try:
            return int(Quartz.CGEventGetIntegerValueField(event, f))
        except Exception:
            continue
    return 0


@dataclass
class _LeftDownState:
    pos: Tuple[float, float]
    at: float
    dragged: bool = False


class LiveUserActionRecorder:
    """
    Capture real user input and convert it to replayable action lines.

    Current mapping:
    - left click -> click(start_box='(...)')
    - left drag -> drag(start_box='(...)', end_box='(...)')
    - right click -> right_single(start_box='(...)')
    - scroll -> scroll(start_box='(...)', direction='up|down|left|right')
    - hotkeys (modifiers + key) -> hotkey(key='cmd shift p')
      - cmd+1 -> iphone_home()
      - cmd+2 -> iphone_app_switcher()
    """

    def __init__(
        self,
        *,
        bounds: Rect,
        coord_factor: int = 1000,
        min_sleep_ms: int = 180,
        max_sleep_ms: int = 2000,
        drag_threshold_px: float = 18.0,
        include_keyboard: bool = True,
    ) -> None:
        self.bounds = bounds
        self.coord_factor = max(100, int(coord_factor))
        self.min_sleep_ms = max(0, int(min_sleep_ms))
        self.max_sleep_ms = max(self.min_sleep_ms, int(max_sleep_ms))
        self.drag_threshold_px = max(1.0, float(drag_threshold_px))
        self.include_keyboard = bool(include_keyboard)

        self._actions: List[str] = []
        self._last_action_ts: Optional[float] = None
        self._last_inside_ts: float = 0.0
        self._left: Optional[_LeftDownState] = None
        self._last_hotkey_sig: str = ""
        self._last_hotkey_ts: float = 0.0

        self._tap = None
        self._source = None
        self._run_loop = None
        self._cb_ref = None
        self._stopped = False

    def _inside(self, pos: Tuple[float, float]) -> bool:
        x, y = pos
        b = self.bounds
        return (b.x <= x <= (b.x + b.width)) and (b.y <= y <= (b.y + b.height))

    def _to_model_xy(self, pos: Tuple[float, float]) -> Tuple[int, int]:
        x, y = pos
        b = self.bounds
        fx = 0.0 if b.width <= 0 else (x - b.x) / b.width
        fy = 0.0 if b.height <= 0 else (y - b.y) / b.height
        mx = _clamp(int(round(fx * self.coord_factor)), 0, self.coord_factor)
        my = _clamp(int(round(fy * self.coord_factor)), 0, self.coord_factor)
        return mx, my

    def _box(self, pos: Tuple[float, float]) -> str:
        mx, my = self._to_model_xy(pos)
        return f"({mx} {my})"

    def _emit(self, line: str, now: float) -> None:
        line = str(line or "").strip()
        if not line:
            return
        if self._last_action_ts is not None:
            gap_ms = int(round((now - self._last_action_ts) * 1000.0))
            if gap_ms >= self.min_sleep_ms:
                self._actions.append(f"sleep(ms={min(gap_ms, self.max_sleep_ms)})")
        self._actions.append(line)
        self._last_action_ts = now

    def _modifiers(self, flags: int) -> List[str]:
        mods: List[str] = []
        if flags & int(getattr(Quartz, "kCGEventFlagMaskCommand", 0)):
            mods.append("cmd")
        if flags & int(getattr(Quartz, "kCGEventFlagMaskControl", 0)):
            mods.append("ctrl")
        if flags & int(getattr(Quartz, "kCGEventFlagMaskAlternate", 0)):
            mods.append("alt")
        if flags & int(getattr(Quartz, "kCGEventFlagMaskShift", 0)):
            mods.append("shift")
        return mods

    def _maybe_emit_hotkey(self, event, now: float) -> None:
        if not self.include_keyboard:
            return
        # Only record keyboard when user recently interacted inside target bounds.
        if (now - self._last_inside_ts) > 2.0:
            return

        keycode = _event_int(event, ("kCGKeyboardEventKeycode",))
        key = _KEYCODE_MAP.get(keycode)
        if not key:
            return

        flags = int(Quartz.CGEventGetFlags(event))
        mods = self._modifiers(flags)
        if not mods:
            return

        seq = " ".join(mods + [key])
        if seq == self._last_hotkey_sig and (now - self._last_hotkey_ts) < 0.2:
            return
        self._last_hotkey_sig = seq
        self._last_hotkey_ts = now

        if seq == "cmd 1":
            self._emit("iphone_home()", now)
            return
        if seq == "cmd 2":
            self._emit("iphone_app_switcher()", now)
            return
        self._emit(f"hotkey(key='{seq}')", now)

    def _on_event(self, type_: int, event) -> None:
        now = time.time()
        try:
            tag = Quartz.CGEventGetIntegerValueField(event, Quartz.kCGEventSourceUserData)
            if int(tag) == int(IPHONECLAW_EVENT_TAG):
                return
        except Exception:
            pass

        pos = None
        try:
            pt = Quartz.CGEventGetLocation(event)
            pos = (float(pt.x), float(pt.y))
        except Exception:
            pos = None

        if pos is not None and self._inside(pos):
            self._last_inside_ts = now

        if type_ == Quartz.kCGEventLeftMouseDown:
            if pos is not None and self._inside(pos):
                self._left = _LeftDownState(pos=pos, at=now, dragged=False)
            return

        if type_ == Quartz.kCGEventLeftMouseDragged:
            if self._left is None or pos is None:
                return
            dx = pos[0] - self._left.pos[0]
            dy = pos[1] - self._left.pos[1]
            if (dx * dx + dy * dy) >= (self.drag_threshold_px * self.drag_threshold_px):
                self._left.dragged = True
            return

        if type_ == Quartz.kCGEventLeftMouseUp:
            if self._left is None:
                return
            start = self._left.pos
            dragged = self._left.dragged
            self._left = None

            if not self._inside(start):
                return
            end = pos if pos is not None else start
            if dragged:
                self._emit(
                    "drag(start_box='%s', end_box='%s')" % (self._box(start), self._box(end)),
                    now,
                )
            else:
                self._emit("click(start_box='%s')" % self._box(start), now)
            return

        if type_ == Quartz.kCGEventRightMouseDown:
            if pos is not None and self._inside(pos):
                self._emit("right_single(start_box='%s')" % self._box(pos), now)
            return

        if type_ == Quartz.kCGEventScrollWheel:
            if pos is None or not self._inside(pos):
                return
            dy = _event_int(
                event,
                (
                    "kCGScrollWheelEventPointDeltaAxis1",
                    "kCGScrollWheelEventDeltaAxis1",
                ),
            )
            dx = _event_int(
                event,
                (
                    "kCGScrollWheelEventPointDeltaAxis2",
                    "kCGScrollWheelEventDeltaAxis2",
                ),
            )
            if abs(dx) > abs(dy):
                if dx == 0:
                    return
                direction = "right" if dx > 0 else "left"
            else:
                if dy == 0:
                    return
                direction = "up" if dy > 0 else "down"
            self._emit(
                "scroll(start_box='%s', direction='%s')" % (self._box(pos), direction),
                now,
            )
            return

        if type_ == Quartz.kCGEventKeyDown:
            self._maybe_emit_hotkey(event, now)
            return

    def stop(self) -> None:
        self._stopped = True
        try:
            if self._run_loop is not None:
                Quartz.CFRunLoopStop(self._run_loop)
        except Exception:
            pass

    def _compact_actions(self) -> List[str]:
        compact: List[str] = []
        for ln in self._actions:
            if compact and ln.startswith("scroll(") and ln == compact[-1]:
                continue
            compact.append(ln)
        return compact

    def record(self, *, seconds: float = 0.0) -> List[str]:
        mask = 0
        listen = [
            Quartz.kCGEventLeftMouseDown,
            Quartz.kCGEventLeftMouseUp,
            Quartz.kCGEventLeftMouseDragged,
            Quartz.kCGEventRightMouseDown,
            Quartz.kCGEventScrollWheel,
            Quartz.kCGEventKeyDown,
        ]
        for t in listen:
            mask |= Quartz.CGEventMaskBit(t)

        def cb(_proxy, type_, event, _refcon):  # noqa: ANN001
            if self._stopped:
                return event
            try:
                self._on_event(int(type_), event)
            except Exception:
                # Keep recording even if one event parse fails.
                pass
            return event

        self._cb_ref = cb
        tap = Quartz.CGEventTapCreate(
            Quartz.kCGSessionEventTap,
            Quartz.kCGHeadInsertEventTap,
            Quartz.kCGEventTapOptionListenOnly,
            mask,
            cb,
            None,
        )
        self._tap = tap
        if tap is None:
            raise RuntimeError(
                "Failed to create CGEvent tap. Check Accessibility permission for your terminal."
            )

        source = Quartz.CFMachPortCreateRunLoopSource(None, tap, 0)
        self._source = source
        rl = Quartz.CFRunLoopGetCurrent()
        self._run_loop = rl
        Quartz.CFRunLoopAddSource(rl, source, Quartz.kCFRunLoopCommonModes)
        Quartz.CGEventTapEnable(tap, True)

        timer: Optional[threading.Timer] = None
        if float(seconds) > 0:
            timer = threading.Timer(float(seconds), self.stop)
            timer.daemon = True
            timer.start()

        try:
            Quartz.CFRunLoopRun()
        except KeyboardInterrupt:
            self.stop()
        finally:
            self._stopped = True
            if timer is not None:
                timer.cancel()
            try:
                if self._source is not None and self._run_loop is not None:
                    Quartz.CFRunLoopRemoveSource(
                        self._run_loop, self._source, Quartz.kCFRunLoopCommonModes
                    )
            except Exception:
                pass

        return self._compact_actions()
