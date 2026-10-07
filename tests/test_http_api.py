"""End-to-end tests against the running HTTP server (no network sockets:
the handler is driven directly over BytesIO).
"""

from __future__ import annotations

import io
import json
import unittest
from http.server import ThreadingHTTPServer

from app.server import TimelineHandler


def request(method: str, path: str, body: bytes | None = None, headers=None):
    raw = []
    raw.append(f"{method} {path} HTTP/1.1\r\n".encode())
    raw.append(b"Host: localhost\r\n")
    if body is not None:
        raw.append(f"Content-Length: {len(body)}\r\n".encode())
        raw.append(b"Content-Type: application/json\r\n")
    for key, value in (headers or {}).items():
        raw.append(f"{key}: {value}\r\n".encode())
    raw.append(b"Connection: close\r\n\r\n")
    if body is not None:
        raw.append(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), TimelineHandler)
    input_stream = io.BytesIO(b"".join(raw))
    output_stream = io.BytesIO()

    class FakeConnection:
        # BaseHTTPRequestHandler reads via makefile but (wbufsize=0) writes
        # straight through a _SocketWriter backed by sendall.
        def makefile(self, *_args, **_kwargs):
            return input_stream

        def sendall(self, data):
            output_stream.write(data)

    TimelineHandler(FakeConnection(), ("127.0.0.1", 0), server)
    server.server_close()

    response = output_stream.getvalue()
    head, _, payload = response.partition(b"\r\n\r\n")
    status_line = head.split(b"\r\n", 1)[0].decode()
    status_code = int(status_line.split()[1])
    return status_code, json.loads(payload) if payload else None


GOOD_BODY = {
    "ticks_per_quarter": 480,
    "sample_rate": 48000,
    "tempo_points": [
        {"tick": 0, "microseconds_per_quarter": 500000, "mode": "constant"}
    ],
    "cues": [{"id": "a", "tick": 480}],
}


class HttpApiTests(unittest.TestCase):
    def test_health(self):
        status, body = request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"status": "ok"})

    def test_projection_success(self):
        status, body = request(
            "POST", "/api/timelines/project", json.dumps(GOOD_BODY).encode()
        )
        self.assertEqual(status, 200)
        cue = body["cues"][0]
        self.assertEqual(cue["id"], "a")
        self.assertEqual(cue["time_nanoseconds"], 500_000_000)
        self.assertEqual(cue["sample_frame"], 24_000)

    def test_validation_error_is_400_without_projections(self):
        bad = dict(GOOD_BODY)
        bad["tempo_points"] = [
            {"tick": 3, "microseconds_per_quarter": 0, "mode": "weird"}
        ]
        status, body = request(
            "POST", "/api/timelines/project", json.dumps(bad).encode()
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "VALIDATION_FAILED")
        self.assertNotIn("cues", body)
        codes = {e["code"] for e in body["error"]["errors"]}
        self.assertIn("TEMPO_GAP", codes)
        self.assertIn("NON_POSITIVE_TEMPO", codes)
        self.assertIn("INVALID_MODE", codes)

    def test_playback_plan_repeats_on_continuous_clock(self):
        body = dict(GOOD_BODY)
        body["cues"] = [
            {"id": "a", "tick": 0},
            {"id": "b", "tick": 480},
            {"id": "c", "tick": 960},
        ]
        # Half-open [0, 960) repeated twice: cue c is excluded from both.
        body["playback_plan"] = [
            {"start_tick": 0, "end_tick": 960, "repeats": 2},
            {"start_tick": 960, "end_tick": 1440, "repeats": 1},
        ]
        status, resp = request(
            "POST", "/api/timelines/project", json.dumps(body).encode()
        )
        self.assertEqual(status, 200)
        self.assertNotIn("cues", resp)
        occ = resp["occurrences"]
        self.assertEqual(
            [
                (o["id"], o["tick"], o["entry_index"], o["repeat_index"],
                 o["time_nanoseconds"], o["sample_frame"])
                for o in occ
            ],
            [
                ("a", 0, 0, 0, 0, 0),
                ("b", 480, 0, 0, 500_000_000, 24_000),
                ("a", 0, 0, 1, 1_000_000_000, 48_000),
                ("b", 480, 0, 1, 1_500_000_000, 72_000),
                ("c", 960, 1, 0, 2_000_000_000, 96_000),
            ],
        )

    def test_playback_plan_validation_error_is_400(self):
        body = dict(GOOD_BODY)
        body["playback_plan"] = [{"start_tick": 10, "end_tick": 10, "repeats": 3}]
        status, resp = request(
            "POST", "/api/timelines/project", json.dumps(body).encode()
        )
        self.assertEqual(status, 400)
        self.assertEqual(resp["error"]["code"], "VALIDATION_FAILED")
        self.assertNotIn("occurrences", resp)
        self.assertNotIn("cues", resp)
        errors = {(e["code"], e["path"]) for e in resp["error"]["errors"]}
        self.assertIn(
            ("INVALID_INTERVAL", "/playback_plan/0/end_tick"), errors
        )

    def test_malformed_json(self):
        status, body = request(
            "POST", "/api/timelines/project", b"{not json"
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "MALFORMED_JSON")

    def test_unknown_route(self):
        status, _ = request("POST", "/nope", b"{}")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
