"""Show the difference between limiting awaiters and limiting workers."""

import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import threading

from offload import offload
from trace_report import ScenarioTrace, build_trace, render_html


async def measure(use_dedicated_pool, trace=None):
    loop = asyncio.get_running_loop()
    release = threading.Event()
    started = [asyncio.Event(), asyncio.Event()]
    finished = [asyncio.Event(), asyncio.Event()]
    lock = threading.Lock()
    active = peak = 0
    semaphore = asyncio.Semaphore(1)
    pool = ThreadPoolExecutor(max_workers=1) if use_dedicated_pool else None

    def record(kind, index=None):
        with lock:
            if trace is not None:
                trace.record(kind, index, active)

    def blocking_call(index):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            if trace is not None:
                trace.record("worker_started", index, active)
        loop.call_soon_threadsafe(started[index].set)
        try:
            if not release.wait(5):
                raise TimeoutError("demo worker was not released")
        finally:
            with lock:
                active -= 1
                if trace is not None:
                    trace.record("worker_finished", index, active)
            loop.call_soon_threadsafe(finished[index].set)

    async def call(index):
        record("awaiter_started", index)
        try:
            if pool is not None:
                result = await offload(pool, blocking_call, index)
            else:
                async with semaphore:
                    result = await asyncio.to_thread(blocking_call, index)
        except asyncio.CancelledError:
            record("awaiter_cancelled", index)
            raise
        else:
            record("awaiter_completed", index)
            return result

    second = None
    record("scenario_started")
    first = asyncio.create_task(call(0))
    try:
        await asyncio.wait_for(started[0].wait(), 2)
        record("cancellation_requested", 0)
        first.cancel()
        try:
            await first
        except asyncio.CancelledError:
            pass
        second = asyncio.create_task(call(1))
        if pool is None:
            await asyncio.wait_for(started[1].wait(), 2)
        else:
            # Let call(1) submit while the only worker is still occupied.
            await asyncio.sleep(0)
            assert not started[1].is_set()
    finally:
        record("workers_released")
        release.set()
        try:
            if not first.done():
                first.cancel()
            await asyncio.gather(first, return_exceptions=True)
            if second is not None:
                await asyncio.wait_for(second, 2)
            # A cancelled awaiter cannot tell us when its blocking call ends.
            # Drain its separate finish signal before exporting the trace.
            if started[0].is_set():
                await asyncio.wait_for(finished[0].wait(), 2)
        finally:
            if pool is not None:
                pool.shutdown(wait=True)
    record("scenario_finished")
    return peak


async def run_experiment():
    scenarios = []
    for dedicated, scenario_id, label in (
        (False, "semaphore", "Semaphore(1) + to_thread"),
        (True, "dedicated_pool", "ThreadPoolExecutor(1)"),
    ):
        trace = ScenarioTrace(scenario_id, label)
        peak = await measure(dedicated, trace)
        scenarios.append(trace.snapshot(peak))
    naive, dedicated = (item["peak_active_calls"] for item in scenarios)
    assert (naive, dedicated) == (2, 1)
    return build_trace(scenarios)


async def main(trace_path=None, report_path=None):
    result = await run_experiment()
    naive, dedicated = (item["peak_active_calls"] for item in result["scenarios"])
    print(f"Semaphore(1) + to_thread: peak active calls = {naive}")
    print(f"ThreadPoolExecutor(1):    peak active calls = {dedicated}")
    for path, content in (
        (trace_path, lambda: json.dumps(result, indent=2) + "\n"),
        (report_path, lambda: render_html(result)),
    ):
        if path is not None:
            path = Path(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content(), encoding="utf-8")


def cli():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace", type=Path, metavar="PATH", help="save measured events as JSON")
    parser.add_argument("--report", type=Path, metavar="PATH", help="save a standalone HTML timeline")
    args = parser.parse_args()
    if args.trace and args.report and args.trace.resolve() == args.report.resolve():
        parser.error("--trace and --report must use different output paths")
    try:
        asyncio.run(main(args.trace, args.report))
    except OSError as error:
        parser.exit(1, f"Could not write output: {error}\n")


if __name__ == "__main__":
    cli()
