"""Mouse input via Quartz CGEvent."""

from __future__ import annotations

import time

import Quartz

from iphoneclaw.macos.user_input_monitor import IPHONECLAW_EVENT_TAG


def _post(event) -> None:
    # Mark event as "agent-injected" so UserInputMonitor can ignore it.
    try:
        Quartz.CGEventSetIntegerValueField(
            event, Quartz.kCGEventSourceUserData, int(IPHONECLAW_EVENT_TAG)
        )
    except Exception:
        pass
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)

def mouse_position() -> tuple[float, float]:
    """
    Current global mouse cursor position.

    Note: macOS has a single system cursor; all CGEvent mouse injection will move it.
    """
    evt = Quartz.CGEventCreate(None)
    pt = Quartz.CGEventGetLocation(evt)
    return float(pt.x), float(pt.y)


def mouse_move(x: float, y: float) -> None:
    """Move the mouse cursor to (x, y) in global screen coordinates."""
    event = Quartz.CGEventCreateMouseEvent(
        None, Quartz.kCGEventMouseMoved, (x, y), Quartz.kCGMouseButtonLeft
    )
    _post(event)


def mouse_click(x: float, y: float, button: str = "left") -> None:
    """Click at (x, y). button: 'left', 'right', 'middle'."""
    btn_map = {
        "left": (
            Quartz.kCGMouseButtonLeft,
            Quartz.kCGEventLeftMouseDown,
            Quartz.kCGEventLeftMouseUp,
        ),
        "right": (
            Quartz.kCGMouseButtonRight,
            Quartz.kCGEventRightMouseDown,
            Quartz.kCGEventRightMouseUp,
        ),
    }
    btn, down_type, up_type = btn_map.get(button, btn_map["left"])
    point = (x, y)

    down = Quartz.CGEventCreateMouseEvent(None, down_type, point, btn)
    up = Quartz.CGEventCreateMouseEvent(None, up_type, point, btn)
    _post(down)
    time.sleep(0.05)
    _post(up)


def mouse_double_click(x: float, y: float) -> None:
    """Double-click at (x, y)."""
    point = (x, y)
    btn = Quartz.kCGMouseButtonLeft

    down1 = Quartz.CGEventCreateMouseEvent(
        None, Quartz.kCGEventLeftMouseDown, point, btn
    )
    up1 = Quartz.CGEventCreateMouseEvent(
        None, Quartz.kCGEventLeftMouseUp, point, btn
    )
    down2 = Quartz.CGEventCreateMouseEvent(
        None, Quartz.kCGEventLeftMouseDown, point, btn
    )
    Quartz.CGEventSetIntegerValueField(
        down2, Quartz.kCGMouseEventClickState, 2
    )
    up2 = Quartz.CGEventCreateMouseEvent(
        None, Quartz.kCGEventLeftMouseUp, point, btn
    )
    Quartz.CGEventSetIntegerValueField(
        up2, Quartz.kCGMouseEventClickState, 2
    )

    _post(down1)
    time.sleep(0.02)
    _post(up1)
    time.sleep(0.02)
    _post(down2)
    time.sleep(0.02)
    _post(up2)


def mouse_right_click(x: float, y: float) -> None:
    """Right-click at (x, y)."""
    mouse_click(x, y, button="right")


def mouse_drag(
    sx: float,
    sy: float,
    ex: float,
    ey: float,
    duration: float = 0.5,
    *,
    hold_before_move_s: float = 0.01,
) -> None:
    """Drag from (sx, sy) to (ex, ey)."""
    btn = Quartz.kCGMouseButtonLeft
    steps = max(10, int(duration * 60))

    # Mouse down at start
    down = Quartz.CGEventCreateMouseEvent(
        None, Quartz.kCGEventLeftMouseDown, (sx, sy), btn
    )
    _post(down)
    # Keep this tiny for swipe-like gestures; a longer hold can trigger iOS icon drag.
    time.sleep(max(0.0, float(hold_before_move_s)))

    # Interpolate movement
    for i in range(1, steps + 1):
        t = i / steps
        cx = sx + (ex - sx) * t
        cy = sy + (ey - sy) * t
        drag_evt = Quartz.CGEventCreateMouseEvent(
            None, Quartz.kCGEventLeftMouseDragged, (cx, cy), btn
        )
        _post(drag_evt)
        time.sleep(duration / steps)

    # Mouse up at end
    up = Quartz.CGEventCreateMouseEvent(
        None, Quartz.kCGEventLeftMouseUp, (ex, ey), btn
    )
    _post(up)


def mouse_scroll(
    x: float,
    y: float,
    direction: str,
    amount: int = 500,
    *,
    unit: str = "pixel",
    repeat: int = 1,
    focus_click: bool = False,
    invert_y: bool = False,
) -> None:
    """Scroll at (x, y) in the given direction."""
    # Move cursor to position first
    mouse_move(x, y)
    time.sleep(0.05)

    if focus_click:
        # Some targets (incl. iPhone Mirroring) ignore wheel events unless focused.
        mouse_click(x, y)
        time.sleep(0.03)

    unit_map = {
        "line": Quartz.kCGScrollEventUnitLine,
        "pixel": Quartz.kCGScrollEventUnitPixel,
    }
    cg_unit = unit_map.get(unit, Quartz.kCGScrollEventUnitPixel)

    if direction not in ("up", "down", "left", "right"):
        return

    # Clamp repeat to a sane range to avoid runaway loops.
    repeat = max(1, min(int(repeat), 20))
    amount = int(amount)

    # Emit multiple smaller events for better compatibility than one huge jump.
    # iPhone Mirroring often responds better to a short burst.
    per = max(1, int(amount / repeat))

    for _ in range(repeat):
        if direction in ("up", "down"):
            # Quartz scroll wheel: conventionally, positive dy scrolls up and negative dy scrolls down.
            # Some macOS setups may feel inverted; allow an explicit invert.
            dy = (per if direction == "up" else -per)
            if invert_y:
                dy = -dy
            scroll_evt = Quartz.CGEventCreateScrollWheelEvent(
                None, cg_unit, 1, dy
            )
        else:
            dx = -per if direction == "left" else per
            scroll_evt = Quartz.CGEventCreateScrollWheelEvent(
                None, cg_unit, 2, 0, dx
            )
        _post(scroll_evt)
        time.sleep(0.01)
