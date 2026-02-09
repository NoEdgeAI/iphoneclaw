from __future__ import annotations

import json
import os
import re
import shlex
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple

from iphoneclaw.parse.action_parser import parse_predictions
from iphoneclaw.types import PredictionParsed
from iphoneclaw.automation.script_registry import resolve_script_path, ScriptRegistryError


class ScriptParseError(ValueError):
    pass


_TOP_SPLIT_CHARS = {"\n", ";", ","}
_WS_RE = re.compile(r"\s+")
_TEMPLATE_RE = re.compile(r"\$\{([A-Z0-9_]+)\}")


def render_template(text: str, vars: Optional[Dict[str, str]] = None) -> str:
    """
    Very small templating: replaces ${VARNAME} with vars[VARNAME] or os.environ[VARNAME].
    Unknown vars are left as-is so scripts remain editable.
    """
    vars = vars or {}

    def repl(m: re.Match[str]) -> str:
        k = m.group(1)
        if k in vars:
            return str(vars[k])
        if k in os.environ:
            return str(os.environ[k])
        return m.group(0)

    return _TEMPLATE_RE.sub(repl, text or "")


def _split_top_level(text: str) -> List[str]:
    """
    Split by newline/semicolon/comma, but only at top-level (not inside quotes/parentheses).
    """
    s = (text or "").strip()
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
        if depth == 0 and ch in _TOP_SPLIT_CHARS:
            flush()
            continue
        buf.append(ch)
    flush()
    return out


_KNOWN_KEYWORDS = (
    "iphone_home",
    "iphone_app_switcher",
    "sleep",
    "wait",
    "swipe",
    "fswipe",
    "scroll",
    "hotkey",
    "type",
    "open_app",
    "include",
    "run_script",
)


def _looks_like_action_call(stmt: str) -> bool:
    s = (stmt or "").strip()
    if not s:
        return False
    # "click(...)" / "sleep(ms=50)" / etc.
    if re.match(r"^[A-Za-z_]\w*\(.*\)$", s):
        return True
    # Bare action tokens: only allow a small whitelist to avoid interpreting DSL keywords
    # like "sleep" or "swipe" as raw action calls.
    if re.fullmatch(r"(iphone_home|iphone_app_switcher|wait|finished|call_user)\b", s):
        return True
    return False


def _split_compound_no_parens(stmt: str) -> List[str]:
    """
    Best-effort split for "iphone_home() sleep swipe left x 10 swipe down"
    when there are no parentheses. We split at keyword boundaries.
    """
    s = _WS_RE.sub(" ", (stmt or "").strip())
    if not s:
        return []
    parts = s.split(" ")

    out: List[str] = []
    cur: List[str] = []
    for tok in parts:
        low = tok.lower()
        is_kw = low in _KNOWN_KEYWORDS
        if is_kw and cur:
            out.append(" ".join(cur).strip())
            cur = [tok]
        else:
            cur.append(tok)
    if cur:
        out.append(" ".join(cur).strip())
    return [x for x in out if x]


def _explode_function_prefix(stmt: str) -> List[str]:
    """
    If stmt starts with a function call and has trailing text, split after the ')'.
    Example: "iphone_home() sleep swipe left" -> ["iphone_home()", "sleep swipe left"]
    """
    s = (stmt or "").strip()
    if not s:
        return []
    if "(" not in s:
        return [s]

    # Find a top-level close-paren for a leading call.
    quote: Optional[str] = None
    depth = 0
    for i, ch in enumerate(s):
        if quote is not None:
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
            continue
        if ch == "(":
            depth += 1
            continue
        if ch == ")":
            depth = max(0, depth - 1)
            if depth == 0:
                head = s[: i + 1].strip()
                tail = s[i + 1 :].strip()
                if head and tail:
                    return [head, tail]
                return [head] if head else ([tail] if tail else [])
    return [s]


def _parse_sleep_tokens(args: List[str]) -> str:
    if not args:
        return "sleep(ms=50)"
    a0 = args[0].strip().lower()
    if a0.endswith("ms"):
        n = a0[:-2].strip()
        return f"sleep(ms={int(float(n))})"
    if a0.endswith("s"):
        n = a0[:-1].strip()
        return f"sleep(seconds={float(n)})"
    # Heuristic: integer -> ms, float -> seconds.
    if re.fullmatch(r"\d+", a0):
        return f"sleep(ms={int(a0)})"
    return f"sleep(seconds={float(a0)})"


def _quote_py_string(s: str) -> str:
    # Use JSON to produce a safe quoted string; parse_predictions() accepts Python AST,
    # and JSON string literals are also valid Python string literals for simple escapes.
    return json.dumps(s)


def _unescape_type_content(s: str) -> str:
    """
    Support the same escape conventions we recommend to the model:
      \\n, \\r, \\t, \\\\, \\\", \\\'
    This keeps scripts writable in plain text while still generating valid
    type(content="...") calls for parse_predictions().
    """
    if not s:
        return ""
    # Order matters: unescape backslash last.
    s = s.replace("\\r", "\r").replace("\\n", "\n").replace("\\t", "\t")
    s = s.replace("\\\"", "\"").replace("\\'", "'")
    s = s.replace("\\\\", "\\")
    return s


def _parse_vars_tokens(tokens: List[str]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for tok in tokens:
        t = (tok or "").strip()
        if not t:
            continue
        if "=" not in t:
            raise ScriptParseError("vars must be KEY=VALUE, got: %r" % t)
        k, v = t.split("=", 1)
        k = k.strip()
        if not k:
            raise ScriptParseError("vars has empty key: %r" % t)
        out[k] = v
    return out


@dataclass(frozen=True)
class ScriptContext:
    base_dir: str
    vars: Dict[str, str]


def _macro_open_app(ctx: ScriptContext, name: str) -> List[str]:
    # Spotlight on iOS: home -> swipe down -> type "name\n" to launch first result.
    name = (name or "").strip()
    if not name:
        raise ScriptParseError("open_app requires a non-empty app name")
    content = name + "\n"
    return [
        "iphone_home()",
        "swipe(direction='down')",
        "sleep(ms=120)",
        f"type(content={_quote_py_string(content)})",
        "sleep(ms=350)",
    ]


def _expand_stmt(ctx: ScriptContext, stmt: str) -> List[str]:
    """
    Expand one statement into 1..N UI-TARS action calls.
    Returns action call strings compatible with iphoneclaw.parse.action_parser.parse_predictions().
    """
    s = (stmt or "").strip()
    if not s:
        return []

    # Comments
    if s.startswith("#") or s.startswith("//"):
        return []

    # Allow raw UI-TARS style action calls, e.g. click(...), iphone_home()
    if _looks_like_action_call(s):
        # Normalize bare "iphone_home" -> "iphone_home()"
        if re.fullmatch(r"(iphone_home|iphone_app_switcher|wait|finished|call_user)\b", s):
            return [s + "()"]
        return [s]

    # DSL mode
    try:
        toks = shlex.split(s, posix=True)
    except Exception:
        toks = s.split()
    if not toks:
        return []

    cmd = toks[0].lower()
    rest = toks[1:]

    # Support "<cmd> ... x N" repetition suffix.
    rep = 1
    if len(rest) >= 2 and rest[-2].lower() == "x":
        try:
            rep = int(rest[-1])
            rest = rest[:-2]
        except Exception:
            rep = 1

    calls: List[str] = []
    if cmd in ("iphone_home", "home"):
        calls = ["iphone_home()"]
    elif cmd in ("iphone_app_switcher", "app_switcher"):
        calls = ["iphone_app_switcher()"]
    elif cmd == "sleep":
        calls = [_parse_sleep_tokens(rest)]
    elif cmd == "wait":
        calls = ["wait()"]
    elif cmd in ("swipe", "fswipe"):
        if not rest:
            raise ScriptParseError("swipe requires a direction: up|down|left|right")
        d = rest[0].lower().strip()
        if d not in ("up", "down", "left", "right"):
            raise ScriptParseError("swipe direction must be up|down|left|right")
        calls = [f"swipe(direction={_quote_py_string(d)})"]
    elif cmd == "scroll":
        if not rest:
            raise ScriptParseError("scroll requires a direction: up|down|left|right")
        d = rest[0].lower().strip()
        if d not in ("up", "down", "left", "right"):
            raise ScriptParseError("scroll direction must be up|down|left|right")
        calls = [f"scroll(direction={_quote_py_string(d)})"]
    elif cmd == "hotkey":
        if not rest:
            raise ScriptParseError("hotkey requires keys, e.g. 'hotkey cmd 1'")
        key = " ".join(rest).strip().lower()
        calls = [f"hotkey(key={_quote_py_string(key)})"]
    elif cmd == "type":
        # Everything after "type" becomes content; allow "\n" escapes from user input.
        content = s[len(toks[0]) :].lstrip()
        content = _unescape_type_content(content)
        calls = [f"type(content={_quote_py_string(content)})"]
    elif cmd == "open_app":
        name = s[len(toks[0]) :].lstrip()
        calls = _macro_open_app(ctx, name)
    elif cmd in ("include", "run_script"):
        # DSL include:
        #   include open_app_spotlight APP=bilibili
        #   include action_scripts/common/open_app_spotlight.txt APP=bilibili
        #   run_script open_app_spotlight APP=bilibili
        if not rest:
            raise ScriptParseError("%s requires a script name/path" % cmd)
        target = rest[0].strip()
        if not target:
            raise ScriptParseError("%s requires a non-empty script name/path" % cmd)
        vars_in = _parse_vars_tokens(rest[1:])
        # Heuristic: if it looks like a path, use path=..., otherwise name=...
        looks_like_path = (
            "/" in target
            or "\\" in target
            or target.endswith(".txt")
            or target.startswith(".")
        )
        key = "path" if looks_like_path else "name"
        if vars_in:
            calls = [
                "run_script(%s=%s, vars=%s)"
                % (key, _quote_py_string(target), json.dumps(vars_in, ensure_ascii=False))
            ]
        else:
            calls = ["run_script(%s=%s)" % (key, _quote_py_string(target))]
    else:
        raise ScriptParseError(f"unknown command: {cmd!r}")

    if rep <= 0:
        return []
    return calls * rep


def script_to_action_calls(
    text: str,
    *,
    vars: Optional[Dict[str, str]] = None,
    base_dir: Optional[str] = None,
) -> List[str]:
    """
    Parse Action Script DSL (v1) into UI-TARS-compatible action call strings.

    Features:
    - separators: newline, ';', ',' (top-level only)
    - comments: lines starting with '#' or '//'
    - supports both raw action calls (click(...), drag(...), iphone_home()) and DSL keywords
    - supports repetition suffix: "swipe left x 10"
    - supports ${VARNAME} substitution (vars dict + environment variables)
    - supports nested script include via DSL:
      - include open_app_spotlight APP=bilibili
      - include action_scripts/common/open_app_spotlight.txt APP=bilibili
    """
    base_dir = os.path.abspath(base_dir or os.getcwd())
    vars = vars or {}
    ctx = ScriptContext(base_dir=base_dir, vars=vars)

    rendered = render_template(text or "", vars)
    stmts = _split_top_level(rendered)

    # Additional best-effort splitting for space-joined sequences.
    expanded_stmts: List[str] = []
    for st in stmts:
        for piece in _explode_function_prefix(st):
            piece = piece.strip()
            if not piece:
                continue
            if "(" not in piece and ")" not in piece and "=" not in piece:
                expanded_stmts.extend(_split_compound_no_parens(piece))
            else:
                expanded_stmts.append(piece)

    out: List[str] = []
    for st in expanded_stmts:
        out.extend(_expand_stmt(ctx, st))
    return [x for x in out if x.strip()]


def script_to_predictions(
    text: str,
    *,
    vars: Optional[Dict[str, str]] = None,
    base_dir: Optional[str] = None,
) -> List[PredictionParsed]:
    calls = script_to_action_calls(text, vars=vars, base_dir=base_dir)
    if not calls:
        return []
    joined = "\n".join(calls)
    preds = parse_predictions("Action: " + joined)
    return [p for p in preds if p.action_type != "error_env"]


def _coerce_vars(obj: object) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if obj is None:
        return out
    if not isinstance(obj, dict):
        raise ScriptParseError("vars must be a dict")
    for k, v in obj.items():
        if not isinstance(k, str) or not k.strip():
            continue
        out[k.strip()] = "" if v is None else str(v)
    return out


def parse_run_script_call(src: str) -> Tuple[Optional[str], Optional[str], Dict[str, str]]:
    """
    Parse:
      run_script(name="open_app_spotlight", vars={"APP":"bilibili"})
      run_script("open_app_spotlight", APP="bilibili")
      run_script(path="action_scripts/common/open_app_spotlight.txt", APP="bilibili")
    Returns: (name, path, vars)
    """
    import ast

    s = (src or "").strip()
    if not s:
        raise ScriptParseError("empty run_script()")
    try:
        node = ast.parse(s, mode="eval").body
    except Exception as e:
        raise ScriptParseError("invalid run_script() syntax: %s" % str(e)) from e

    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name) or node.func.id != "run_script":
        raise ScriptParseError("not a run_script(...) call")

    name: Optional[str] = None
    path: Optional[str] = None
    vars_in: Dict[str, str] = {}

    # Positional: run_script("name")
    if node.args:
        try:
            v0 = ast.literal_eval(node.args[0])
        except Exception:
            v0 = None
        if isinstance(v0, str) and v0.strip():
            name = v0.strip()

    # Keywords
    for kw in node.keywords:
        if kw.arg is None:
            continue
        k = kw.arg
        try:
            v = ast.literal_eval(kw.value)
        except Exception:
            v = None
        if k == "name":
            if isinstance(v, str) and v.strip():
                name = v.strip()
        elif k == "path":
            if isinstance(v, str) and v.strip():
                path = v.strip()
        elif k == "vars":
            vars_in.update(_coerce_vars(v))
        else:
            # Sugar: run_script("x", APP="bilibili")
            vars_in[str(k)] = "" if v is None else str(v)

    return name, path, vars_in


def run_script_to_predictions(
    raw_action: str,
    *,
    registry_path: str,
) -> List[PredictionParsed]:
    """
    Expand a `run_script(...)` action call into executable predictions.
    """
    name, path, vars_in = parse_run_script_call(raw_action)
    target = path or name
    if not target:
        raise ScriptParseError("run_script(...) missing name/path")
    try:
        script_path = resolve_script_path(str(target), registry_path=registry_path)
    except ScriptRegistryError as e:
        raise ScriptParseError(str(e)) from e

    with open(script_path, "r", encoding="utf-8") as f:
        src = f.read()
    return script_to_predictions(src, vars=vars_in, base_dir=os.path.dirname(script_path))


def _expand_prediction_recursive(
    pred: PredictionParsed,
    *,
    registry_path: str,
    stack: Tuple[str, ...],
    depth_left: int,
) -> List[PredictionParsed]:
    if pred.action_type != "run_script":
        return [pred]

    if depth_left <= 0:
        raise ScriptParseError("run_script expansion depth exceeded; possible recursion")

    name, path, vars_in = parse_run_script_call(pred.raw_action)
    target = path or name
    if not target:
        raise ScriptParseError("run_script(...) missing name/path")
    try:
        script_path = resolve_script_path(str(target), registry_path=registry_path)
    except ScriptRegistryError as e:
        raise ScriptParseError(str(e)) from e

    script_path = os.path.abspath(script_path)
    if script_path in stack:
        loop_chain = list(stack) + [script_path]
        raise ScriptParseError(
            "circular script include detected: %s"
            % " -> ".join(os.path.basename(p) for p in loop_chain)
        )

    with open(script_path, "r", encoding="utf-8") as f:
        src = f.read()
    inner = script_to_predictions(src, vars=vars_in, base_dir=os.path.dirname(script_path))

    out: List[PredictionParsed] = []
    next_stack = stack + (script_path,)
    for p in inner:
        out.extend(
            _expand_prediction_recursive(
                p,
                registry_path=registry_path,
                stack=next_stack,
                depth_left=depth_left - 1,
            )
        )
    return out


def expand_special_predictions(
    preds: Iterable[PredictionParsed],
    *,
    registry_path: str,
    max_expand_depth: int = 2,
) -> List[PredictionParsed]:
    """
    Expand special action types like run_script(...) into concrete actions.
    """
    depth = max(0, int(max_expand_depth))
    out: List[PredictionParsed] = []
    for p in list(preds):
        out.extend(
            _expand_prediction_recursive(
                p,
                registry_path=registry_path,
                stack=tuple(),
                depth_left=depth,
            )
        )
    return out
