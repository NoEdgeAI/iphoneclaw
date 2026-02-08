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
    ) -> None:
        self.config = config
        self.hub = hub
        self.control = control
        self.conversation_store = conversation_store
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
