from __future__ import annotations

import json
import queue
import threading
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional

from iphoneclaw.config import Config
from iphoneclaw.agent.recorder import RunRecorder
from iphoneclaw.macos.capture import ScreenCapture
from iphoneclaw.macos.window import WindowFinder
from iphoneclaw.agent.executor import execute_action
from iphoneclaw.parse.action_parser import parse_predictions
from iphoneclaw.automation.action_script import expand_special_predictions
from iphoneclaw.supervisor.hub import SupervisorHub
from iphoneclaw.supervisor.state import WorkerControl


def _json_bytes(obj: Any) -> bytes:
    return (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")


class SupervisorHTTPServer:
    def __init__(
        self,
        config: Config,
        hub: SupervisorHub,
        control: WorkerControl,
        conversation_store,
        recorder: Optional[RunRecorder] = None,
    ) -> None:
        self.config = config
        self.hub = hub
        self.control = control
        self.conversation_store = conversation_store
        self.recorder = recorder
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        host = self.config.supervisor_host
        port = int(self.config.supervisor_port)

        outer = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "iphoneclaw-supervisor/0.1"

            def _auth_ok(self) -> bool:
                token = outer.config.supervisor_token or ""
                if not token:
                    return True
                got = self.headers.get("Authorization", "")
                return got.strip() == f"Bearer {token}"

            def _send(self, status: int, body: bytes, content_type: str = "application/json") -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _send_json(self, status: int, obj: Any) -> None:
                self._send(status, _json_bytes(obj))

            def do_GET(self) -> None:
                if not self._auth_ok():
                    self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                    return

                parsed = urllib.parse.urlparse(self.path)
                path = parsed.path
                qs = urllib.parse.parse_qs(parsed.query or "")

                if path == "/health":
                    self._send_json(HTTPStatus.OK, {"ok": True, "ts": time.time()})
                    return

                if path == "/v1/agent/context":
                    tail = int((qs.get("tailRounds") or ["5"])[0])
                    ctx = outer.conversation_store.tail_rounds(tail)
                    self._send_json(
                        HTTPStatus.OK,
                        {
                            "status": outer.control.snapshot(),
                            "context": ctx,
                        },
                    )
                    return

                if path == "/v1/agent/run":
                    rid = getattr(outer.recorder, "run_id", "") if outer.recorder else ""
                    root = getattr(outer.recorder, "root", "") if outer.recorder else ""
                    self._send_json(HTTPStatus.OK, {"ok": True, "run_id": rid, "root": root})
                    return

                if path == "/v1/agent/screenshot/latest":
                    if not bool(getattr(outer.config, "enable_supervisor_images", False)):
                        self._send_json(HTTPStatus.FORBIDDEN, {"error": "images disabled"})
                        return
                    if not outer.recorder:
                        self._send_json(HTTPStatus.NOT_FOUND, {"error": "no recorder"})
                        return
                    step = outer.recorder.latest_step()
                    if not step:
                        self._send_json(HTTPStatus.NOT_FOUND, {"error": "no steps yet"})
                        return
                    d = outer.recorder.step_dir(step)
                    jpg = d + "/screenshot.jpg"
                    self._send_json(HTTPStatus.OK, {"ok": True, "step": step, "path": jpg})
                    return

                if path == "/v1/agent/events":
                    # SSE: keep the connection open and stream events.
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Connection", "keep-alive")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()

                    q = outer.hub.subscribe()
                    try:
                        # Initial status push
                        init = {"type": "status", "data": outer.control.snapshot(), "ts": time.time()}
                        self.wfile.write(b"event: status\n")
                        self.wfile.write(b"data: " + json.dumps(init).encode("utf-8") + b"\n\n")
                        self.wfile.flush()

                        while True:
                            try:
                                evt = q.get(timeout=15)
                            except queue.Empty:
                                # heartbeat
                                self.wfile.write(b": ping\n\n")
                                self.wfile.flush()
                                continue

                            payload = {"type": evt.type, "data": evt.data, "ts": evt.ts}
                            self.wfile.write(("event: %s\n" % evt.type).encode("utf-8"))
                            self.wfile.write(b"data: " + json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n\n")
                            self.wfile.flush()
                    except Exception:
                        return
                    finally:
                        outer.hub.unsubscribe(q)
                    return

                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

            def do_OPTIONS(self) -> None:
                # CORS preflight support
                self.send_response(HTTPStatus.NO_CONTENT)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_POST(self) -> None:
                if not self._auth_ok():
                    self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                    return

                parsed = urllib.parse.urlparse(self.path)
                path = parsed.path

                length = int(self.headers.get("Content-Length") or 0)
                if length > 1_048_576:  # 1 MB
                    self._send_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "body too large"})
                    return
                raw = self.rfile.read(length) if length > 0 else b""
                body: Dict[str, Any] = {}
                if raw:
                    try:
                        body = json.loads(raw.decode("utf-8"))
                    except Exception:
                        body = {}

                if path == "/v1/agent/pause":
                    outer.control.pause()
                    outer.hub.set_status(outer.control.snapshot()["status"])
                    self._send_json(HTTPStatus.OK, {"ok": True, "status": outer.control.snapshot()})
                    return

                if path == "/v1/agent/resume":
                    outer.control.resume()
                    outer.hub.set_status(outer.control.snapshot()["status"])
                    self._send_json(HTTPStatus.OK, {"ok": True, "status": outer.control.snapshot()})
                    return

                if path == "/v1/agent/stop":
                    outer.control.stop()
                    outer.hub.set_status(outer.control.snapshot()["status"])
                    self._send_json(HTTPStatus.OK, {"ok": True, "status": outer.control.snapshot()})
                    return

                if path == "/v1/agent/inject":
                    text = str(body.get("text") or "")
                    if text:
                        outer.control.inject(text)
                        outer.hub.publish("inject", {"len": len(text)})

                    if bool(body.get("pause")):
                        outer.control.pause()
                    if bool(body.get("resume")):
                        outer.control.resume()
                    outer.hub.set_status(outer.control.snapshot()["status"])

                    self._send_json(HTTPStatus.OK, {"ok": True, "status": outer.control.snapshot()})
                    return

                if path == "/v1/agent/context/clear":
                    # Optional safety gates.
                    if bool(body.get("pause")):
                        outer.control.pause()
                    mode = str(body.get("mode") or "all").strip().lower()
                    removed = 0
                    if mode in ("all", "clear"):
                        keep_sys = bool(body.get("keep_last_system", True))
                        removed = int(outer.conversation_store.clear(keep_last_system=keep_sys))
                        outer.hub.publish("context_cleared", {"mode": "all", "removed": removed, "keep_last_system": keep_sys})
                    elif mode in ("tail", "trim", "drop"):
                        drop_rounds = int(body.get("dropRounds") or body.get("tailRounds") or 1)
                        removed = int(outer.conversation_store.trim_tail_rounds(drop_rounds))
                        outer.hub.publish("context_cleared", {"mode": "tail", "dropRounds": drop_rounds, "removed": removed})
                    else:
                        self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid mode", "mode": mode})
                        return

                    if bool(body.get("resume")):
                        outer.control.resume()
                    outer.hub.set_status(outer.control.snapshot()["status"])
                    self._send_json(
                        HTTPStatus.OK,
                        {"ok": True, "status": outer.control.snapshot(), "removed": removed},
                    )
                    return

                if path == "/v1/agent/exec":
                    if not bool(getattr(outer.config, "enable_supervisor_exec", False)):
                        self._send_json(HTTPStatus.FORBIDDEN, {"error": "exec disabled"})
                        return
                    snap = outer.control.snapshot()
                    if not bool(snap.get("paused")):
                        self._send_json(HTTPStatus.CONFLICT, {"error": "worker must be paused to exec"})
                        return

                    actions = body.get("actions")
                    if isinstance(actions, str):
                        actions = [actions]
                    if not isinstance(actions, list) or not actions:
                        self._send_json(HTTPStatus.BAD_REQUEST, {"error": "missing actions"})
                        return
                    # Parse using the same parser the worker uses.
                    joined = "\n".join(str(x) for x in actions if str(x).strip())
                    preds = parse_predictions("Action: " + joined)
                    preds = [p for p in preds if p.action_type != "error_env"]
                    try:
                        preds = expand_special_predictions(
                            preds,
                            registry_path=str(
                                getattr(outer.config, "script_registry_path", "./action_scripts/registry.json")
                            ),
                        )
                    except Exception as e:
                        self._send_json(
                            HTTPStatus.BAD_REQUEST,
                            {"error": "run_script expand failed: %s" % str(e)},
                        )
                        return
                    if not preds:
                        self._send_json(HTTPStatus.BAD_REQUEST, {"error": "unparseable actions"})
                        return

                    try:
                        wf = WindowFinder(app_name=outer.config.target_app, window_contains=outer.config.window_contains)
                        wf.find_window()
                        cap = ScreenCapture(wf)
                        shot = cap.capture()
                        wf.activate_app()
                        results = []
                        # For safety: cap total actions per request.
                        max_actions = int(getattr(outer.config, "supervisor_exec_max_actions", 50) or 50)
                        for p in preds[:max_actions]:
                            res = execute_action(outer.config, p, shot)
                            results.append(res)
                        outer.hub.publish("supervisor_exec", {"count": len(results)})
                        if outer.recorder:
                            outer.recorder.log_event("supervisor_exec", {"actions": actions, "results": results})
                        self._send_json(HTTPStatus.OK, {"ok": True, "results": results})
                        return
                    except Exception as e:
                        if outer.recorder:
                            outer.recorder.log_event("supervisor_exec_error", {"error": str(e)})
                        self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(e)})
                        return

                if path == "/v1/agent/script/run":
                    if not bool(getattr(outer.config, "enable_supervisor_exec", False)):
                        self._send_json(HTTPStatus.FORBIDDEN, {"error": "exec disabled"})
                        return
                    snap = outer.control.snapshot()
                    if not bool(snap.get("paused")):
                        self._send_json(HTTPStatus.CONFLICT, {"error": "worker must be paused to run script"})
                        return

                    name = body.get("name")
                    script_path = body.get("path")
                    vars_in = body.get("vars") or {}
                    if not isinstance(vars_in, dict):
                        self._send_json(HTTPStatus.BAD_REQUEST, {"error": "vars must be an object"})
                        return

                    if isinstance(script_path, str) and script_path.strip():
                        raw = "run_script(path=%r, vars=%r)" % (script_path.strip(), vars_in)
                    elif isinstance(name, str) and name.strip():
                        raw = "run_script(name=%r, vars=%r)" % (name.strip(), vars_in)
                    else:
                        self._send_json(HTTPStatus.BAD_REQUEST, {"error": "missing name/path"})
                        return

                    preds = parse_predictions("Action: " + raw)
                    preds = [p for p in preds if p.action_type != "error_env"]
                    try:
                        preds = expand_special_predictions(
                            preds,
                            registry_path=str(
                                getattr(outer.config, "script_registry_path", "./action_scripts/registry.json")
                            ),
                        )
                    except Exception as e:
                        self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(e)})
                        return
                    preds = [p for p in preds if p.action_type != "error_env"]
                    if not preds:
                        self._send_json(HTTPStatus.BAD_REQUEST, {"error": "script produced no actions"})
                        return

                    try:
                        wf = WindowFinder(app_name=outer.config.target_app, window_contains=outer.config.window_contains)
                        wf.find_window()
                        cap = ScreenCapture(wf)
                        shot = cap.capture()
                        wf.activate_app()
                        max_actions = int(getattr(outer.config, "supervisor_exec_max_actions", 50) or 50)
                        results = []
                        for p in preds[:max_actions]:
                            res = execute_action(outer.config, p, shot)
                            results.append(res)
                            if not bool(res.get("ok")):
                                break
                        outer.hub.publish("supervisor_exec", {"count": len(results), "script": name or script_path})
                        if outer.recorder:
                            outer.recorder.log_event("supervisor_script_run", {"raw": raw, "results": results})
                        self._send_json(HTTPStatus.OK, {"ok": True, "raw": raw, "results": results})
                        return
                    except Exception as e:
                        if outer.recorder:
                            outer.recorder.log_event("supervisor_exec_error", {"error": str(e)})
                        self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(e)})
                        return

                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

            def log_message(self, fmt: str, *args: Any) -> None:
                # Keep stdout clean by default.
                return

        self._httpd = ThreadingHTTPServer((host, port), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        self._thread = None
