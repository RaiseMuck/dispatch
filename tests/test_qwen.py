"""Qwen area picking: down -> 'unsorted' with no exception; up -> the area it names."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

from dispatch import qwen
from dispatch.areas import UNSORTED

DEAD_URL = "http://127.0.0.1:9"      # nothing listens on port 9


def test_unreachable_returns_unsorted_quickly():
    start = time.monotonic()
    assert qwen.pick_area("call Sam about Acme Plumbing", base_url=DEAD_URL, timeout=1.0) == UNSORTED
    assert time.monotonic() - start < 5


def test_capture_succeeds_when_qwen_is_down(service):
    t = service.capture("add: call Sam about Acme Plumbing by Friday",
                        area_picker=lambda title: qwen.pick_area(title, base_url=DEAD_URL, timeout=1.0))
    assert t.id and t.title == "call Sam about Acme Plumbing"
    assert t.area == UNSORTED and t.status == "active" and t.due_at is not None


def test_capture_survives_a_picker_that_raises(service):
    def boom(_):
        raise RuntimeError("model crashed")
    t = service.capture("add: something", area_picker=boom)
    assert t.area == UNSORTED


def test_match_area():
    assert qwen.match_area("Portfolio") == "Portfolio"
    assert qwen.match_area("<think>hmm</think>\nCoreflow/CLARA") == "Coreflow/CLARA"
    assert qwen.match_area("I think it's School.") == "School"
    assert qwen.match_area("banana") == UNSORTED


def test_happy_path_against_a_fake_ollama():
    class Handler(BaseHTTPRequestHandler):
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
            self._send({"models": [{"name": "llama3:8b", "size": 1},
                                   {"name": "qwen3.5:4b", "size": 3, "capabilities": ["thinking"]},
                                   {"name": "qwen3.5:27b", "size": 17}]})

        def do_POST(self):
            req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            Handler.seen = req
            # Phase 2 (plan 4.4): the model is asked for JSON, not a bare area name.
            self._send({"response": '{"area": "Fantasy", "payoff": 2, "likelihood": 0.9, '
                                    '"estimate_minutes": 10}'})

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{srv.server_port}"
        assert qwen.pick_area("set waiver claims", base_url=url) == "Fantasy"
        # Phase 2: preference order (27b first), never the llama; JSON mode, no thinking.
        assert Handler.seen["model"] == qwen.QWEN_PREFERENCE[0]
        assert Handler.seen["format"] == "json"
        assert Handler.seen["think"] is False
    finally:
        srv.shutdown()
