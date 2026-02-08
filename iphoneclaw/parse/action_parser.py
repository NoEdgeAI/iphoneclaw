from __future__ import annotations

import ast
import re
from typing import Any, Dict, List, Optional, Tuple

from iphoneclaw.types import ActionInputs, PredictionParsed


_ACTION_SPLIT_RE = re.compile(r"Action[:：]")
_THOUGHT_RE = re.compile(r"Thought:\s*([\s\S]+?)(?=\s*Action[:：]|$)", re.IGNORECASE)
_REFLECTION_RE = re.compile(
    r"Reflection:\s*([\s\S]+?)Action_Summary:\s*([\s\S]+?)(?=\s*Action[:：]|$)",
    re.IGNORECASE,
)
_SUMMARY_RE = re.compile(r"Action_Summary:\s*([\s\S]+?)(?=\s*Action[:：]|$)", re.IGNORECASE)

_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _extract_thought_reflection_action(text: str) -> Tuple[str, Optional[str], str]:
    """
    Mirror UI-TARS-desktop parsing:
    - Thought: ... until Action[:：]
    - Or Reflection + Action_Summary patterns
    - actionStr is the last segment after Action[:：] (or whole text if no Action keyword)
    """
    text = (text or "").strip()
    reflection: Optional[str] = None
    thought = ""

    m = _THOUGHT_RE.search(text)
    if m:
        thought = m.group(1).strip()
        # If the model repeats "Thought:" on new lines, strip the repeated labels.
        thought = re.sub(r"(?im)^\s*Thought:\s*", "", thought).strip()
    else:
        m = _REFLECTION_RE.search(text)
        if m:
            reflection = m.group(1).strip()
            thought = m.group(2).strip()
        else:
            m = _SUMMARY_RE.search(text)
            if m:
                thought = m.group(1).strip()

    if not _ACTION_SPLIT_RE.search(text):
        action_str = text
    else:
        parts = _ACTION_SPLIT_RE.split(text)
        action_str = parts[-1] if parts else ""

    return thought, reflection, action_str.strip()


def parse_box_point(s: Optional[str]) -> Optional[Tuple[float, float]]:
    """
    Parse UI-TARS-compatible point/box strings.
    Supports: "(x,y)", "x y", "[x,y]", "[x1,y1,x2,y2]", "<bbox>..</bbox>", "<point>..</point>".
    If 4 numbers are present, returns the box center.
    """
    if not s:
        return None
    ss = str(s).strip()
    if not ss or ss == "[]":
        return None

    nums = [float(x) for x in _NUM_RE.findall(ss)]
    if len(nums) < 2:
        return None
    if len(nums) >= 4:
        x1, y1, x2, y2 = nums[0], nums[1], nums[2], nums[3]
        return (x1 + x2) / 2.0, (y1 + y2) / 2.0
    return nums[0], nums[1]


def _split_args(args_str: str) -> List[str]:
    out: List[str] = []
    buf: List[str] = []
    quote: Optional[str] = None
    depth = 0
    for ch in args_str:
        if quote is not None:
            buf.append(ch)
            if ch == quote:
                quote = None
            continue

        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
            continue
        if ch == "(":
            depth += 1
            buf.append(ch)
            continue
        if ch == ")":
            depth = max(0, depth - 1)
            buf.append(ch)
            continue
        if ch == "," and depth == 0:
            part = "".join(buf).strip()
            if part:
                out.append(part)
            buf = []
            continue
        buf.append(ch)
    last = "".join(buf).strip()
    if last:
        out.append(last)
    return out


def _preprocess_action(action_str: str) -> str:
    s = (action_str or "").strip()
    # Remove UI-TARS box tags
    s = s.replace("<|box_start|>", "").replace("<|box_end|>", "")
    # Normalize point/start_point/end_point into start_box/end_box (UI-TARS behavior)
    s = re.sub(r"start_point\s*=", "start_box=", s)
    s = re.sub(r"end_point\s*=", "end_box=", s)
    # After rewriting start_point/end_point, rewrite any remaining bare point= into start_box=
    s = re.sub(r"\bpoint\s*=", "start_box=", s)
    return s


def _parse_action_call(action_src: str) -> Tuple[str, Dict[str, Any]]:
    """
    Parse a single action call.
    Strategy:
    1) Preprocess UI-TARS variants (point/start_point/end_point, <|box_*|> tags)
    2) Try Python AST (strict, supports escaped strings)
    3) Fallback to UI-TARS regex-like parsing (tolerant)
    """
    action_src = _preprocess_action(action_src).strip().rstrip(".")

    # Allow bare action tokens like "iphone_home" (models sometimes omit "()").
    if re.fullmatch(r"\w+", action_src):
        return action_src, {}

    try:
        node = ast.parse(action_src, mode="eval").body
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            func = node.func.id
            kwargs: Dict[str, Any] = {}
            for kw in node.keywords:
                if kw.arg is None:
                    continue
                kwargs[kw.arg] = ast.literal_eval(kw.value)
            return func, kwargs
    except Exception:
        pass

    # Fallback: regex-ish
    m = re.match(r"^(\w+)\((.*)\)$", action_src.strip())
    if not m:
        raise ValueError(f"Not a function call: {action_src!r}")
    func = m.group(1)
    args_str = m.group(2).strip()
    kwargs2: Dict[str, Any] = {}
    if args_str:
        for pair in _split_args(args_str):
            if "=" not in pair:
                continue
            key, value = pair.split("=", 1)
            key = key.strip()
            value = value.strip()
            # Remove surrounding quotes
            if (value.startswith("'") and value.endswith("'")) or (
                value.startswith('"') and value.endswith('"')
            ):
                value = value[1:-1]

            # Support <bbox> / <point>
            if "<bbox>" in value:
                v = re.sub(r"</?bbox>", "", value, flags=re.IGNORECASE)
                v = re.sub(r"\s+", ",", v.strip())
                value = f"({v})"
            if "<point>" in value:
                v = re.sub(r"</?point>", "", value, flags=re.IGNORECASE)
                v = re.sub(r"\s+", ",", v.strip())
                value = f"({v})"

            kwargs2[key] = value
    return func, kwargs2


def _split_actions(action_str: str) -> List[str]:
    """
    Split a multi-action string into individual action calls.

    Models vary: some separate actions by blank lines, some by newlines, some by semicolons.
    We split on ';' or newline at top-level (not inside parentheses/quotes).
    """
    s = (action_str or "").strip()
    if not s:
        return []

    out: List[str] = []
    buf: List[str] = []
    quote: Optional[str] = None
    depth = 0

    def flush() -> None:
        part = "".join(buf).strip()
        buf.clear()
        if part:
            out.append(part)

    for ch in s:
        if quote is not None:
            buf.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
            continue
        if ch == "(":
            depth += 1
            buf.append(ch)
            continue
        if ch == ")":
            depth = max(0, depth - 1)
            buf.append(ch)
            continue
        if depth == 0 and ch in (";", "\n"):
            flush()
            continue
        buf.append(ch)
    flush()

    # Also handle the historical "blank line" separator by trimming empties above.
    return [x for x in out if x.strip()]


def parse_predictions(text: str) -> List[PredictionParsed]:
    thought, reflection, action_str = _extract_thought_reflection_action(text)
    if not action_str:
        return [
            PredictionParsed(
                action_type="error_env",
                action_inputs=ActionInputs(content="missing action"),
                thought=thought,
                reflection=reflection,
                raw_action="",
            )
        ]

    # Parse multiple actions; tolerate UI-TARS-desktop and provider variations.
    raw_actions = _split_actions(action_str)
    out: List[PredictionParsed] = []
    for raw in raw_actions:
        try:
            action_type, kwargs = _parse_action_call(raw.strip())
        except Exception as e:
            out.append(
                PredictionParsed(
                    action_type="error_env",
                    action_inputs=ActionInputs(content=f"parse error: {e}"),
                    thought=thought,
                    reflection=reflection,
                    raw_action=raw.strip(),
                )
            )
            continue

        ai = ActionInputs()
        # UI-TARS sometimes uses "text" for typing.
        if "content" in kwargs:
            ai.content = str(kwargs["content"])
        elif "text" in kwargs:
            ai.content = str(kwargs["text"])
        if "start_box" in kwargs:
            ai.start_box = str(kwargs["start_box"])
        if "end_box" in kwargs:
            ai.end_box = str(kwargs["end_box"])
        if "key" in kwargs:
            ai.key = str(kwargs["key"])
        elif "hotkey" in kwargs:
            ai.key = str(kwargs["hotkey"])
        if "direction" in kwargs:
            ai.direction = str(kwargs["direction"])
        # Optional timing helpers.
        if "seconds" in kwargs:
            try:
                ai.seconds = float(kwargs["seconds"])
            except Exception:
                ai.seconds = None
        if "ms" in kwargs:
            try:
                ai.ms = int(kwargs["ms"])
            except Exception:
                ai.ms = None
        if "interval_ms" in kwargs:
            try:
                ai.interval_ms = int(kwargs["interval_ms"])
            except Exception:
                ai.interval_ms = None

        out.append(
            PredictionParsed(
                action_type=action_type,
                action_inputs=ai,
                thought=thought,
                reflection=reflection,
                raw_action=raw.strip(),
            )
        )

    return out
