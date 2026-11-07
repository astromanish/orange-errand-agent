"""Local side-by-side Web UI & HTTP server for the Errand Agent demo (Deliverable #6).

Serves a split-screen interface on http://127.0.0.1:8765:
- Left pane: Chat conversation, Quick Scenario Prompts, Live Fault-Injection Controls
  (Force Timeout-After-Success, Force Price Drift, Simulate Provider Cancellation, Reset),
  and Active Bookings & Stored Memories inspector.
- Right pane: Real-time Observability Trace (State Machine badge, Approval Gate status,
  Context Budget meter, and step-by-step Tool & Model Calls with sanitized outputs).
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from errand_agent.agent import ErrandAgent, ExecutionLimits
from errand_agent.context import seed_fifty_plus_memories_and_long_history
from errand_agent.store import ErrandStore
from errand_agent.tools import ScenarioConfig, ToolEnvironment


WEB_DIR = Path(__file__).resolve().parent / "web"


class DemoRuntime:
    """Holds the live stateful agent, store, and fault-injection config for the demo UI."""

    def __init__(self, db_path: str = ":memory:"):
        self.db_path = db_path
        self.reset(seed_memories=True)

    def reset(self, seed_memories: bool = True) -> dict[str, Any]:
        if hasattr(self, "store") and self.store:
            try:
                self.store.close()
            except Exception:
                pass
        self.store = ErrandStore(self.db_path)
        self.config = ScenarioConfig(seed=42, force_book_outcome="success")
        self.env = ToolEnvironment(self.store, self.config, default_user_id="user_demo")
        self.agent = ErrandAgent(
            self.store,
            self.env,
            user_id="user_demo",
            session_id="sess_live_demo",
            limits=ExecutionLimits(max_model_turns=5, max_tool_calls=6, max_wall_clock_seconds=15.0),
            token_budget=420,
        )
        # Seed a completed electrician booking so "Book the same electrician as last time" works out of the box
        self.env.seed_booking("user_demo", "prov_elec_01", "2026-10-20T08:00", status="completed", booking_id="bk_past_elec")
        if seed_memories:
            seed_fifty_plus_memories_and_long_history(
                self.store, user_id="user_demo", session_id=self.agent.session_id
            )
        return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        bks = self.env.list_bookings("user_demo").get("bookings", [])
        mems = self.store.list_memories("user_demo", include_forgotten=True, only_confirmed=False)
        traces = self.store.get_traces(self.agent.session_id)
        approvals = [
            dict(r)
            for r in self.store._conn.execute(
                "SELECT * FROM approvals WHERE user_id = ? ORDER BY granted_at DESC, rowid DESC LIMIT 5",
                ("user_demo",),
            ).fetchall()
        ]
        return {
            "session_id": self.agent.session_id,
            "user_id": "user_demo",
            "state": asdict(self.agent.state),
            "fault_config": {
                "force_book_outcome": self.config.force_book_outcome,
                "hold_price_delta": self.config.hold_price_delta,
                "token_budget": self.agent.packer.max_tokens,
            },
            "bookings": bks,
            "memories_summary": {
                "total": len(mems),
                "active_confirmed": sum(1 for m in mems if m["status"] == "active" and m["confirmed"]),
                "unconfirmed_inferred": sum(1 for m in mems if m["status"] == "active" and not m["confirmed"]),
                "forgotten": sum(1 for m in mems if m["status"] == "forgotten"),
                "items": mems[:12],
            },
            "approvals": approvals,
            "traces": traces,
        }


RUNTIME = DemoRuntime()


class DemoHTTPHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        pass  # keep console clean

    def _send_json(self, data: dict[str, Any], status: int = 200) -> None:
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        return json.loads(raw) if raw else {}

    def do_GET(self) -> None:
        if self.path in ("/", "/index.html"):
            html_file = WEB_DIR / "index.html"
            content = html_file.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            return

        if self.path == "/api/state":
            self._send_json(RUNTIME.snapshot())
            return

        self._send_json({"error": "Not found"}, status=404)

    def do_POST(self) -> None:
        body = self._read_json()

        if self.path == "/api/chat":
            msg = str(body.get("message") or "").strip()
            if not msg:
                self._send_json({"error": "Empty message"}, status=400)
                return
            turn_res = RUNTIME.agent.handle_turn(msg)
            self._send_json(
                {
                    "turn": asdict(turn_res),
                    "snapshot": RUNTIME.snapshot(),
                }
            )
            return

        if self.path == "/api/faults":
            if "force_book_outcome" in body:
                RUNTIME.config.force_book_outcome = body["force_book_outcome"]
            if "hold_price_delta" in body:
                RUNTIME.config.hold_price_delta = int(body["hold_price_delta"])
            # If mutate_active_hold_price is passed, simulate mid-approval price change in DB!
            if "mutate_active_hold_price" in body and RUNTIME.agent.state.active_hold_id:
                new_p = int(body["mutate_active_hold_price"])
                RUNTIME.store._conn.execute(
                    "UPDATE holds SET quoted_price = ? WHERE hold_id = ?",
                    (new_p, RUNTIME.agent.state.active_hold_id),
                )
                RUNTIME.store._conn.commit()
            self._send_json({"ok": True, "snapshot": RUNTIME.snapshot()})
            return

        if self.path == "/api/proactive-cancel":
            bid = body.get("booking_id")
            if not bid:
                bks = [
                    b for b in RUNTIME.env.list_bookings("user_demo")["bookings"]
                    if b["status"] == "confirmed"
                ]
                if bks:
                    bid = bks[0]["booking_id"]
            if not bid:
                self._send_json({"error": "No confirmed booking to cancel"}, status=400)
                return
            RUNTIME.env.simulate_provider_cancellation_event(bid)
            pro_res = RUNTIME.agent.handle_proactive_cancellation(bid)
            self._send_json(
                {
                    "turn": asdict(pro_res),
                    "snapshot": RUNTIME.snapshot(),
                }
            )
            return

        if self.path == "/api/reset":
            snap = RUNTIME.reset(seed_memories=True)
            self._send_json({"ok": True, "snapshot": snap})
            return

        self._send_json({"error": "Unknown endpoint"}, status=404)


def run_server(host: str = "127.0.0.1", port: int = 8765) -> None:
    server = ThreadingHTTPServer((host, port), DemoHTTPHandler)
    print(f"🍊 Errand Agent side-by-side UI running at http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Run the Errand Agent side-by-side Web UI.")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    run_server(args.host, args.port)
