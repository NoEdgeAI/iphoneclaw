from __future__ import annotations

import argparse
import base64
import os
import re
import subprocess
import time
from typing import List, Optional

from iphoneclaw.config import Config
from iphoneclaw.config import load_config_from_env
from iphoneclaw.supervisor.hub import SupervisorHub
from iphoneclaw.supervisor.server import SupervisorHTTPServer
from iphoneclaw.supervisor.state import WorkerControl
from iphoneclaw.macos.capture import ScreenCapture
from iphoneclaw.macos.permissions import run_doctor
from iphoneclaw.macos.window import WindowFinder
from iphoneclaw.macos.window import expand_app_aliases
from iphoneclaw.macos.window import list_on_screen_windows
from iphoneclaw.agent.conversation import ConversationStore
from iphoneclaw.agent.loop import Worker

from iphoneclaw.automation.action_script import ScriptParseError, script_to_predictions
from iphoneclaw.automation.action_script import expand_special_predictions
from iphoneclaw.agent.executor import execute_action


def _normalize_model_name(model: str) -> str:
    """
    Accept UI-TARS-desktop style provider labels and normalize to a real Ark/OpenAI model id.
    Example:
      "VolcEngine Ark for Doubao-1.5-thinking-vision-pro" -> "doubao-1.5-thinking-vision-pro"
    """
    m = (model or "").strip()
    if not m:
        return m
    prefix = "VolcEngine Ark for "
    if m.startswith(prefix):
        m = m[len(prefix) :].strip()
    # Common "display names" from docs; keep as-is except normalize spaces.
    # If user pasted the full UI label (with spaces), remove spaces around tokens.
    m2 = " ".join(m.split())
    return m2


def _add_common_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--app",
        default=Config().target_app,
        help="Target macOS app name (default: iPhone Mirroring).",
    )
    p.add_argument(
        "--window-contains",
        default="",
        help="Override window match: require owner/title to contain this substring (debug/escape hatch).",
    )


def cmd_doctor(_args: argparse.Namespace) -> int:
    return 0 if run_doctor() else 2


def cmd_launch(args: argparse.Namespace) -> int:
    wf = WindowFinder(app_name=args.app, window_contains=args.window_contains)
    wf.launch_app()
    b = wf.bounds
    print(
        f"window: app={args.app!r} id={wf.window_id} "
        f"bounds=({b.x:.1f},{b.y:.1f},{b.width:.1f},{b.height:.1f})"
    )
    return 0


def cmd_bounds(args: argparse.Namespace) -> int:
    wf = WindowFinder(app_name=args.app, window_contains=args.window_contains)
    wf.find_window()
    b = wf.bounds
    print(f"{b.x:.1f} {b.y:.1f} {b.width:.1f} {b.height:.1f}")
    return 0


def cmd_screenshot(args: argparse.Namespace) -> int:
    wf = WindowFinder(app_name=args.app, window_contains=args.window_contains)
    wf.find_window()
    cap = ScreenCapture(wf)
    shot = cap.capture()

    out_path = args.out
    if out_path is None:
        out_path = os.path.abspath("screenshot.jpg")
    else:
        out_path = os.path.abspath(out_path)

    jpg = base64.b64decode(shot.base64)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "wb") as f:
        f.write(jpg)

    b = shot.window_bounds
    print(
        f"wrote: {out_path}\n"
        f"scale_factor: {shot.scale_factor:.3f}\n"
        f"bounds: ({b.x:.1f},{b.y:.1f},{b.width:.1f},{b.height:.1f})"
    )
    if shot.crop_rect_px:
        print(f"crop_rect_px: {shot.crop_rect_px} (raw {shot.raw_image_width}x{shot.raw_image_height})")
    return 0


def cmd_calibrate(args: argparse.Namespace) -> int:
    wf = WindowFinder(app_name=args.app, window_contains=args.window_contains)
    wf.find_window()
    cap = ScreenCapture(wf)
    shot = cap.capture()

    out_dir = os.path.abspath(args.out_dir or ".")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "calibrate_screenshot.jpg")

    jpg = base64.b64decode(shot.base64)
    with open(out_path, "wb") as f:
        f.write(jpg)

    b = shot.window_bounds
    print("wrote: %s" % out_path)
    print("window bounds (global): x=%.1f y=%.1f w=%.1f h=%.1f" % (b.x, b.y, b.width, b.height))
    print("screenshot pixels: w=%d h=%d scale_factor=%.3f" % (shot.image_width, shot.image_height, shot.scale_factor))
    if shot.crop_rect_px:
        print("raw window pixels: w=%d h=%d crop_rect_px=%s" % (shot.raw_image_width, shot.raw_image_height, shot.crop_rect_px))
    print("mapping: screen_x = x + (model_x/1000)*w ; screen_y = y + (model_y/1000)*h")
    return 0


def cmd_ocr(args: argparse.Namespace) -> int:
    """Run Apple Vision OCR directly on the current target window screenshot."""
    import json

    wf = WindowFinder(app_name=args.app, window_contains=args.window_contains)
    wf.find_window()

    # Match script-run behavior: ensure target app is frontmost before capture.
    if _is_target_frontmost(args.app):
        print("target app already frontmost: %r" % args.app)
    else:
        for _ in range(4):
            wf.activate_app()
            time.sleep(0.25)
            if _is_target_frontmost(args.app):
                break
        if _is_target_frontmost(args.app):
            print("activated target app to frontmost: %r" % args.app)
        else:
            print(
                "warning: failed to bring target app %r to front. frontmost now=%r"
                % (args.app, _frontmost_app_name())
            )

    # Refresh once after optional activation to avoid stale bounds.
    wf.refresh()
    cap = ScreenCapture(wf)
    shot = cap.capture()

    try:
        from iphoneclaw.macos.ocr_vision import recognize_screenshot_text, save_ocr_debug_visualization

        payload = recognize_screenshot_text(
            shot,
            coord_factor=int(args.coord_factor),
            min_confidence=float(args.min_confidence),
            max_items=(int(args.max_items) if int(args.max_items) > 0 else None),
            languages=_parse_ocr_langs(getattr(args, "lang", [])),
            auto_detect_language=not bool(getattr(args, "no_auto_detect_language", False)),
        )

        if bool(args.debug_draw):
            try:
                dbg = save_ocr_debug_visualization(
                    shot,
                    payload,
                    out_dir=str(args.debug_dir or "./ocr_debug"),
                    prefix=str(args.debug_prefix or "ocr"),
                )
                payload["debug"] = dbg
            except Exception as e:
                payload["debug_error"] = str(e)
    except Exception as e:
        print("ocr error: %s" % str(e))
        return 2

    print(json.dumps({"ok": True, **payload}, ensure_ascii=False, indent=2))
    return 0


def cmd_windows(args: argparse.Namespace) -> int:
    wins = list_on_screen_windows()
    needle = (args.contains or "").lower().strip()
    limit = int(args.limit)

    rows = []
    for w in wins:
        owner = str(w.get("kCGWindowOwnerName") or "")
        title = str(w.get("kCGWindowName") or "")
        bounds = w.get("kCGWindowBounds") or {}
        ww = int(bounds.get("Width") or 0)
        hh = int(bounds.get("Height") or 0)
        layer = int(w.get("kCGWindowLayer") or 0)
        wid = int(w.get("kCGWindowNumber") or 0)
        pid = int(w.get("kCGWindowOwnerPID") or 0)

        hay = (owner + " " + title).lower()
        if needle and needle not in hay:
            continue
        if ww < 50 or hh < 50:
            continue
        rows.append((ww * hh, owner, title, wid, pid, layer, ww, hh))

    rows.sort(reverse=True, key=lambda x: x[0])
    rows = rows[:limit]

    for area, owner, title, wid, pid, layer, ww, hh in rows:
        print(
            "area=%d owner=%r title=%r wid=%d pid=%d layer=%d size=%dx%d"
            % (area, owner, title, wid, pid, layer, ww, hh)
        )
    if not rows:
        print("no windows matched")
    return 0


def _supervisor_base(cfg: Config) -> str:
    return "http://%s:%d" % (cfg.supervisor_host, int(cfg.supervisor_port))


def cmd_serve(args: argparse.Namespace) -> int:
    cfg = load_config_from_env()
    cfg.supervisor_host = args.host
    cfg.supervisor_port = int(args.port)
    cfg.supervisor_token = args.token or cfg.supervisor_token

    hub = SupervisorHub()
    control = WorkerControl()
    conv = ConversationStore()
    srv = SupervisorHTTPServer(cfg, hub, control, conv)
    srv.start()
    print("supervisor listening on %s" % _supervisor_base(cfg))
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        srv.stop()
        return 130


def cmd_run(args: argparse.Namespace) -> int:
    cfg = load_config_from_env()
    cfg.target_app = args.app
    cfg.window_contains = args.window_contains or cfg.window_contains
    cfg.model_base_url = args.base_url or cfg.model_base_url
    cfg.model_api_key = args.api_key or cfg.model_api_key
    cfg.model_name = _normalize_model_name(args.model or cfg.model_name)
    cfg.dry_run = bool(args.dry_run)
    cfg.record_dir = args.record_dir or cfg.record_dir
    cfg.supervisor_host = args.host or cfg.supervisor_host
    cfg.supervisor_port = int(args.port or cfg.supervisor_port)
    cfg.supervisor_token = args.token or cfg.supervisor_token
    cfg.max_tokens = int(args.max_tokens or cfg.max_tokens)
    cfg.temperature = float(args.temperature if args.temperature is not None else cfg.temperature)
    cfg.top_p = float(args.top_p if args.top_p is not None else cfg.top_p)
    if args.volc_thinking_type:
        cfg.volc_thinking_type = args.volc_thinking_type
    if args.scroll_mode:
        cfg.scroll_mode = args.scroll_mode
    if args.scroll_unit:
        cfg.scroll_unit = args.scroll_unit
    if args.scroll_amount is not None:
        cfg.scroll_amount = int(args.scroll_amount)
    if args.scroll_repeat is not None:
        cfg.scroll_repeat = int(args.scroll_repeat)
    if args.scroll_focus_click is not None:
        cfg.scroll_focus_click = bool(args.scroll_focus_click)
    if getattr(args, "auto_pause_on_user_input", False):
        cfg.auto_pause_on_user_input = True
    if getattr(args, "no_auto_pause_on_user_input", False):
        cfg.auto_pause_on_user_input = False

    hub = SupervisorHub()
    control = WorkerControl()
    conv = ConversationStore()
    # Create a recorder up-front so the supervisor server can expose run artifacts.
    from iphoneclaw.agent.recorder import RunRecorder
    recorder = RunRecorder(cfg)

    srv = None
    if cfg.enable_supervisor:
        srv = SupervisorHTTPServer(cfg, hub, control, conv, recorder=recorder)
        srv.start()
        print("supervisor listening on %s" % _supervisor_base(cfg))

    w = Worker(cfg, hub=hub, control=control, conversation=conv, recorder=recorder)
    try:
        w.run(args.instruction)
    finally:
        if srv is not None:
            srv.stop()
    return 0


def cmd_ctl(args: argparse.Namespace) -> int:
    import json
    import urllib.parse
    import urllib.request
    import urllib.error

    cfg = load_config_from_env()
    base = args.base or _supervisor_base(cfg)
    token = args.token or cfg.supervisor_token

    def req(method: str, path: str, body: Optional[dict] = None) -> dict:
        url = base.rstrip("/") + path
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
        r = urllib.request.Request(url, data=data, method=method)
        if token:
            r.add_header("Authorization", "Bearer %s" % token)
        if body is not None:
            r.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(r, timeout=30) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw)
        except urllib.error.HTTPError as e:
            try:
                raw = e.read().decode("utf-8", errors="replace")
            except Exception:
                raw = ""
            msg = (
                f"Supervisor API returned HTTP {e.code} for {url} (cmd={args.action}).\n"
                "This usually means the worker is running an older iphoneclaw that does not have this endpoint, "
                "or you are pointing `ctl` at the wrong base URL.\n"
                "Fix: stop the running worker and restart `python -m iphoneclaw run ...` after pulling latest.\n"
                "If needed, override with `--base http://127.0.0.1:17334` and `--token ...`.\n"
            )
            if raw.strip():
                msg += "Response body:\n" + raw.strip() + "\n"
            raise RuntimeError(msg) from e
        except urllib.error.URLError as e:
            # Common: server not running (ConnectionRefusedError).
            tip = ""
            if str(args.action) == "ocr":
                tip = (
                    "\nTip: you can test OCR locally without supervisor:\n"
                    "  python -m iphoneclaw ocr --app \"iPhone Mirroring\" --min-confidence 0.2"
                )
            raise RuntimeError(
                "Failed to reach supervisor API at %s (cmd=%s). "
                "Is `python -m iphoneclaw run ...` currently running, and is the host/port correct? "
                "You can override with `--base http://127.0.0.1:17334` and `--token ...`."
                "%s"
                % (base, args.action, tip)
            ) from e

    if args.action == "pause":
        result = req("POST", "/v1/agent/pause")
    elif args.action == "resume":
        result = req("POST", "/v1/agent/resume")
    elif args.action == "stop":
        result = req("POST", "/v1/agent/stop")
    elif args.action == "inject":
        result = req("POST", "/v1/agent/inject", {"text": args.text, "pause": args.pause, "resume": args.resume})
    elif args.action == "clear_context":
        result = req(
            "POST",
            "/v1/agent/context/clear",
            {
                "mode": "all",
                "keep_last_system": bool(args.keep_last_system),
                "pause": bool(args.pause),
                "resume": bool(args.resume),
            },
        )
    elif args.action == "trim_context":
        result = req(
            "POST",
            "/v1/agent/context/clear",
            {
                "mode": "tail",
                "dropRounds": int(args.drop_rounds),
                "pause": bool(args.pause),
                "resume": bool(args.resume),
            },
        )
    elif args.action == "screenshot_latest":
        result = req("GET", "/v1/agent/screenshot/latest")
    elif args.action == "ocr":
        q = []
        if args.min_confidence is not None:
            q.append("minConfidence=%s" % float(args.min_confidence))
        if args.max_items is not None and int(args.max_items) > 0:
            q.append("maxItems=%d" % int(args.max_items))
        langs = _parse_ocr_langs(getattr(args, "lang", []))
        for lg in langs:
            q.append("lang=%s" % urllib.parse.quote(lg, safe="-_"))
        if bool(getattr(args, "no_auto_detect_language", False)):
            q.append("autoDetectLanguage=0")
        path = "/v1/agent/ocr"
        if q:
            path += "?" + "&".join(q)
        result = req("GET", path)
    elif args.action == "exec_actions":
        # Supervisor-side "manual control" when the worker is paused.
        acts = args.action_text or []
        if isinstance(acts, str):
            acts = [acts]
        result = req("POST", "/v1/agent/exec", {"actions": list(acts)})
    elif args.action == "run_script":
        payload = {"name": args.name or "", "path": args.path or "", "vars": _parse_vars(args.var)}
        result = req("POST", "/v1/agent/script/run", payload)
    elif args.action == "context":
        result = req("GET", "/v1/agent/context?tailRounds=%d" % int(args.tail))
    else:
        raise SystemExit(2)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _extract_keywords(text: str, *, limit: int = 6) -> List[str]:
    # ASCII-ish tokenization so it's stable across shells/encodings.
    t = (text or "").lower()
    toks = re.split(r"[^a-z0-9]+", t)
    stop = {
        "the",
        "a",
        "an",
        "and",
        "or",
        "to",
        "of",
        "in",
        "on",
        "for",
        "with",
        "at",
        "from",
        "into",
        "then",
        "open",
        "click",
        "tap",
        "scroll",
        "type",
        "iphone",
        "ios",
        "app",
        "agent",
        "worker",
    }
    out: List[str] = []
    seen = set()
    for w in toks:
        if len(w) < 3:
            continue
        if w in stop:
            continue
        if w in seen:
            continue
        seen.add(w)
        out.append(w)
        if len(out) >= limit:
            break
    return out


def cmd_diary_grep(args: argparse.Namespace) -> int:
    """
    Grep WORKER_DIARY.md using auto-extracted keywords from task text.
    This exists so Claude Code skills can stay within `python -m iphoneclaw *`.
    """
    path = os.path.abspath(args.path or "WORKER_DIARY.md")
    if not os.path.exists(path):
        print("(no WORKER_DIARY.md found at %s)" % path)
        return 0

    tail = int(args.tail)
    text = str(args.text or "")
    kws = _extract_keywords(text, limit=int(args.keywords))
    # Always include a few high-signal global tags.
    baseline = ["scroll", "wheel", "drag", "spotlight", "ime", "ascii", "type", "home"]
    # Build a single ERE pattern for grep -E.
    pat_terms = [re.escape(x) for x in (kws + baseline) if x]
    pat = "|".join(pat_terms) if pat_terms else "^DIARY\\|"

    # 1) Print the most recent DIARY lines (for quick orientation).
    try:
        raw = subprocess.run(
            ["grep", "-n", "^DIARY|", path],
            check=False,
            capture_output=True,
            text=True,
        ).stdout
        lines = [ln for ln in raw.splitlines() if ln.strip()]
        if lines:
            print("== diary tail (DIARY|) ==")
            for ln in lines[-tail:]:
                print(ln)
    except Exception:
        pass

    # 2) Keyword grep.
    try:
        out = subprocess.run(
            ["grep", "-niE", pat, path],
            check=False,
            capture_output=True,
            text=True,
        ).stdout
        hits = [ln for ln in out.splitlines() if ln.strip()]
        if hits:
            print("\n== diary hits (pattern: %s) ==" % pat)
            for ln in hits[-tail:]:
                print(ln)
        else:
            print("\n== diary hits ==")
            print("(none)")
    except Exception as e:
        print("\n== diary grep error ==")
        print(str(e))
        return 2

    return 0


def _parse_vars(kvs: List[str]) -> dict:
    out = {}
    for item in kvs or []:
        s = str(item)
        if "=" not in s:
            raise SystemExit("invalid --var, expected KEY=VALUE, got: %r" % s)
        k, v = s.split("=", 1)
        k = k.strip()
        if not k:
            raise SystemExit("invalid --var, empty key in: %r" % s)
        out[k] = v
    return out


def _parse_ocr_langs(items: List[str]) -> List[str]:
    out: List[str] = []
    seen = set()
    for raw in items or []:
        for part in str(raw).split(","):
            s = part.strip()
            if not s or s in seen:
                continue
            seen.add(s)
            out.append(s)
    return out


def _frontmost_app_name() -> str:
    try:
        from AppKit import NSWorkspace

        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        return str((app.localizedName() if app else "") or "")
    except Exception:
        return ""


def _is_target_frontmost(app_name: str) -> bool:
    front = _frontmost_app_name().strip().lower()
    if not front:
        return False
    for alias in expand_app_aliases(app_name):
        a = str(alias or "").strip().lower()
        if not a:
            continue
        if front == a or (front in a) or (a in front):
            return True
    return False


def cmd_script_run(args: argparse.Namespace) -> int:
    cfg = load_config_from_env()
    cfg.target_app = args.app
    cfg.window_contains = args.window_contains or cfg.window_contains
    cfg.dry_run = bool(args.dry_run)

    path = os.path.abspath(args.file)
    with open(path, "r", encoding="utf-8") as f:
        src = f.read()

    try:
        preds = script_to_predictions(src, vars=_parse_vars(args.var), base_dir=os.path.dirname(path))
    except ScriptParseError as e:
        print("script parse error: %s" % str(e))
        return 2
    try:
        preds = expand_special_predictions(
            preds,
            registry_path=str(getattr(cfg, "script_registry_path", "./action_scripts/registry.json")),
            max_expand_depth=8,
        )
    except Exception as e:
        print("script expand error: %s" % str(e))
        return 2

    if not preds:
        print("no actions")
        return 0

    wf = WindowFinder(app_name=args.app, window_contains=args.window_contains)
    wf.find_window()
    if _is_target_frontmost(args.app):
        print("target app already frontmost: %r" % args.app)
    else:
        for _ in range(4):
            wf.activate_app()
            time.sleep(0.25)
            if _is_target_frontmost(args.app):
                break
        if _is_target_frontmost(args.app):
            print("activated target app to frontmost: %r" % args.app)
        else:
            print(
                "warning: failed to bring target app %r to front. frontmost now=%r"
                % (args.app, _frontmost_app_name())
            )

    # Refresh once after optional activation to avoid stale bounds.
    wf.refresh()
    cap = ScreenCapture(wf)
    shot = cap.capture()

    results = []
    # Allow longer scripts; caller can keep them small, but CLI shouldn't hard-cap at 3.
    for p in preds:
        res = execute_action(cfg, p, shot)
        results.append(res)
        if not bool(res.get("ok")) and not bool(args.keep_going):
            break

    import json
    print(json.dumps({"ok": True, "count": len(results), "results": results}, ensure_ascii=False, indent=2))
    return 0


def cmd_script_record(args: argparse.Namespace) -> int:
    """
    Record a script by reading lines from stdin and writing to a .txt file.
    This is a lightweight helper so users can build reusable scripts without a full editor.
    """
    out_path = os.path.abspath(args.out)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    print(
        "recording to %s (Ctrl-D to finish)\n"
        "NOTE: this records action lines from stdin only; it does NOT capture live mouse/keyboard gestures."
        % out_path
    )
    lines: List[str] = []
    try:
        while True:
            ln = input()
            lines.append(ln)
    except EOFError:
        pass

    with open(out_path, "w", encoding="utf-8") as f:
        for ln in lines:
            f.write(ln.rstrip("\n") + "\n")
    print("wrote %d lines" % len(lines))
    if not lines:
        print(
            "hint: for executed action export, use `python -m iphoneclaw script from-run --run-dir runs/<id> --out ...`.\n"
            "for real user gesture recording, use `python -m iphoneclaw script record-user --out ...`."
        )
    return 0


def cmd_script_record_user(args: argparse.Namespace) -> int:
    """
    Record real user mouse/keyboard gestures in target window into action script lines.
    """
    from iphoneclaw.automation.user_record import LiveUserActionRecorder

    out_path = os.path.abspath(args.out)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    wf = WindowFinder(app_name=args.app, window_contains=args.window_contains)
    wf.find_window()
    wf.activate_app()
    # Align recording coordinate system with worker/ocr screenshots (auto-cropped bounds).
    wf.refresh()
    cap = ScreenCapture(wf)
    shot = cap.capture()
    b = shot.window_bounds

    seconds = max(0.0, float(args.seconds or 0.0))
    recorder = LiveUserActionRecorder(
        bounds=b,
        coord_factor=int(args.coord_factor),
        min_sleep_ms=int(args.min_sleep_ms),
        max_sleep_ms=int(args.max_sleep_ms),
        drag_threshold_px=float(args.drag_threshold_px),
        include_keyboard=not bool(args.no_keyboard),
    )

    print(
        "recording real user actions to %s\n"
        "target window: app=%r bounds=(%.1f, %.1f, %.1f, %.1f)\n"
        "stop: %s\n"
        "notes: only events inside target window are captured; cmd+1/cmd+2 become iphone_home()/iphone_app_switcher()."
        % (
            out_path,
            args.app,
            b.x,
            b.y,
            b.width,
            b.height,
            ("auto after %.1fs" % seconds) if seconds > 0 else "Ctrl-C",
        )
    )
    if shot.crop_rect_px:
        print(
            "using cropped bounds for recording: crop_rect_px=%s (raw %dx%d)"
            % (shot.crop_rect_px, shot.raw_image_width, shot.raw_image_height)
        )

    try:
        actions = recorder.record(seconds=seconds)
    except RuntimeError as e:
        print("record-user error: %s" % str(e))
        return 2

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(
            "# Recorded by: python -m iphoneclaw script record-user --app %s\n"
            % str(args.app)
        )
        for a in actions:
            f.write(a.rstrip("\n") + "\n")

    print("wrote %s (%d actions)" % (out_path, len(actions)))
    if not actions:
        print(
            "hint: grant Accessibility permission to your terminal/python and make sure you interact inside the target window."
        )
    return 0


def cmd_script_from_run(args: argparse.Namespace) -> int:
    """
    Export raw executed actions from runs/<id>/events.jsonl into a replayable script.
    """
    run_dir = os.path.abspath(args.run_dir)
    events_path = os.path.join(run_dir, "events.jsonl")
    if not os.path.exists(events_path):
        raise SystemExit("events.jsonl not found: %s" % events_path)

    out_path = os.path.abspath(args.out)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    actions: List[str] = []
    import json

    with open(events_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            t = str(obj.get("type") or "")
            if t == "exec":
                data = obj.get("data") or {}
                raw = str(data.get("raw_action") or "").strip()
                if raw:
                    actions.append(raw)
            elif t == "supervisor_exec" and bool(args.include_supervisor_exec):
                data = obj.get("data") or {}
                acts = data.get("actions")
                if isinstance(acts, str):
                    acts = [acts]
                if isinstance(acts, list):
                    for a in acts:
                        a = str(a).strip()
                        if a:
                            actions.append(a)

    if not actions:
        print("no actions found in %s" % events_path)
        return 0

    with open(out_path, "w", encoding="utf-8") as f:
        f.write("# Exported from %s\n" % run_dir)
        for a in actions:
            f.write(a.rstrip("\n") + "\n")
    print("wrote %s (%d actions)" % (out_path, len(actions)))
    return 0




def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="iphoneclaw")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_doctor = sub.add_parser("doctor", help="Check macOS permissions.")
    p_doctor.set_defaults(func=cmd_doctor)

    p_launch = sub.add_parser("launch", help="Launch target app and print window bounds.")
    _add_common_args(p_launch)
    p_launch.set_defaults(func=cmd_launch)

    p_bounds = sub.add_parser("bounds", help="Print window bounds (x y w h).")
    _add_common_args(p_bounds)
    p_bounds.set_defaults(func=cmd_bounds)

    p_shot = sub.add_parser("screenshot", help="Capture target window to a JPEG file.")
    _add_common_args(p_shot)
    p_shot.add_argument("--out", default=None, help="Output path (default: ./screenshot.jpg).")
    p_shot.set_defaults(func=cmd_screenshot)

    p_cal = sub.add_parser("calibrate", help="Capture a screenshot and print coordinate mapping info.")
    _add_common_args(p_cal)
    p_cal.add_argument("--out-dir", default=None, help="Output directory (default: current directory).")
    p_cal.set_defaults(func=cmd_calibrate)

    p_ocr = sub.add_parser("ocr", help="Run Apple Vision OCR on current target window.")
    _add_common_args(p_ocr)
    p_ocr.add_argument(
        "--min-confidence",
        default=0.0,
        type=float,
        help="Keep text items with confidence >= value (0..1).",
    )
    p_ocr.add_argument(
        "--max-items",
        default=0,
        type=int,
        help="Limit returned OCR items (0 means no limit).",
    )
    p_ocr.add_argument(
        "--coord-factor",
        default=Config().coord_factor,
        type=int,
        help="Model coordinate factor for model_box output (default from config; usually 1000).",
    )
    p_ocr.add_argument(
        "--lang",
        action="append",
        default=[],
        help="OCR language tag (repeatable), e.g. --lang zh-Hans --lang en-US. Supports comma-separated values too.",
    )
    p_ocr.add_argument(
        "--no-auto-detect-language",
        action="store_true",
        help="Disable Vision automatic language detection.",
    )
    p_ocr.add_argument(
        "--debug-draw",
        action="store_true",
        help="Draw OCR text boxes on screenshot and save debug artifacts.",
    )
    p_ocr.add_argument(
        "--debug-dir",
        default="./ocr_debug",
        help="Output directory for OCR debug artifacts (used with --debug-draw).",
    )
    p_ocr.add_argument(
        "--debug-prefix",
        default="ocr",
        help="Filename prefix for OCR debug artifacts (used with --debug-draw).",
    )
    p_ocr.set_defaults(func=cmd_ocr)

    p_win = sub.add_parser("windows", help="Debug: list visible windows from CGWindowList.")
    p_win.add_argument("--contains", default="", help="Case-insensitive substring filter across owner/title.")
    p_win.add_argument("--limit", default=30, type=int, help="Max rows to print.")
    p_win.set_defaults(func=cmd_windows)

    p_run = sub.add_parser("run", help="Run the worker loop (and supervisor API).")
    _add_common_args(p_run)
    p_run.add_argument("--instruction", required=True, help="Task instruction for the agent.")
    p_run.add_argument("--base-url", default=None, help="Model base URL (OpenAI-compatible).")
    p_run.add_argument("--api-key", default=None, help="Model API key.")
    p_run.add_argument("--model", default=None, help="Model name (UniTAR).")
    p_run.add_argument("--max-tokens", default=None, type=int, help="max_tokens for model output.")
    p_run.add_argument("--temperature", default=None, type=float, help="temperature for model.")
    p_run.add_argument("--top-p", default=None, type=float, help="top_p for model.")
    p_run.add_argument(
        "--volc-thinking-type",
        default="",
        help="Volcengine Ark only: thinking.type (disabled|enabled).",
    )
    p_run.add_argument("--dry-run", action="store_true", help="Parse actions but do not execute.")
    ap = p_run.add_mutually_exclusive_group()
    ap.add_argument(
        "--auto-pause-on-user-input",
        action="store_true",
        help="Auto-pause when you move mouse/press keys (emits SSE auto_pause).",
    )
    ap.add_argument(
        "--no-auto-pause-on-user-input",
        action="store_true",
        help="Disable auto-pause on user input (overrides env).",
    )
    p_run.add_argument(
        "--scroll-mode",
        default="",
        help="Scroll mode: wheel (default) or drag (iOS-style swipe).",
    )
    p_run.add_argument(
        "--scroll-unit",
        default="",
        help="Wheel unit: pixel (default) or line.",
    )
    p_run.add_argument(
        "--scroll-amount",
        default=None,
        type=int,
        help="Scroll magnitude per action (pixels or lines depending on --scroll-unit).",
    )
    p_run.add_argument(
        "--scroll-repeat",
        default=None,
        type=int,
        help="Split scroll into N smaller wheel events (default from config).",
    )
    p_run.add_argument(
        "--scroll-focus-click",
        default=None,
        type=int,
        help="1/0: click to focus before scrolling (default from config).",
    )
    p_run.add_argument("--record-dir", default=None, help="Directory to store runs (default: ./runs).")
    p_run.add_argument("--host", default=None, help="Supervisor host (default: 127.0.0.1).")
    p_run.add_argument("--port", default=None, help="Supervisor port (default: 17334).")
    p_run.add_argument("--token", default=None, help="Supervisor bearer token.")
    p_run.set_defaults(func=cmd_run)

    p_serve = sub.add_parser("serve", help="Start supervisor API server only (no worker).")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", default=17334, type=int)
    p_serve.add_argument("--token", default="")
    p_serve.set_defaults(func=cmd_serve)

    p_ctl = sub.add_parser("ctl", help="Control/inspect a running worker via supervisor API.")
    p_ctl.add_argument("--base", default=None, help="Supervisor base URL, e.g. http://127.0.0.1:17334")
    p_ctl.add_argument("--token", default=None, help="Bearer token")
    p_ctl_sub = p_ctl.add_subparsers(dest="action", required=True)
    for name in ("pause", "resume", "stop"):
        sp = p_ctl_sub.add_parser(name)
        sp.set_defaults(action=name)
    sp_inj = p_ctl_sub.add_parser("inject")
    sp_inj.add_argument("--text", required=True)
    sp_inj.add_argument("--pause", action="store_true")
    sp_inj.add_argument("--resume", action="store_true")
    sp_inj.set_defaults(action="inject")
    sp_ctx = p_ctl_sub.add_parser("context")
    sp_ctx.add_argument("--tail", default=5, type=int)
    sp_ctx.set_defaults(action="context")

    sp_clr = p_ctl_sub.add_parser("clear-context", help="Clear ALL worker conversation context (keeps last system by default).")
    sp_clr.add_argument("--keep-last-system", action="store_true", default=True)
    sp_clr.add_argument("--drop-system", action="store_false", dest="keep_last_system")
    sp_clr.add_argument("--pause", action="store_true", help="Pause before clearing.")
    sp_clr.add_argument("--resume", action="store_true", help="Resume after clearing.")
    sp_clr.set_defaults(action="clear_context")

    sp_trim = p_ctl_sub.add_parser("trim-context", help="Drop the most recent N assistant rounds from conversation context.")
    sp_trim.add_argument("--drop-rounds", default=1, type=int, help="Number of assistant rounds to drop.")
    sp_trim.add_argument("--pause", action="store_true", help="Pause before trimming.")
    sp_trim.add_argument("--resume", action="store_true", help="Resume after trimming.")
    sp_trim.set_defaults(action="trim_context")
    p_ctl.set_defaults(func=cmd_ctl)

    sp_sh = p_ctl_sub.add_parser("screenshot-latest", help="Get latest screenshot path (requires images enabled).")
    sp_sh.set_defaults(action="screenshot_latest")

    sp_ocr = p_ctl_sub.add_parser("ocr", help="Run Apple Vision OCR on current screen via supervisor API.")
    sp_ocr.add_argument("--min-confidence", default=0.0, type=float, help="Keep text items with confidence >= value (0..1).")
    sp_ocr.add_argument("--max-items", default=0, type=int, help="Limit returned OCR items (0 means no limit).")
    sp_ocr.add_argument(
        "--lang",
        action="append",
        default=[],
        help="OCR language tag (repeatable), e.g. --lang zh-Hans --lang en-US. Supports comma-separated values too.",
    )
    sp_ocr.add_argument(
        "--no-auto-detect-language",
        action="store_true",
        help="Disable Vision automatic language detection.",
    )
    sp_ocr.set_defaults(action="ocr")

    sp_ex = p_ctl_sub.add_parser("exec", help="Execute actions directly (requires exec enabled, worker paused).")
    sp_ex.add_argument(
        "--action",
        dest="action_text",
        action="append",
        default=[],
        help="Action call string, e.g. click(start_box='(500,500)'); repeatable.",
    )
    sp_ex.set_defaults(action="exec_actions")

    sp_rs = p_ctl_sub.add_parser("run-script", help="Run a registered script (or a script path) while worker is paused.")
    sp_rs.add_argument("--name", default="", help="Registry short name (see action_scripts/registry.json).")
    sp_rs.add_argument("--path", default="", help="Script file path (overrides --name).")
    sp_rs.add_argument("--var", action="append", default=[], help="Template vars KEY=VALUE (for ${KEY}).")
    sp_rs.set_defaults(action="run_script")

    p_diary = sub.add_parser("diary", help="Supervisor diary helpers (grep-friendly).")
    p_diary_sub = p_diary.add_subparsers(dest="action", required=True)
    p_dg = p_diary_sub.add_parser("grep", help="Grep WORKER_DIARY.md using auto keywords from task text.")
    p_dg.add_argument("--text", required=True, help="Task/instruction text (e.g. $ARGUMENTS).")
    p_dg.add_argument("--path", default="WORKER_DIARY.md", help="Diary path (default: ./WORKER_DIARY.md).")
    p_dg.add_argument("--tail", default=30, type=int, help="Max lines to print per section.")
    p_dg.add_argument("--keywords", default=6, type=int, help="Max auto-extracted keywords.")
    p_dg.set_defaults(func=cmd_diary_grep)

    p_script = sub.add_parser("script", help="Action script helpers (run/record/record-user/from-run).")
    p_script_sub = p_script.add_subparsers(dest="action", required=True)

    sp_run = p_script_sub.add_parser("run", help="Run an action script (.txt) against the target window.")
    _add_common_args(sp_run)
    sp_run.add_argument("--file", required=True, help="Script path (txt).")
    sp_run.add_argument("--var", action="append", default=[], help="Template vars KEY=VALUE (for ${KEY}).")
    sp_run.add_argument("--dry-run", action="store_true", help="Parse but do not execute.")
    sp_run.add_argument("--keep-going", action="store_true", help="Continue even if an action fails.")
    sp_run.set_defaults(func=cmd_script_run)

    sp_rec = p_script_sub.add_parser(
        "record",
        help="Record a script by reading action lines from stdin (not live mouse/keyboard capture).",
    )
    sp_rec.add_argument("--out", required=True, help="Output script path.")
    sp_rec.set_defaults(func=cmd_script_record)

    sp_rec_user = p_script_sub.add_parser(
        "record-user",
        help="Record real user mouse/keyboard gestures inside target window.",
    )
    _add_common_args(sp_rec_user)
    sp_rec_user.add_argument("--out", required=True, help="Output script path.")
    sp_rec_user.add_argument(
        "--seconds",
        default=0.0,
        type=float,
        help="Auto-stop after N seconds (0 means run until Ctrl-C).",
    )
    sp_rec_user.add_argument(
        "--coord-factor",
        default=Config().coord_factor,
        type=int,
        help="Model coordinate factor (default from config; usually 1000).",
    )
    sp_rec_user.add_argument(
        "--min-sleep-ms",
        default=180,
        type=int,
        help="Insert sleep(ms=...) only when action gap >= this threshold.",
    )
    sp_rec_user.add_argument(
        "--max-sleep-ms",
        default=2000,
        type=int,
        help="Cap auto-inserted sleep(ms=...) to this value.",
    )
    sp_rec_user.add_argument(
        "--drag-threshold-px",
        default=18.0,
        type=float,
        help="Pointer move threshold to treat left mouse as drag instead of click.",
    )
    sp_rec_user.add_argument(
        "--no-keyboard",
        action="store_true",
        help="Do not record hotkeys (mouse/scroll only).",
    )
    sp_rec_user.set_defaults(func=cmd_script_record_user)

    sp_fr = p_script_sub.add_parser("from-run", help="Export executed actions from runs/<id>/events.jsonl.")
    sp_fr.add_argument("--run-dir", required=True, help="Run directory (e.g. runs/20260208_154607).")
    sp_fr.add_argument("--out", required=True, help="Output script path.")
    sp_fr.add_argument("--include-supervisor-exec", action="store_true", help="Include supervisor_exec actions too.")
    sp_fr.set_defaults(func=cmd_script_from_run)

    return p


def main(argv: Optional[List[str]] = None) -> None:
    import logging
    logging.basicConfig(level=logging.WARNING, format="%(name)s: %(message)s")

    parser = build_parser()
    args = parser.parse_args(argv)
    rc = int(args.func(args))  # type: ignore[attr-defined]
    raise SystemExit(rc)
