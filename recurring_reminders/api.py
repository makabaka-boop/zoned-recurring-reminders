"""Minimal JSON HTTP API (stdlib only).

Endpoints
  POST /series                      create a series
  GET  /series/{id}                 series with all rule versions + exceptions
  POST /series/{id}/versions        modify series (new version, effective_from)
  POST /series/{id}/exceptions      add/replace a single-day exception
  GET  /series/{id}/occurrences?from=YYYY-MM-DD&to=YYYY-MM-DD
  POST /series/{id}/confirm         {"date": "YYYY-MM-DD"} freeze an instance
  POST /tick                        run generator; optional {"now": ISO} sets a
                                    ManualClock first (controllable clock)
  GET  /reminders?series_id=N       list reminders
  GET  /runs                        generator audit log
"""
from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .clock import ManualClock
from .service import Service, parse_utc


class ApiError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _make_handler(service: Service):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):  # keep tests quiet
            pass

        # ---------------------------------------------------------- helpers
        def _send(self, code: int, obj) -> None:
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b"{}"
            try:
                data = json.loads(raw or b"{}")
            except json.JSONDecodeError as exc:
                raise ApiError(400, f"invalid JSON body: {exc}")
            if not isinstance(data, dict):
                raise ApiError(400, "JSON body must be an object")
            return data

        def _dispatch(self, method: str) -> None:
            parsed = urlparse(self.path)
            path, qs = parsed.path, parse_qs(parsed.query)
            body = self._body() if method == "POST" else {}

            if method == "POST" and path == "/series":
                sid = service.create_series(**body)
                return self._send(201, service.get_series(sid))

            m = re.fullmatch(r"/series/(\d+)", path)
            if method == "GET" and m:
                return self._send(200, service.get_series(int(m.group(1))))

            m = re.fullmatch(r"/series/(\d+)/versions", path)
            if method == "POST" and m:
                eff = body.pop("effective_from", None)
                if eff is None:
                    raise ApiError(400, "effective_from is required")
                version = service.modify_series(int(m.group(1)), effective_from=eff, **body)
                return self._send(201, {"series_id": int(m.group(1)), "version": version})

            m = re.fullmatch(r"/series/(\d+)/exceptions", path)
            if method == "POST" and m:
                ex_id = service.add_exception(int(m.group(1)), **body)
                return self._send(201, {"exception_id": ex_id})

            m = re.fullmatch(r"/series/(\d+)/occurrences", path)
            if method == "GET" and m:
                frm, to = qs.get("from", [None])[0], qs.get("to", [None])[0]
                if not frm or not to:
                    raise ApiError(400, "from= and to= query params are required")
                return self._send(200, {"occurrences": service.list_occurrences(int(m.group(1)), frm, to)})

            m = re.fullmatch(r"/series/(\d+)/confirm", path)
            if method == "POST" and m:
                if "date" not in body:
                    raise ApiError(400, "date is required")
                return self._send(200, service.confirm_occurrence(int(m.group(1)), body["date"]))

            if method == "POST" and path == "/tick":
                if "now" in body:
                    if not isinstance(service.clock, ManualClock):
                        raise ApiError(400, "service clock is not controllable")
                    service.clock.set(parse_utc(body["now"]))
                return self._send(200, service.tick())

            if method == "GET" and path == "/reminders":
                sid = qs.get("series_id", [None])[0]
                return self._send(200, {"reminders": service.list_reminders(int(sid) if sid else None)})

            if method == "GET" and path == "/runs":
                return self._send(200, {"runs": service.generator_runs()})

            if method == "GET" and path == "/health":
                return self._send(200, {"ok": True})

            raise ApiError(404, f"no route for {method} {path}")

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

        def _handle(self, method: str) -> None:
            try:
                self._dispatch(method)
            except ApiError as exc:
                self._send(exc.code, {"error": exc.message})
            except ValueError as exc:
                self._send(400, {"error": str(exc)})
            except Exception as exc:  # pragma: no cover - defensive
                self._send(500, {"error": f"{type(exc).__name__}: {exc}"})

    return Handler


def make_server(service: Service, host: str = "127.0.0.1", port: int = 8080) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), _make_handler(service))
