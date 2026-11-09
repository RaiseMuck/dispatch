import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dispatch import db  # noqa: E402
from dispatch.clock import ET, FakeClock  # noqa: E402
from dispatch.service import TaskService  # noqa: E402


# Fixed weight table for tests (the original 2026-09-27 weights). Ranking tests and seeded chat
# scenarios assert which item ranks first, so they must not move when Max retunes the live
# weights in dispatch/areas.py (first retune: 2026-10-22, Nationwide 5 -> 20).
TEST_AREA_WEIGHTS = {
    "Coreflow/CLARA": 22, "School": 18, "Side-venture ideas": 14, "Portfolio": 12,
    "System improvement": 10, "Personal admin": 8, "Fantasy": 6, "Nationwide": 5, "Home & life": 5,
}


@pytest.fixture(autouse=True)
def _pinned_area_weights(monkeypatch):
    from dispatch import areas
    assert set(areas.AREAS) == set(TEST_AREA_WEIGHTS), "area names changed - update TEST_AREA_WEIGHTS"
    for name, w in TEST_AREA_WEIGHTS.items():
        monkeypatch.setitem(areas.AREAS, name, w)


@pytest.fixture
def clock():
    return FakeClock(datetime(2026, 9, 28, 9, 0, tzinfo=ET))


@pytest.fixture
def service(tmp_path, clock):
    conn = db.connect(tmp_path / "test.db")
    db.migrate(conn, clock.now())
    return TaskService(conn, clock)


@pytest.fixture
def fake_ollama():
    """Start a local fake Ollama. respond(model, request_json) -> response text, or raise/sleep.
    Returns (url, seen_requests). Threaded, so a slow model does not block the next one."""
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    servers = []

    def start(respond, models=("qwen3.5:27b", "qwen3.5:4b")):
        seen = []

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, obj):
                body = json.dumps(obj).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                self._send({"models": [{"name": m, "size": i} for i, m in enumerate(models)]})

            def do_POST(self):
                req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                seen.append(req)
                try:
                    text = respond(req["model"], req)
                except Exception:  # noqa: BLE001
                    self.send_response(500)
                    self.end_headers()
                    return
                self._send({"response": text})

        srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
        return f"http://127.0.0.1:{srv.server_port}", seen

    yield start
    for s in servers:
        s.shutdown()
        s.server_close()


@pytest.fixture(autouse=True)
def _no_real_qwen_model_override(monkeypatch):
    """Tests must not depend on a QWEN_MODEL set on the machine (or in a real .env)."""
    monkeypatch.delenv("QWEN_MODEL", raising=False)
