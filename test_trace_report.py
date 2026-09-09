"""Check recorded lifetimes, command-line output, and report escaping."""

import asyncio
import copy
from html.parser import HTMLParser
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from demo import run_experiment
from trace_report import render_html


REPO = Path(__file__).resolve().parent
EXPECTED_OUTPUT = (
    "Semaphore(1) + to_thread: peak active calls = 2\n"
    "ThreadPoolExecutor(1):    peak active calls = 1\n"
)


class TraceTests(unittest.IsolatedAsyncioTestCase):
    async def test_trace_records_complete_ordered_worker_lifetimes(self):
        trace = await run_experiment()
        self.assertEqual(trace["schema_version"], 1)
        self.assertEqual(trace["time_unit"], "ns")
        for scenario, expected_peak in zip(trace["scenarios"], (2, 1)):
            with self.subTest(scenario=scenario["id"]):
                events = scenario["events"]
                self.assertEqual(events[0]["kind"], "scenario_started")
                self.assertEqual(events[0]["elapsed_ns"], 0)
                self.assertEqual(events[-1]["kind"], "scenario_finished")
                self.assertEqual([e["sequence"] for e in events], list(range(1, len(events) + 1)))
                times = [e["elapsed_ns"] for e in events]
                self.assertEqual(times, sorted(times))
                self.assertTrue(all(isinstance(value, int) for value in times))
                running = set()
                starts, finishes = [], []
                for event in events:
                    if event["kind"] == "worker_started":
                        self.assertNotIn(event["call"], running)
                        running.add(event["call"])
                        starts.append(event["call"])
                    elif event["kind"] == "worker_finished":
                        self.assertIn(event["call"], running)
                        running.remove(event["call"])
                        finishes.append(event["call"])
                    self.assertEqual(event["active_calls"], len(running))
                self.assertEqual(sorted(starts), [1, 2])
                self.assertEqual(sorted(finishes), [1, 2])
                self.assertFalse(running)
                self.assertEqual(scenario["peak_active_calls"], expected_peak)
                self.assertEqual(max(e["active_calls"] for e in events), expected_peak)
                order = {(e["kind"], e["call"]): e["sequence"] for e in events}
                for call in (1, 2):
                    self.assertLess(order["awaiter_started", call], order["worker_started", call])
                    self.assertLess(order["worker_started", call], order["worker_finished", call])
                self.assertLess(order["worker_started", 1], order["cancellation_requested", 1])
                self.assertLess(order["cancellation_requested", 1], order["awaiter_cancelled", 1])
                self.assertLess(order["awaiter_cancelled", 1], order["worker_finished", 1])
                self.assertLess(order["awaiter_cancelled", 1], order["awaiter_started", 2])
                self.assertLess(order["workers_released", None], order["worker_finished", 1])
                self.assertLess(order["worker_finished", 2], order["awaiter_completed", 2])
                if scenario["id"] == "semaphore":
                    self.assertLess(order["worker_started", 2], order["workers_released", None])
                else:
                    self.assertLess(order["worker_finished", 1], order["worker_started", 2])


class ReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.trace = asyncio.run(run_experiment())

    def test_report_escapes_trace_text_and_is_self_contained(self):
        trace = copy.deepcopy(self.trace)
        unsafe = '<script>alert("trace")</script> & <img src=x onerror=alert(1)>'
        trace["scenarios"][0]["label"] = unsafe
        trace["runtime"]["system"] = unsafe
        trace["generated_at"] = unsafe
        trace["scenarios"][0]["events"][0]["kind"] = unsafe
        trace["scenarios"][0]["events"][0]["call"] = unsafe
        report = render_html(trace)
        self.assertNotIn(unsafe, report)
        self.assertIn("&lt;script&gt;", report)
        self.assertIn("&quot;trace&quot;", report)
        self.assertIn("&amp;", report)

        class Resources(HTMLParser):
            def __init__(self):
                super().__init__()
                self.external = []

            def handle_starttag(self, tag, attrs):
                if tag in ("link", "img", "iframe") or (tag == "script" and "src" in dict(attrs)):
                    self.external.append(tag)

        parser = Resources()
        parser.feed(report)
        self.assertEqual(parser.external, [])
        self.assertIn('lang="en"', report)
        self.assertEqual(report.count('<div class="timeline" role="img"'), 2)
        self.assertEqual(report.count("<caption>"), 2)

    def test_cli_exports_both_formats_and_preserves_console_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "nested"
            trace_path, report_path = directory / "events.json", directory / "report.html"
            result = subprocess.run(
                [sys.executable, str(REPO / "demo.py"), "--trace", str(trace_path), "--report", str(report_path)],
                check=True, capture_output=True, text=True, timeout=15,
            )
            self.assertEqual(result.stdout, EXPECTED_OUTPUT)
            self.assertEqual(result.stderr, "")
            trace = json.loads(trace_path.read_text(encoding="utf-8"))
            self.assertEqual([s["peak_active_calls"] for s in trace["scenarios"]], [2, 1])
            # The HTML must be generated from exactly the same run as the JSON.
            self.assertEqual(report_path.read_text(encoding="utf-8"), render_html(trace))

    def test_cli_without_flags_keeps_original_output(self):
        result = subprocess.run(
            [sys.executable, str(REPO / "demo.py")],
            check=True, capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.stdout, EXPECTED_OUTPUT)
        self.assertEqual(result.stderr, "")

    def test_cli_rejects_shared_output_path_before_running(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "report.html"
            result = subprocess.run(
                [sys.executable, str(REPO / "demo.py"), "--trace", str(output), "--report", str(output)],
                capture_output=True, text=True, timeout=15,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("different output paths", result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
