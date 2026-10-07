"""Unit tests for exact timeline projection and whole-request validation."""

from __future__ import annotations

import unittest
from fractions import Fraction

from app.timeline import validate_and_project


def project(payload):
    status, body = validate_and_project(payload)
    if status != 200:
        raise AssertionError(f"projection failed: {body}")
    return body["cues"]


def errors_for(payload):
    status, body = validate_and_project(payload)
    assert status == 400, body
    assert body["error"]["code"] == "VALIDATION_FAILED"
    return body["error"]["errors"]


BASE = {
    "ticks_per_quarter": 480,
    "sample_rate": 48000,
    "tempo_points": [
        {"tick": 0, "microseconds_per_quarter": 500000, "mode": "constant"}
    ],
}


def cue(cid, tick):
    return {"id": cid, "tick": tick}


class ConstantTempoTests(unittest.TestCase):
    def test_basic_constant_grid(self):
        payload = {
            **BASE,
            "cues": [cue("a", 0), cue("b", 480), cue("c", 240), cue("d", 960)],
        }
        cues = project(payload)
        self.assertEqual(
            [(c["id"], c["time_nanoseconds"], c["sample_frame"]) for c in cues],
            [
                ("a", 0, 0),
                ("b", 500_000_000, 24_000),
                ("c", 250_000_000, 12_000),
                ("d", 1_000_000_000, 48_000),
            ],
        )

    def test_order_is_preserved(self):
        ticks = [480, 0, 960, 240, 480]
        payload = {**BASE, "cues": [cue(f"c{i}", tick) for i, tick in enumerate(ticks)]}
        cues = project(payload)
        self.assertEqual([c["tick"] for c in cues], ticks)
        self.assertEqual([c["id"] for c in cues], [f"c{i}" for i in range(5)])

    def test_last_point_holds_forever(self):
        # Tempo changes at 480 to 250000us and then stays there forever.
        payload = {
            "ticks_per_quarter": 480,
            "sample_rate": 48000,
            "tempo_points": [
                {"tick": 0, "microseconds_per_quarter": 500000, "mode": "constant"},
                {"tick": 480, "microseconds_per_quarter": 250000, "mode": "linear"},
            ],
            "cues": [cue("far", 480 + 480 * 10)],
        }
        result = project(payload)[0]
        # 0.5s for the first quarter + 10 quarters at 250000us = 3.0s
        self.assertEqual(result["time_nanoseconds"], 3_000_000_000)
        self.assertEqual(result["sample_frame"], 144_000)

    def test_no_accumulated_error_across_many_segments(self):
        # 300 constant one-tick segments at 1us/quarter, tpq=3 => 100us total.
        points = [
            {"tick": i, "microseconds_per_quarter": 1, "mode": "constant"}
            for i in range(300)
        ]
        payload = {
            "ticks_per_quarter": 3,
            "sample_rate": 3_000_000,
            "tempo_points": points,
            "cues": [cue("end", 300)],
        }
        result = project(payload)[0]
        self.assertEqual(result["time_nanoseconds"], 100_000)
        self.assertEqual(result["sample_frame"], 300)


class LinearRampTests(unittest.TestCase):
    def test_ramp_midpoint_and_endpoint(self):
        # Tempo ramps linearly from 600000 to 200000 us over one quarter.
        payload = {
            "ticks_per_quarter": 480,
            "sample_rate": 48000,
            "tempo_points": [
                {"tick": 0, "microseconds_per_quarter": 600000, "mode": "linear"},
                {"tick": 480, "microseconds_per_quarter": 200000, "mode": "constant"},
            ],
            "cues": [cue("mid", 240), cue("end", 480), cue("start", 0)],
        }
        cues = {c["id"]: c for c in project(payload)}
        # Full segment: trapezoid (600000+200000)/2 = 400000us = 0.4s
        self.assertEqual(cues["end"]["time_nanoseconds"], 400_000_000)
        self.assertEqual(cues["end"]["sample_frame"], 19_200)
        # Half segment: average tempo at midpoint (600000+400000)/2=500000us,
        # over half a quarter => 250000us = 0.25s
        self.assertEqual(cues["mid"]["time_nanoseconds"], 250_000_000)
        self.assertEqual(cues["mid"]["sample_frame"], 12_000)
        self.assertEqual(cues["start"]["time_nanoseconds"], 0)

    def test_ramp_matches_independent_fraction_calculation(self):
        tpq = 480
        a_tempo, b_tempo = 499_999, 500_007
        a_tick, b_tick = 120, 840
        points = [
            {"tick": 0, "microseconds_per_quarter": 333_333, "mode": "constant"},
            {"tick": a_tick, "microseconds_per_quarter": a_tempo, "mode": "linear"},
            {"tick": b_tick, "microseconds_per_quarter": b_tempo, "mode": "constant"},
        ]
        ticks = [0, 60, 120, 121, 480, 839, 840, 1000]
        payload = {
            "ticks_per_quarter": tpq,
            "sample_rate": 96_000,
            "tempo_points": points,
            "cues": [cue(str(t), t) for t in ticks],
        }
        cues = {c["id"]: c for c in project(payload)}

        def half_even(frac: Fraction) -> int:
            q, r = divmod(frac.numerator, frac.denominator)
            if 2 * r > frac.denominator or (2 * r == frac.denominator and q % 2):
                return q + 1
            return q

        for tick in ticks:
            if tick <= a_tick:
                elapsed = Fraction(333_333 * tick, tpq)
            elif tick < b_tick:
                elapsed = Fraction(333_333 * a_tick, tpq)
                d = tick - a_tick
                avg = a_tempo + Fraction((b_tempo - a_tempo) * d, 2 * (b_tick - a_tick))
                elapsed += avg * Fraction(d, tpq)
            else:
                elapsed = Fraction(333_333 * a_tick, tpq)
                elapsed += Fraction((a_tempo + b_tempo) * (b_tick - a_tick), 2 * tpq)
                elapsed += Fraction(b_tempo * (tick - b_tick), tpq)
            expected_ns = half_even(elapsed * 1000)
            expected_frame = half_even(elapsed * 96_000 / Fraction(1_000_000))
            self.assertEqual(cues[str(tick)]["time_nanoseconds"], expected_ns, tick)
            self.assertEqual(cues[str(tick)]["sample_frame"], expected_frame, tick)

    def test_ramp_with_no_cues_inside_is_exact(self):
        payload = {
            "ticks_per_quarter": 480,
            "sample_rate": 48000,
            "tempo_points": [
                {"tick": 0, "microseconds_per_quarter": 1, "mode": "linear"},
                {"tick": 1, "microseconds_per_quarter": 2, "mode": "constant"},
            ],
            "cues": [cue("a", 0), cue("b", 1)],
        }
        cues = {c["id"]: c for c in project(payload)}
        # (1+2)/2 * (1/480) us = 1.5/480 us = 3.125 ns -> half-even 3 ns
        self.assertEqual(cues["b"]["time_nanoseconds"], 3)


class HalfEvenRoundingTests(unittest.TestCase):
    def test_nanosecond_halves_round_to_even(self):
        # tpq=16, tempo 1us: tick 1 => 62.5ns -> 62; tick 3 => 187.5ns -> 188
        payload = {
            "ticks_per_quarter": 16,
            "sample_rate": 1,
            "tempo_points": [
                {"tick": 0, "microseconds_per_quarter": 1, "mode": "constant"}
            ],
            "cues": [cue("one", 1), cue("three", 3), cue("two", 2)],
        }
        cues = {c["id"]: c for c in project(payload)}
        self.assertEqual(cues["one"]["time_nanoseconds"], 62)    # 62.5 -> 62
        self.assertEqual(cues["three"]["time_nanoseconds"], 188)  # 187.5 -> 188
        self.assertEqual(cues["two"]["time_nanoseconds"], 125)

    def test_sample_frame_halves_round_to_even(self):
        # tpq=1, tempo 1us, tick 1 => elapsed 1us.
        # sr 500000 -> 0.5 frames -> 0; sr 1500000 -> 1.5 frames -> 2.
        for sr, expected in ((500_000, 0), (1_500_000, 2), (2_500_000, 2)):
            payload = {
                "ticks_per_quarter": 1,
                "sample_rate": sr,
                "tempo_points": [
                    {"tick": 0, "microseconds_per_quarter": 1, "mode": "constant"}
                ],
                "cues": [cue("x", 1)],
            }
            self.assertEqual(project(payload)[0]["sample_frame"], expected, sr)


class SameTickTests(unittest.TestCase):
    def test_cues_at_same_tick_are_identical(self):
        payload = {
            **BASE,
            "tempo_points": [
                {"tick": 0, "microseconds_per_quarter": 600000, "mode": "linear"},
                {"tick": 480, "microseconds_per_quarter": 240000, "mode": "constant"},
            ],
            "cues": [
                cue("light", 240),
                cue("subtitle", 240),
                cue("record", 240),
            ],
        }
        cues = project(payload)
        ns = {c["time_nanoseconds"] for c in cues}
        frames = {c["sample_frame"] for c in cues}
        self.assertEqual(len(ns), 1)
        self.assertEqual(len(frames), 1)

    def test_cue_exactly_on_tempo_tick_matches_cumulative(self):
        payload = {
            "ticks_per_quarter": 480,
            "sample_rate": 48000,
            "tempo_points": [
                {"tick": 0, "microseconds_per_quarter": 500000, "mode": "linear"},
                {"tick": 480, "microseconds_per_quarter": 200000, "mode": "constant"},
                {"tick": 960, "microseconds_per_quarter": 100000, "mode": "constant"},
            ],
            "cues": [cue("at480", 480), cue("at960", 960)],
        }
        cues = {c["id"]: c for c in project(payload)}
        # Ramp 500000->200000 across one quarter = 350000us.
        self.assertEqual(cues["at480"]["time_nanoseconds"], 350_000_000)
        # + one quarter at 200000us = 550000us.
        self.assertEqual(cues["at960"]["time_nanoseconds"], 550_000_000)


class ValidationTests(unittest.TestCase):
    def assert_code_at(self, payload, code, path):
        errors = errors_for(payload)
        matches = [e for e in errors if e["code"] == code and e["path"] == path]
        self.assertTrue(matches, f"expected {code} at {path}, got {errors}")

    def test_first_point_must_be_at_zero(self):
        payload = {
            **BASE,
            "tempo_points": [
                {"tick": 12, "microseconds_per_quarter": 500000, "mode": "constant"}
            ],
            "cues": [cue("a", 0)],
        }
        self.assert_code_at(payload, "TEMPO_GAP", "/tempo_points/0/tick")

    def test_duplicate_and_unordered_tempo_ticks(self):
        points = [
            {"tick": 0, "microseconds_per_quarter": 500000, "mode": "constant"},
            {"tick": 480, "microseconds_per_quarter": 400000, "mode": "constant"},
            {"tick": 480, "microseconds_per_quarter": 300000, "mode": "linear"},
            {"tick": 200, "microseconds_per_quarter": 200000, "mode": "constant"},
        ]
        payload = {**BASE, "tempo_points": points, "cues": [cue("a", 0)]}
        errors = errors_for(payload)
        codes_paths = {(e["code"], e["path"]) for e in errors}
        self.assertIn(
            ("DUPLICATE_TEMPO_TICK", "/tempo_points/2/tick"), codes_paths
        )
        self.assertIn(
            ("TEMPO_NOT_ORDERED", "/tempo_points/3/tick"), codes_paths
        )

    def test_invalid_mode(self):
        points = [
            {"tick": 0, "microseconds_per_quarter": 500000, "mode": "rubato"}
        ]
        payload = {**BASE, "tempo_points": points, "cues": [cue("a", 0)]}
        self.assert_code_at(payload, "INVALID_MODE", "/tempo_points/0/mode")

    def test_non_positive_tempo_with_position(self):
        points = [
            {"tick": 0, "microseconds_per_quarter": 500000, "mode": "constant"},
            {"tick": 480, "microseconds_per_quarter": 0, "mode": "constant"},
            {"tick": 960, "microseconds_per_quarter": -7, "mode": "linear"},
        ]
        payload = {**BASE, "tempo_points": points, "cues": [cue("a", 0)]}
        errors = errors_for(payload)
        paths = {e["path"] for e in errors if e["code"] == "NON_POSITIVE_TEMPO"}
        self.assertEqual(
            paths,
            {
                "/tempo_points/1/microseconds_per_quarter",
                "/tempo_points/2/microseconds_per_quarter",
            },
        )

    def test_negative_cue_tick(self):
        payload = {**BASE, "cues": [cue("ok", 0), cue("bad", -1)]}
        self.assert_code_at(payload, "CUE_OUT_OF_RANGE", "/cues/1/tick")

    def test_duplicate_cue_ids_report_both_positions(self):
        payload = {**BASE, "cues": [cue(7, 0), cue(7, 480), cue("x", 12)]}
        errors = errors_for(payload)
        dup = [e for e in errors if e["code"] == "DUPLICATE_CUE_ID"]
        self.assertEqual(len(dup), 1)
        self.assertEqual(dup[0]["path"], "/cues/1/id")
        self.assertIn("/cues/0", dup[0]["message"])

    def test_count_limits(self):
        too_many_points = [
            {"tick": 0, "microseconds_per_quarter": 1, "mode": "constant"}
        ] + [
            {"tick": i, "microseconds_per_quarter": 1, "mode": "constant"}
            for i in range(1, 501)
        ]
        self.assertEqual(len(too_many_points), 501)
        payload = {**BASE, "tempo_points": too_many_points, "cues": [cue("a", 0)]}
        self.assert_code_at(payload, "INVALID_COUNT", "/tempo_points")

        payload = {**BASE, "cues": []}
        self.assert_code_at(payload, "INVALID_COUNT", "/cues")

        too_many_cues = [cue(i, i) for i in range(2001)]
        payload = {**BASE, "cues": too_many_cues}
        self.assert_code_at(payload, "INVALID_COUNT", "/cues")

    def test_type_errors_including_bool_rejection(self):
        payload = {
            "ticks_per_quarter": True,
            "sample_rate": "48000",
            "tempo_points": [
                {"tick": 0, "microseconds_per_quarter": 1.5, "mode": "constant"}
            ],
            "cues": [{"id": None, "tick": 0}],
        }
        errors = errors_for(payload)
        paths = {e["path"] for e in errors if e["code"] == "INVALID_TYPE"}
        self.assertIn("/ticks_per_quarter", paths)
        self.assertIn("/sample_rate", paths)
        self.assertIn("/tempo_points/0/microseconds_per_quarter", paths)
        self.assertIn("/cues/0/id", paths)

    def test_missing_and_unknown_fields(self):
        payload = {"sample_rate": 48000}
        errors = errors_for(payload)
        missing = {e["path"] for e in errors if e["code"] == "MISSING_FIELD"}
        self.assertEqual(missing, {"/ticks_per_quarter", "/tempo_points", "/cues"})

        payload = {**BASE, "cues": [cue("a", 0)], "extra": 1}
        self.assert_code_at(payload, "UNKNOWN_FIELD", "/extra")

    def test_errors_never_carry_partial_projections(self):
        payload = {
            **BASE,
            "tempo_points": [
                {"tick": 0, "microseconds_per_quarter": 0, "mode": "constant"}
            ],
            "cues": [cue("a", 0), cue("b", 480)],
        }
        status, body = validate_and_project(payload)
        self.assertEqual(status, 400)
        self.assertNotIn("cues", body)
        self.assertIn("errors", body["error"])

    def test_non_object_body(self):
        for payload in ([], "x", 42, None):
            status, body = validate_and_project(payload)
            self.assertEqual(status, 400)
            self.assertEqual(body["error"]["code"], "INVALID_REQUEST")

    def test_error_codes_are_stable_strings(self):
        payload = {**BASE, "cues": [cue("a", -1)]}
        errors = errors_for(payload)
        self.assertTrue(all(isinstance(e["code"], str) for e in errors))
        # A second identical request yields byte-identical error content.
        _, body1 = validate_and_project(payload)
        _, body2 = validate_and_project(payload)
        self.assertEqual(body1, body2)


def plan_payload(entries, cues=None, **overrides):
    payload = {**BASE, **overrides}
    payload["cues"] = cues if cues is not None else [cue("a", 0), cue("b", 480)]
    payload["playback_plan"] = entries
    return payload


def entry(start, end, repeats):
    return {"start_tick": start, "end_tick": end, "repeats": repeats}


def occurrences(payload):
    status, body = validate_and_project(payload)
    if status != 200:
        raise AssertionError(f"plan projection failed: {body}")
    assert "cues" not in body
    return body["occurrences"]


class PlaybackPlanTests(unittest.TestCase):
    def test_basic_repeats_and_entries(self):
        payload = plan_payload([entry(0, 480, 2), entry(480, 960, 1)])
        result = occurrences(payload)
        self.assertEqual(
            [
                (o["id"], o["tick"], o["plan_entry"], o["repeat"])
                for o in result
            ],
            [("a", 0, 0, 0), ("a", 0, 0, 1), ("b", 480, 1, 0)],
        )
        # Constant 500000us/quarter: each [0,480) is 0.5s; [480,960) is 0.5s.
        self.assertEqual([o["time_nanoseconds"] for o in result],
                         [0, 500_000_000, 1_000_000_000])
        self.assertEqual([o["sample_frame"] for o in result],
                         [0, 24_000, 48_000])

    def test_response_shape_when_enabled(self):
        status, body = validate_and_project(
            plan_payload([entry(0, 480, 1)])
        )
        self.assertEqual(status, 200)
        self.assertIn("occurrences", body)
        self.assertNotIn("cues", body)
        self.assertEqual(body["ticks_per_quarter"], 480)

    def test_omitted_plan_is_unchanged(self):
        payload = {**BASE, "cues": [cue("a", 480)]}
        status, body = validate_and_project(payload)
        self.assertEqual(status, 200)
        self.assertIn("cues", body)
        self.assertNotIn("occurrences", body)
        self.assertEqual(
            body["cues"][0],
            {"id": "a", "tick": 480,
             "time_nanoseconds": 500_000_000, "sample_frame": 24_000},
        )

    def test_order_entry_repeat_original_cue_order(self):
        # Cues deliberately unsorted; original request order must be kept
        # inside each occurrence.
        cues = [cue("late", 480), cue("early", 0), cue("mid", 240)]
        payload = plan_payload([entry(0, 960, 2)], cues=cues)
        result = occurrences(payload)
        self.assertEqual(
            [(o["id"], o["plan_entry"], o["repeat"]) for o in result],
            [
                ("late", 0, 0), ("early", 0, 0), ("mid", 0, 0),
                ("late", 0, 1), ("early", 0, 1), ("mid", 0, 1),
            ],
        )

    def test_half_open_interval_boundary(self):
        cues = [cue("at-start", 0), cue("inside", 240), cue("at-end", 480)]
        payload = plan_payload(
            [entry(0, 480, 1), entry(480, 960, 1)], cues=cues
        )
        result = occurrences(payload)
        self.assertEqual(
            [(o["id"], o["plan_entry"]) for o in result],
            [("at-start", 0), ("inside", 0), ("at-end", 1)],
        )

    def test_clock_carries_across_cue_free_segments(self):
        # tpq=3, constant 1us/quarter => each tick is 1/3 us.
        points = [
            {"tick": i, "microseconds_per_quarter": 1, "mode": "constant"}
            for i in range(300)
        ]
        payload = plan_payload(
            [entry(0, 150, 2), entry(150, 300, 1)],
            cues=[cue("x", 150)],
            ticks_per_quarter=3,
            sample_rate=3_000_000,
            tempo_points=points,
        )
        result = occurrences(payload)
        # Two silent 50us entries, then the cue: 100us exact.
        self.assertEqual(result[0]["time_nanoseconds"], 100_000)
        self.assertEqual(result[0]["sample_frame"], 300)

    def test_backward_jump_keeps_clock_continuous(self):
        points = [
            {"tick": i, "microseconds_per_quarter": 1, "mode": "constant"}
            for i in range(300)
        ]
        payload = plan_payload(
            [entry(200, 300, 1), entry(0, 100, 2), entry(0, 2, 1)],
            cues=[cue("a", 0), cue("b", 1), cue("c", 100)],
            ticks_per_quarter=3,
            sample_rate=3_000_000,
            tempo_points=points,
        )
        result = occurrences(payload)
        ns = [o["time_nanoseconds"] for o in result]
        frames = [o["sample_frame"] for o in result]
        self.assertEqual(ns, sorted(ns))
        self.assertEqual(frames, sorted(frames))
        # First entry [200,300) has no cues but still consumes 100/3 us.
        # Entry 1 repeat 0 starts at 100/3 us: cue a there, cue b +1/3 us.
        self.assertEqual(result[0]["id"], "a")
        self.assertEqual(result[0]["plan_entry"], 1)
        self.assertAlmostEqual(result[0]["time_nanoseconds"], 33_333, delta=1)

    def test_overlapping_and_duplicate_entries_are_allowed(self):
        payload = plan_payload(
            [entry(0, 480, 1), entry(0, 480, 1), entry(240, 960, 1)],
            cues=[cue("a", 0), cue("b", 480)],
        )
        result = occurrences(payload)
        self.assertEqual(
            [(o["id"], o["plan_entry"], o["repeat"]) for o in result],
            [("a", 0, 0), ("a", 1, 0), ("b", 2, 0)],
        )
        # 0.5s + 0.5s to reach the last entry; cue b is 0.25s into
        # [240,960) (tick 480 - tick 240 at 500000us/quarter) => 1.25s.
        self.assertEqual(result[-1]["time_nanoseconds"], 1_250_000_000)

    def test_linear_ramp_segment_durations(self):
        points = [
            {"tick": 0, "microseconds_per_quarter": 600000, "mode": "linear"},
            {"tick": 480, "microseconds_per_quarter": 200000, "mode": "constant"},
        ]
        payload = plan_payload(
            [entry(0, 480, 1), entry(480, 960, 1)],
            cues=[cue("mid", 240), cue("end", 480)],
            tempo_points=points,
        )
        result = {o["id"]: o for o in occurrences(payload)}
        # Ramp [0,480) trapezoid = 400000us; midpoint = 250000us.
        self.assertEqual(result["mid"]["time_nanoseconds"], 250_000_000)
        # [480,960) at constant 200000us = 200000us after the 400000us ramp.
        self.assertEqual(result["end"]["time_nanoseconds"], 400_000_000)

    def test_cues_outside_every_interval_never_occur(self):
        payload = plan_payload([entry(0, 10, 1)], cues=[cue("far", 9999)])
        self.assertEqual(occurrences(payload), [])

    def test_expansion_cap_boundary(self):
        # 2000 cues repeated twice = 4000 (allowed); three times = 6000 (denied).
        cues = [cue(i, 0) for i in range(2000)]
        self.assertEqual(len(occurrences(plan_payload([entry(0, 10, 2)], cues=cues))), 4000)
        errors = errors_for(plan_payload([entry(0, 10, 3)], cues=cues))
        self.assertIn(
            ("PLAN_TOO_LARGE", "/playback_plan"),
            {(e["code"], e["path"]) for e in errors},
        )

    def test_errors_never_carry_occurrences(self):
        status, body = validate_and_project(
            plan_payload([entry(10, 5, 1)])  # end <= start
        )
        self.assertEqual(status, 400)
        self.assertNotIn("occurrences", body)
        self.assertNotIn("cues", body)


class PlaybackPlanValidationTests(unittest.TestCase):
    def assert_code_at(self, entries, code, path, cues=None):
        errors = errors_for(plan_payload(entries, cues=cues))
        matches = [e for e in errors if e["code"] == code and e["path"] == path]
        self.assertTrue(matches, f"expected {code} at {path}, got {errors}")

    def test_plan_type_and_count(self):
        self.assert_code_at("nope", "INVALID_TYPE", "/playback_plan")  # type: ignore[arg-type]
        self.assert_code_at([], "INVALID_COUNT", "/playback_plan")
        too_many = [entry(0, 1, 1) for _ in range(65)]
        self.assert_code_at(too_many, "INVALID_COUNT", "/playback_plan")

    def test_entry_must_be_object(self):
        self.assert_code_at([42], "INVALID_TYPE", "/playback_plan/0")

    def test_missing_fields(self):
        self.assert_code_at(
            [{"start_tick": 0, "end_tick": 480}],
            "MISSING_FIELD", "/playback_plan/0/repeats",
        )
        self.assert_code_at(
            [{"end_tick": 480, "repeats": 1}],
            "MISSING_FIELD", "/playback_plan/0/start_tick",
        )

    def test_type_and_bool_rejection(self):
        self.assert_code_at(
            [{"start_tick": True, "end_tick": "1", "repeats": 1.0}],
            "INVALID_TYPE", "/playback_plan/0/start_tick",
        )
        self.assert_code_at(
            [{"start_tick": 0, "end_tick": "1", "repeats": 1.0}],
            "INVALID_TYPE", "/playback_plan/0/end_tick",
        )
        self.assert_code_at(
            [{"start_tick": 0, "end_tick": 1, "repeats": True}],
            "INVALID_TYPE", "/playback_plan/0/repeats",
        )

    def test_negative_ticks(self):
        self.assert_code_at(
            [entry(-1, 480, 1)],
            "PLAN_TICK_OUT_OF_RANGE", "/playback_plan/0/start_tick",
        )
        self.assert_code_at(
            [entry(0, -2, 1)],
            "PLAN_TICK_OUT_OF_RANGE", "/playback_plan/0/end_tick",
        )

    def test_invalid_intervals(self):
        for bad_end in (100, 0):
            self.assert_code_at(
                [entry(100, bad_end, 1)],
                "INVALID_INTERVAL", "/playback_plan/0/end_tick",
            )

    def test_repeats_range(self):
        self.assert_code_at(
            [entry(0, 1, 0)], "REPEATS_OUT_OF_RANGE", "/playback_plan/0/repeats"
        )
        self.assert_code_at(
            [entry(0, 1, 17)], "REPEATS_OUT_OF_RANGE", "/playback_plan/0/repeats"
        )

    def test_unknown_field(self):
        self.assert_code_at(
            [{"start_tick": 0, "end_tick": 1, "repeats": 1, "extra": 9}],
            "UNKNOWN_FIELD", "/playback_plan/0/extra",
        )

    def test_plan_errors_are_deterministic(self):
        payload = plan_payload([entry(5, 5, 17)])
        _, body1 = validate_and_project(payload)
        _, body2 = validate_and_project(payload)
        self.assertEqual(body1, body2)


if __name__ == "__main__":
    unittest.main()
