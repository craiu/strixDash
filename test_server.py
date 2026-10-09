"""Focused tests for the strixDash collector and bounded history store.

The tests deliberately use only the public helpers and Store methods.  They can
be run from the repository root with ``python -m unittest strixDash.test_server``
or from this directory with ``python -m unittest test_server``.
"""

from __future__ import annotations

import math
import tempfile
import time
import unittest
from pathlib import Path

try:  # Running from the strixDash directory.
    from server import Store, counter_rate, parse_prometheus, read_number, safe_ratio
except ModuleNotFoundError:  # Running as ``python -m unittest strixDash.test_server``.
    from strixDash.server import (  # type: ignore[no-redef]
        Store,
        counter_rate,
        parse_prometheus,
        read_number,
        safe_ratio,
    )


def _snapshot(timestamp: float, output_tps: float = 0.0) -> dict:
    """Return a complete, representative normalized snapshot."""

    return {
        "timestamp": timestamp,
        "host": {
            "cpu_percent": 17.5,
            "gpu": {"busy_percent": 42.0, "temp_c": 51.25},
            "memory": {"used": 12.5},
        },
        "model": {
            "output_tps": output_tps,
            "kv_ratio": 0.375,
            "active": 1,
        },
    }


class _CaptureStore:
    """Small Store double for exercising Collector.sample without SQLite."""

    def __init__(self) -> None:
        self.samples: list[dict] = []
        self.events_seen: list[tuple[str, str]] = []

    def add_sample(self, snapshot: dict) -> None:
        self.samples.append(snapshot)

    def add_event(self, level: str, message: str) -> None:
        self.events_seen.append((level, message))


def _hardware() -> dict:
    return {
        "cpu_percent": 12.5,
        "gpu": {"busy_percent": 44.0, "temp_c": 52.0},
        "memory": {"used": 8 * 1024**3},
    }


def _metrics() -> str:
    return """llamacpp:tokens_predicted_total 1234
llamacpp:predicted_tokens_seconds 21.5
llamacpp:prompt_tokens_seconds 35.0
llamacpp:requests_processing 2
llamacpp:requests_deferred 1
llamacpp:kv_cache_tokens 4096
llamacpp:kv_cache_usage_ratio 0.25
llamacpp:prompt_tokens_total 500
halogen:requests_total 8
"""


def _cache() -> dict:
    return {"hits": 8, "misses": 2, "bytes": 4096}


class CollectorSampleTests(unittest.TestCase):
    def _collector(self):
        # Importing here keeps the helper tests usable if this file is run
        # while only the public server helpers are being developed.
        from_server = __import__(Store.__module__, fromlist=["Collector"])
        capture = _CaptureStore()
        collector = from_server.Collector(capture, "http://model.invalid")
        collector.hardware = lambda elapsed: _hardware()
        collector.metadata = lambda now: None
        collector.viewers.touch("test-viewer")
        # The helper's viewer represents an already-connected browser.  Tests
        # that specifically exercise the pause/resume transition release it
        # and re-touch it around the sample under test.
        collector.model_paused = False
        return collector, capture

    def test_sample_uses_cached_health_and_reports_valid_model(self) -> None:
        collector, capture = self._collector()
        now = time.time()
        collector.health = {
            "status": "ok",
            "engine": {"responds": True},
            "model": "Qwen/Qwen3.8-Next-Flash",
            "version": {"engine": "halogen-0.9.1"},
        }
        collector.health_last_ok = now
        collector.last_health_check = now
        calls: list[str] = []

        def fetch(path: str, as_json: bool = True):
            calls.append(path)
            if path == "/metrics":
                return _metrics()
            if path == "/cache":
                return _cache()
            if path == "/health":
                raise AssertionError("fresh health request was not expected")
            raise AssertionError(f"unexpected model request: {path}")

        collector.fetch = fetch
        collector.sample()

        snapshot = capture.samples[-1]
        model = snapshot["model"]
        self.assertNotIn("/health", calls)
        self.assertTrue(model["online"])
        self.assertEqual(model["id"], "Qwen/Qwen3.8-Next-Flash")
        self.assertEqual(model["active"], 2)
        self.assertEqual(model["output_tokens"], 1234.0)
        self.assertEqual(snapshot["sources"]["model"]["ok"], True)

    def test_busy_requests_keep_model_online_when_health_probe_is_not_ready(self) -> None:
        collector, capture = self._collector()
        collector.last_health_check = 0
        collector.health = {}
        calls: list[str] = []

        def fetch(path: str, as_json: bool = True):
            calls.append(path)
            if path == "/metrics":
                return _metrics()
            if path == "/cache":
                return _cache()
            if path == "/health":
                return {
                    "status": "ok",
                    "engine": {"responds": False},
                    "model": "Qwen/Qwen3.8-Next-Flash",
                }
            raise AssertionError(f"unexpected model request: {path}")

        collector.fetch = fetch
        collector.sample()

        model = capture.samples[-1]["model"]
        self.assertIn("/health", calls)
        self.assertEqual(model["active"], 2)
        self.assertTrue(model["online"], "active requests prove the endpoint is busy")
        self.assertTrue(capture.samples[-1]["sources"]["model"]["ok"])

    def test_metrics_failure_degrades_model_source_without_throwing(self) -> None:
        collector, capture = self._collector()

        def fetch(path: str, as_json: bool = True):
            raise OSError("metrics endpoint unavailable")

        collector.fetch = fetch
        collector.sample()

        snapshot = capture.samples[-1]
        self.assertFalse(snapshot["sources"]["model"]["ok"])
        self.assertIsNone(snapshot["model"].get("output_tps"))
        self.assertFalse(snapshot["model"]["online"])

    def test_no_viewers_pauses_all_sampling_and_keeps_state_bounded(self) -> None:
        collector, capture = self._collector()
        collector.viewers.release("test-viewer")
        hardware_calls: list[float] = []
        metadata_calls: list[float] = []
        collector.hardware = lambda elapsed: (hardware_calls.append(elapsed), _hardware())[1]
        collector.metadata = lambda now: metadata_calls.append(now)

        def fetch(path: str, as_json: bool = True):
            raise AssertionError(f"model polling must be paused, got {path}")

        collector.fetch = fetch
        initial_counts = (
            collector.model_requests_total,
            collector.hardware_samples_total,
            collector.history_samples_total,
            len(capture.samples),
        )
        for _ in range(3):
            collector.sample()

        snapshot = collector.snapshot
        model = snapshot["model"]
        self.assertEqual(hardware_calls, [])
        self.assertEqual(metadata_calls, [])
        self.assertTrue(model["sampling_paused"])
        for field in ("output_tps", "decode_tps", "prefill_tps", "kv_ratio", "active"):
            self.assertIsNone(model.get(field), field)
        self.assertEqual(snapshot["host"], {})
        self.assertFalse(snapshot["monitoring"]["hardware_sampling"])
        self.assertFalse(snapshot["monitoring"]["model_sampling"])
        self.assertEqual(
            (
                collector.model_requests_total,
                collector.hardware_samples_total,
                collector.history_samples_total,
                len(capture.samples),
            ),
            initial_counts,
        )
        self.assertIsNone(collector.prev_cpu)
        self.assertIsNone(collector.prev_net)
        self.assertIsNone(collector.prev_time)
        self.assertIsNone(collector.prev_monotonic)

    def test_releasing_one_of_two_viewers_keeps_polling_until_final_release(self) -> None:
        collector, capture = self._collector()
        collector.viewers.touch("second-viewer")
        collector.health = {
            "status": "ok",
            "engine": {"responds": True},
            "model": "Qwen/Qwen3.8-Next-Flash",
        }
        collector.last_health_check = time.time()
        calls: list[str] = []

        def fetch(path: str, as_json: bool = True):
            calls.append(path)
            if path == "/metrics":
                return _metrics()
            if path == "/cache":
                return _cache()
            raise AssertionError(f"unexpected model request: {path}")

        collector.fetch = fetch
        collector.sample()
        self.assertIn("/metrics", calls)

        collector.viewers.release("test-viewer")
        calls.clear()
        collector.sample()
        self.assertIn("/metrics", calls, "the second viewer must keep polling active")

        collector.viewers.release("second-viewer")
        calls.clear()
        sample_count = len(capture.samples)
        collector.sample()
        self.assertEqual(calls, [])
        self.assertEqual(len(capture.samples), sample_count)
        self.assertTrue(collector.snapshot["model"]["sampling_paused"])
        self.assertFalse(collector.snapshot["monitoring"]["hardware_sampling"])

    def test_expired_viewer_is_pruned_using_supplied_time(self) -> None:
        from_server = __import__(Store.__module__, fromlist=["ViewerPresence"])
        viewers = from_server.ViewerPresence(ttl=20)

        self.assertTrue(viewers.touch("fixed-viewer", now=100.0))
        self.assertEqual(viewers.count(now=119.9), 1)
        self.assertEqual(viewers.count(now=120.0), 0)
        self.assertFalse(viewers.touch("bad viewer", now=121.0))
        self.assertFalse(viewers.touch("x" * 81, now=121.0))
        self.assertTrue(viewers.touch("valid_2", now=121.0))
        viewers.release("valid_2")
        self.assertEqual(viewers.count(now=121.0), 0)

    def test_resume_clears_rate_history_and_forces_health_refresh(self) -> None:
        collector, capture = self._collector()
        collector.health = {
            "status": "ok",
            "engine": {"responds": True},
            "model": "Qwen/Qwen3.8-Next-Flash",
        }
        collector.health_last_ok = time.time()
        collector.last_health_check = time.time()
        output_value = [1234.0]
        calls: list[str] = []

        def fetch(path: str, as_json: bool = True):
            calls.append(path)
            if path == "/metrics":
                return _metrics().replace("1234", str(output_value[0]))
            if path == "/cache":
                return _cache()
            if path == "/health":
                return {
                    "status": "ok",
                    "engine": {"responds": True},
                    "model": "Qwen/Qwen3.8-Next-Flash",
                }
            raise AssertionError(f"unexpected model request: {path}")

        collector.fetch = fetch
        collector.sample()
        collector.viewers.release("test-viewer")
        calls.clear()
        sample_count = len(capture.samples)
        collector.sample()
        self.assertEqual(len(capture.samples), sample_count)
        self.assertTrue(collector.snapshot["model"]["sampling_paused"])
        self.assertFalse(collector.snapshot["monitoring"]["hardware_sampling"])
        self.assertIsNone(collector.prev_cpu)
        self.assertIsNone(collector.prev_net)
        self.assertIsNone(collector.prev_time)
        self.assertIsNone(collector.prev_monotonic)

        collector.viewers.touch("test-viewer")
        output_value[0] = 1300.0
        calls.clear()
        collector.sample()

        model = capture.samples[-1]["model"]
        self.assertIn("/health", calls, "resuming must force a fresh health check")
        self.assertIsNone(model.get("output_tps"), "paused counters must not create a rate spike")


class CollectorHelperTests(unittest.TestCase):
    def test_parse_prometheus_ignores_comments_and_labels_and_accepts_float_forms(self) -> None:
        metrics = parse_prometheus(
            """# HELP strix_cpu CPU percentage
            # TYPE strix_cpu gauge
            strix_cpu 1.25e+02
            strix_gpu_busy_percent{card=\"0\",name=\"Strix Halo\"} 37.5
            strix_temp_c -2.5E-3
            strix_zero 0
            """
        )

        self.assertAlmostEqual(metrics["strix_cpu"], 125.0)
        self.assertAlmostEqual(metrics["strix_gpu_busy_percent"], 37.5)
        self.assertAlmostEqual(metrics["strix_temp_c"], -0.0025)
        self.assertEqual(metrics["strix_zero"], 0.0)
        self.assertNotIn("#", metrics)

    def test_counter_rate_handles_missing_reset_and_invalid_elapsed(self) -> None:
        self.assertAlmostEqual(counter_rate(150.0, 100.0, 5.0), 10.0)
        self.assertIsNone(counter_rate(None, 100.0, 5.0))
        self.assertIsNone(counter_rate(150.0, None, 5.0))
        self.assertIsNone(counter_rate(90.0, 100.0, 5.0), "counter reset must not become a negative rate")
        self.assertIsNone(counter_rate(150.0, 100.0, 0.0))
        self.assertIsNone(counter_rate(150.0, 100.0, -1.0))
        self.assertIsNone(counter_rate(150.0, 100.0, math.nan))

    def test_safe_ratio_handles_zero_and_missing_denominators(self) -> None:
        self.assertAlmostEqual(safe_ratio(3.0, 4.0), 0.75)
        self.assertEqual(safe_ratio(0.0, 4.0), 0.0)
        self.assertIsNone(safe_ratio(3.0, 0.0))
        self.assertIsNone(safe_ratio(3.0, None))
        self.assertIsNone(safe_ratio(None, 4.0))

    def test_read_number_returns_float_or_none_without_raising(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            valid = root / "valid"
            invalid = root / "invalid"
            valid.write_text("  -6.02e+23\n", encoding="ascii")
            invalid.write_text("not-a-number\n", encoding="ascii")

            self.assertAlmostEqual(read_number(valid), -6.02e23)
            self.assertIsNone(read_number(invalid))
            self.assertIsNone(read_number(root / "missing"))


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "history.sqlite3"
        self.store = Store(self.db_path)

    def tearDown(self) -> None:
        # Store implementations may expose a close method; use it when
        # available so Windows test runs can remove the temporary database.
        close = getattr(self.store, "close", None)
        if callable(close):
            close()
        self.temp_dir.cleanup()

    @staticmethod
    def _timestamps(rows: list[dict]) -> list[float]:
        return [float(row["timestamp"]) for row in rows]

    def test_history_discards_samples_older_than_seven_days(self) -> None:
        now = time.time()
        old_timestamp = now - (8 * 24 * 60 * 60)
        self.store.add_sample(_snapshot(old_timestamp, 1.0))
        self.store.add_sample(_snapshot(now, 2.0))

        rows = self.store.history(10 * 24 * 60 * 60)
        timestamps = self._timestamps(rows)

        self.assertTrue(timestamps)
        self.assertNotIn(old_timestamp, timestamps)
        self.assertGreaterEqual(min(timestamps), now - (7 * 24 * 60 * 60))

    def test_history_is_monotonic_and_bounded_to_six_hundred_points(self) -> None:
        # Use current-era timestamps so the range filter includes the samples.
        now = time.time()
        for index in range(650):
            self.store.add_sample(_snapshot(now - 649 + index, float(index)))

        rows = self.store.history(2_000)
        timestamps = self._timestamps(rows)

        self.assertGreater(len(rows), 0)
        self.assertLessEqual(len(rows), 600)
        self.assertEqual(timestamps, sorted(timestamps))
        self.assertAlmostEqual(timestamps[-1], now)

    def test_events_keep_only_the_last_two_hundred(self) -> None:
        for index in range(250):
            self.store.add_event("info", f"event-{index}")

        rows = self.store.events()
        messages = {row["message"] for row in rows}

        self.assertLessEqual(len(rows), 200)
        self.assertIn("event-249", messages)
        self.assertNotIn("event-0", messages)


if __name__ == "__main__":
    unittest.main()
