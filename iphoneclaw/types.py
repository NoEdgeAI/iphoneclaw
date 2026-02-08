from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class StatusEnum(str, Enum):
    INIT = "init"
    RUNNING = "running"
    PAUSE = "pause"
    HANG = "hang"
    END = "end"
    CALL_USER = "call_user"
    USER_STOPPED = "user_stopped"
    ERROR = "error"


@dataclass
class ActionInputs:
    content: Optional[str] = None
    start_box: Optional[str] = None
    end_box: Optional[str] = None
    key: Optional[str] = None
    direction: Optional[str] = None
    # Timing helpers for multi-action sequences.
    seconds: Optional[float] = None
    ms: Optional[int] = None
    interval_ms: Optional[int] = None
    # Resolved screen coordinates (after coordinate mapping)
    start_coords: Optional[tuple[float, float]] = None
    end_coords: Optional[tuple[float, float]] = None


@dataclass
class PredictionParsed:
    action_type: str
    action_inputs: ActionInputs
    thought: str = ""
    reflection: Optional[str] = None
    raw_action: str = ""


@dataclass
class Rect:
    x: float
    y: float
    width: float
    height: float


@dataclass
class ScreenshotOutput:
    base64: str
    scale_factor: float
    window_bounds: Rect
    image_width: int = 0
    image_height: int = 0
    # Optional: crop rectangle in *pixel coordinates* relative to the raw captured window image:
    # (x, y, width, height). When set, base64/image_width/image_height/window_bounds correspond
    # to the cropped region.
    crop_rect_px: Optional[tuple[int, int, int, int]] = None
    raw_image_width: int = 0
    raw_image_height: int = 0


@dataclass
class InvokeResult:
    prediction: str
    parsed_predictions: list[PredictionParsed]
    cost_tokens: int = 0
    cost_time: float = 0.0


@dataclass
class SupervisorEvent:
    type: str
    data: Dict[str, Any] = field(default_factory=dict)
    ts: float = 0.0
