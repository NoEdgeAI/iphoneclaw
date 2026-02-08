from __future__ import annotations

from dataclasses import dataclass
import os

from iphoneclaw.constants import MAX_LOOP_COUNT


@dataclass
class Config:
    # Model
    model_base_url: str = "http://localhost:8000/v1"
    model_api_key: str = ""
    model_name: str = "doubao-1-5-ui-tars-250428"
    # Many OpenAI-compatible providers reject very large max_tokens with HTTP 400.
    max_tokens: int = 8192
    temperature: float = 0.0
    top_p: float = 0.7

    # Volcengine/Doubao compatibility knobs (OpenAI-compatible, but has quirks).
    # - "thinking" field is accepted by Ark; keep disabled by default for stability.
    volc_thinking_type: str = "disabled"  # disabled|enabled

    # Agent
    max_loop_count: int = MAX_LOOP_COUNT
    loop_interval_ms: int = 1000
    language: str = "en"
    dry_run: bool = False

    # Model coordinate system factor (UI-TARS typically uses 0..1000)
    coord_factor: int = 1000

    # Supervisor
    supervisor_host: str = "127.0.0.1"
    supervisor_port: int = 17334
    supervisor_token: str = ""
    hang_on_finished: bool = True
    hang_on_call_user: bool = True
    enable_supervisor: bool = True
    # Optional: allow supervisor API to expose image paths / run artifacts (local-only).
    # Default off to keep the supervisor API "text-only" unless explicitly enabled.
    enable_supervisor_images: bool = True
    # Optional: allow supervisor API to execute actions directly (only when paused).
    # Default off for safety.
    enable_supervisor_exec: bool = True

    # Recording
    record_dir: str = "./runs"

    # Target app
    target_app: str = "iPhone Mirroring"
    window_contains: str = ""

    # AppleScript typing (macOS only). If configured, typing will use System Events keystroke.
    applescript_mode: str = "native"  # auto|native|osascript

    # Scrolling behavior (iPhone Mirroring can be sensitive to wheel magnitude).
    scroll_mode: str = "wheel"  # wheel|drag
    scroll_unit: str = "pixel"  # pixel|line
    scroll_amount: int = 1000  # pixels or lines, depending on scroll_unit
    scroll_repeat: int = 10
    # If enabled, scroll will click to focus first. This can accidentally open items under cursor,
    # so keep it OFF by default.
    scroll_focus_click: bool = False
    scroll_invert_y: bool = False

    # UX: restore mouse cursor position after each action.
    # Default OFF: restoring can feel "fighty" while the operator uses the Mac.
    restore_cursor: bool = False

    # Double-click timing: some iPhone UIs require a slightly slower double-click to show controls.
    double_click_interval_ms: int = 50

    # UX: if the user moves the mouse / presses keys while the worker is running,
    # automatically pause and emit an SSE event for external supervisors.
    # Default off to avoid surprising pauses; enable explicitly when needed.
    auto_pause_on_user_input: bool = False

    # Safety: detect repeated identical actions (often a dead-loop) and auto-pause
    # to request supervisor intervention via SSE (`needs_supervisor`).
    auto_pause_on_repeat_action: bool = True
    repeat_action_streak_threshold: int = 4

    # Input constraint: disallow non-ASCII typing. For Chinese, type pinyin (ASCII)
    # and then select IME candidates via clicks.
    type_ascii_only: bool = True


def load_config_from_env() -> Config:
    """Lightweight env override to avoid hard dependency on pydantic."""
    c = Config()
    c.model_base_url = os.getenv("IPHONECLAW_MODEL_BASE_URL", c.model_base_url)
    c.model_api_key = os.getenv("IPHONECLAW_MODEL_API_KEY", c.model_api_key)
    c.model_name = os.getenv("IPHONECLAW_MODEL_NAME", c.model_name)
    c.supervisor_host = os.getenv("IPHONECLAW_SUPERVISOR_HOST", c.supervisor_host)
    c.supervisor_port = int(os.getenv("IPHONECLAW_SUPERVISOR_PORT", str(c.supervisor_port)))
    c.supervisor_token = os.getenv("IPHONECLAW_SUPERVISOR_TOKEN", c.supervisor_token)
    c.target_app = os.getenv("IPHONECLAW_TARGET_APP", c.target_app)
    c.window_contains = os.getenv("IPHONECLAW_WINDOW_CONTAINS", c.window_contains)
    c.record_dir = os.getenv("IPHONECLAW_RECORD_DIR", c.record_dir)
    c.enable_supervisor_images = os.getenv(
        "IPHONECLAW_ENABLE_SUPERVISOR_IMAGES", "1" if c.enable_supervisor_images else "0"
    ).strip().lower() in ("1", "true", "yes", "y", "on")
    c.enable_supervisor_exec = os.getenv(
        "IPHONECLAW_ENABLE_SUPERVISOR_EXEC", "1" if c.enable_supervisor_exec else "0"
    ).strip().lower() in ("1", "true", "yes", "y", "on")

    # AppleScript runner mode for typing/hotkeys.
    c.applescript_mode = os.getenv("IPHONECLAW_APPLESCRIPT_MODE", c.applescript_mode)

    c.scroll_mode = os.getenv("IPHONECLAW_SCROLL_MODE", c.scroll_mode)
    c.scroll_unit = os.getenv("IPHONECLAW_SCROLL_UNIT", c.scroll_unit)
    c.scroll_amount = int(os.getenv("IPHONECLAW_SCROLL_AMOUNT", str(c.scroll_amount)))
    c.scroll_repeat = int(os.getenv("IPHONECLAW_SCROLL_REPEAT", str(c.scroll_repeat)))
    c.scroll_focus_click = os.getenv(
        "IPHONECLAW_SCROLL_FOCUS_CLICK", "1" if c.scroll_focus_click else "0"
    ).strip().lower() in ("1", "true", "yes", "y", "on")
    c.scroll_invert_y = os.getenv(
        "IPHONECLAW_SCROLL_INVERT_Y", "1" if c.scroll_invert_y else "0"
    ).strip().lower() in ("1", "true", "yes", "y", "on")
    c.restore_cursor = os.getenv(
        "IPHONECLAW_RESTORE_CURSOR", "1" if c.restore_cursor else "0"
    ).strip().lower() in ("1", "true", "yes", "y", "on")
    c.double_click_interval_ms = int(
        os.getenv("IPHONECLAW_DOUBLE_CLICK_INTERVAL_MS", str(c.double_click_interval_ms))
    )
    c.auto_pause_on_user_input = os.getenv(
        "IPHONECLAW_AUTO_PAUSE_ON_USER_INPUT", "1" if c.auto_pause_on_user_input else "0"
    ).strip().lower() in ("1", "true", "yes", "y", "on")
    c.auto_pause_on_repeat_action = os.getenv(
        "IPHONECLAW_AUTO_PAUSE_ON_REPEAT_ACTION", "1" if c.auto_pause_on_repeat_action else "0"
    ).strip().lower() in ("1", "true", "yes", "y", "on")
    c.repeat_action_streak_threshold = int(
        os.getenv("IPHONECLAW_REPEAT_ACTION_STREAK_THRESHOLD", str(c.repeat_action_streak_threshold))
    )
    c.type_ascii_only = os.getenv(
        "IPHONECLAW_TYPE_ASCII_ONLY", "1" if c.type_ascii_only else "0"
    ).strip().lower() in ("1", "true", "yes", "y", "on")
    return c
