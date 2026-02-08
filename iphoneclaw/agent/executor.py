from __future__ import annotations

import time
from dataclasses import asdict
from typing import Any, Dict, Optional, Tuple

from iphoneclaw.agent.coords import model_point_to_screen
from iphoneclaw.config import Config
from iphoneclaw.macos import input_mouse
import sys
import math

from iphoneclaw.macos.input_keyboard import paste_text, press
from iphoneclaw.macos.applescript_typing import type_text_macos_applescript
from iphoneclaw.parse.action_parser import parse_box_point
from iphoneclaw.parse.hotkey_map import maybe_rewrite_hotkey
from iphoneclaw.types import PredictionParsed, Rect, ScreenshotOutput


def _box_to_xy(box: Optional[str], bounds: Rect, factor: int) -> Optional[Tuple[float, float]]:
    pt = parse_box_point(box)
    if not pt:
        return None
    return model_point_to_screen(pt[0], pt[1], bounds=bounds, coord_factor=factor)

def _clamp_xy(x: float, y: float, bounds: Rect) -> Tuple[float, float]:
    cx = max(bounds.x + 1.0, min(bounds.x + bounds.width - 2.0, x))
    cy = max(bounds.y + 1.0, min(bounds.y + bounds.height - 2.0, y))
    return (cx, cy)

def _dist(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def execute_action(
    cfg: Config,
    pred: PredictionParsed,
    screenshot: ScreenshotOutput,
) -> Dict[str, Any]:
    """
    Execute one parsed action. Returns a dict suitable for recording.
    """
    t0 = time.time()
    action_type = pred.action_type
    ai = pred.action_inputs

    # Stability rewrite layer for cmd 1/2/3.
    rewrite = maybe_rewrite_hotkey(action_type, ai.key)
    if rewrite:
        action_type = rewrite

    bounds = screenshot.window_bounds
    factor = int(cfg.coord_factor)

    out: Dict[str, Any] = {
        "action_type": action_type,
        "raw_action_type": pred.action_type,
        "raw_action": pred.raw_action,
        "thought": pred.thought,
        "inputs": asdict(ai),
    }

    if action_type in ("finished", "call_user", "error_env"):
        out["skipped"] = True
        out["reason"] = "terminal"
        out["ok"] = True
        out["dt"] = time.time() - t0
        return out

    if cfg.dry_run:
        out["skipped"] = True
        out["reason"] = "dry_run"
        out["ok"] = True
        out["dt"] = time.time() - t0
        return out

    # iPhone specific shortcuts (mirroring app typically forwards these).
    if action_type == "iphone_home":
        press("1", modifiers=["cmd"])
        out["ok"] = True
        out["dt"] = time.time() - t0
        return out
    if action_type == "iphone_app_switcher":
        press("2", modifiers=["cmd"])
        out["ok"] = True
        out["dt"] = time.time() - t0
        return out

    try:
        # macOS has one shared cursor; best-effort restore so users can keep using their Mac.
        pre_cursor: Optional[Tuple[float, float]] = None
        expected_cursor: Optional[Tuple[float, float]] = None
        if cfg.restore_cursor:
            try:
                pre_cursor = input_mouse.mouse_position()
            except Exception:
                pre_cursor = None

        if action_type == "click":
            xy = _box_to_xy(ai.start_box, bounds, factor)
            if not xy:
                raise ValueError("missing start_box")
            expected_cursor = (xy[0], xy[1])
            input_mouse.mouse_click(xy[0], xy[1], button="left")

        elif action_type in ("left_double", "double_click", "doubleclick", "dblclick"):
            xy = _box_to_xy(ai.start_box, bounds, factor)
            if not xy:
                raise ValueError("missing start_box")
            expected_cursor = (xy[0], xy[1])
            interval_ms = ai.interval_ms
            if interval_ms is None:
                interval_ms = int(getattr(cfg, "double_click_interval_ms", 50))
            input_mouse.mouse_double_click(xy[0], xy[1], interval_s=float(interval_ms) / 1000.0)

        elif action_type == "right_single":
            xy = _box_to_xy(ai.start_box, bounds, factor)
            if not xy:
                raise ValueError("missing start_box")
            expected_cursor = (xy[0], xy[1])
            input_mouse.mouse_right_click(xy[0], xy[1])

        elif action_type == "drag":
            sxy = _box_to_xy(ai.start_box, bounds, factor)
            exy = _box_to_xy(ai.end_box, bounds, factor)
            if not sxy or not exy:
                raise ValueError("missing start_box/end_box")
            expected_cursor = (exy[0], exy[1])
            # Heuristic: long-distance drags on iPhone are usually swipe gestures (page/back),
            # where we must avoid a long-press that can start icon drag/rearrange.
            dist = math.hypot(exy[0] - sxy[0], exy[1] - sxy[1])
            is_swipe_like = dist >= max(220.0, min(bounds.width, bounds.height) * 0.25)
            if is_swipe_like:
                input_mouse.mouse_drag(
                    sxy[0],
                    sxy[1],
                    exy[0],
                    exy[1],
                    duration=0.18,
                    hold_before_move_s=0.004,
                )
            else:
                input_mouse.mouse_drag(
                    sxy[0],
                    sxy[1],
                    exy[0],
                    exy[1],
                    duration=0.45,
                    hold_before_move_s=0.02,
                )

        elif action_type == "scroll":
            xy = _box_to_xy(ai.start_box, bounds, factor)
            if not xy:
                # UI-TARS-desktop allows scroll(direction=...) without start_box.
                xy = (bounds.x + bounds.width / 2.0, bounds.y + bounds.height / 2.0)
            direction = (ai.direction or "").lower().strip()
            xy = _clamp_xy(xy[0], xy[1], bounds)
            expected_cursor = (xy[0], xy[1])

            if cfg.scroll_mode == "drag":
                # iOS-style scroll: use a short swipe gesture inside the window.
                # For vertical: to scroll down (see more below), swipe up.
                # For horizontal: match common "direction = swipe direction" expectation.
                dist = max(80.0, min(bounds.height * 0.35, 520.0))
                sx, sy = xy
                if direction == "down":
                    ex, ey = sx, sy - dist
                elif direction == "up":
                    ex, ey = sx, sy + dist
                elif direction == "left":
                    ex, ey = sx - dist, sy
                elif direction == "right":
                    ex, ey = sx + dist, sy
                else:
                    raise ValueError("missing/invalid direction")
                ex, ey = _clamp_xy(ex, ey, bounds)
                expected_cursor = (ex, ey)
                input_mouse.mouse_drag(
                    sx, sy, ex, ey, duration=0.16, hold_before_move_s=0.004
                )
            else:
                input_mouse.mouse_scroll(
                    xy[0],
                    xy[1],
                    direction=direction,
                    amount=int(cfg.scroll_amount),
                    unit=str(cfg.scroll_unit),
                    repeat=int(cfg.scroll_repeat),
                    focus_click=bool(cfg.scroll_focus_click),
                    invert_y=bool(getattr(cfg, "scroll_invert_y", False)),
                )

        elif action_type == "hotkey":
            # Expect "ctrl c" or "cmd shift p" style.
            key = (ai.key or "").strip().lower()
            parts = [p for p in key.split(" ") if p]
            if not parts:
                raise ValueError("missing key")
            mods = parts[:-1]
            k = parts[-1]
            press(k, modifiers=mods)

        elif action_type == "type":
            content = ai.content or ""
            if getattr(cfg, "type_ascii_only", True) and not content.isascii():
                raise ValueError(
                    "type(content=...) must be ASCII only. For Chinese, type pinyin (ASCII) "
                    "via the iPhone IME and select the Chinese candidate via clicks."
                )
            if sys.platform == "darwin":
                # macOS: prefer AppleScript keystroke (matches UI-TARS-desktop behavior).
                try:
                    # UI-TARS allows optional start_box for type() (focus first).
                    if ai.start_box:
                        xy = _box_to_xy(ai.start_box, bounds, factor)
                        if xy:
                            input_mouse.mouse_click(xy[0], xy[1], button="left")
                            time.sleep(0.05)
                    type_text_macos_applescript(
                        app_name=cfg.target_app,
                        content=content,
                        mode=cfg.applescript_mode,
                    )
                except Exception:
                    # Fallback: clipboard paste.
                    press_enter = content.endswith("\n")
                    if press_enter:
                        content = content[:-1]
                    paste_text(content, press_enter=press_enter)
            else:
                press_enter = content.endswith("\n")
                if press_enter:
                    content = content[:-1]
                paste_text(content, press_enter=press_enter)

        elif action_type == "sleep":
            # Fine-grained delays for multi-action sequences (e.g. click + sleep + click).
            if ai.ms is not None:
                time.sleep(max(0.0, float(ai.ms) / 1000.0))
            elif ai.seconds is not None:
                time.sleep(max(0.0, float(ai.seconds)))
            else:
                # Default: short sleep so it remains "fine-grained".
                time.sleep(0.05)

        elif action_type == "wait":
            time.sleep(5.0)

        else:
            raise ValueError("unsupported action_type: %s" % action_type)

        out["ok"] = True
    except Exception as e:
        out["ok"] = False
        out["error"] = str(e)
    finally:
        if cfg.restore_cursor and pre_cursor and expected_cursor:
            try:
                cur = input_mouse.mouse_position()
                # If the user moved the cursor away while we were executing, don't fight them.
                # Only restore when the cursor is still near where we expect our action left it.
                if _dist(cur, expected_cursor) <= 80.0:
                    input_mouse.mouse_move(pre_cursor[0], pre_cursor[1])
            except Exception:
                pass

    out["dt"] = time.time() - t0
    return out
