# Asyncio Timeouts

[Inspect a recorded timeline](https://dicnunz.github.io/demos/asyncio/) · [Download its event trace (JSON)](https://dicnunz.github.io/demos/asyncio/events.json)

A reproducible experiment showing why `Semaphore(1)` does not limit blocking calls to one after an async timeout. Cancelling the first caller releases its semaphore permit while its worker keeps running, allowing a second call to overlap.

That result matters when an async server wraps a blocking database client, file operation, or SDK. A request can time out while the operation it started continues using a connection or modifying data. Retrying adds another operation.

Here is the pattern I tested:

```python
async def limited_call(semaphore, blocking_call):
    async with semaphore:
        return await asyncio.to_thread(blocking_call)
```

It looks reasonable: acquire capacity, run the operation, release capacity. The catch is that the context manager follows the lifetime of the awaiting coroutine. A running thread has its own lifetime.

## Run

Run the included experiment with Python 3.11 or later. It uses only the standard library:

```sh
python3 demo.py
python3 -m unittest -v
```

The demo prints:

```text
Semaphore(1) + to_thread: peak active calls = 2
ThreadPoolExecutor(1):    peak active calls = 1
```

The first worker signals an event when it starts, then waits for a release event. The experiment cancels its caller only after receiving that signal. The second worker gets its own start event. Neither worker finishes until the experiment releases it.

Events coordinate the workers instead of relying on sleep durations. A lock protects the active-worker count.

The test suite separately checks `asyncio.wait_for`. It waits until the worker starts, applies a short timeout, then verifies that the worker still has not finished. The small timeout triggers cancellation; it is not used to guess whether the thread started.

## Export the measured timeline

Save a standalone HTML report and the events behind it with the same command:

```sh
python3 demo.py --report artifacts/report.html --trace artifacts/events.json
```

Open `artifacts/report.html` in a browser. Separate awaiter and worker lanes show the blocking call continuing after cancellation. Step through events or scrub the timeline to inspect the recorded run. The standalone report needs no server or network; its complete timeline and event ledgers also work without JavaScript.

Both flags are optional. Each invocation records a fresh experiment; using both flags exports the same run in both formats. Generated files in `artifacts/` are ignored by Git.

Timestamps use `time.perf_counter_ns()`, serialized under a lock. Each scenario starts at zero on the shared time scale. Durations include scheduling and tracing overhead and are not a performance comparison. Worker lanes measure blocking calls, not executor thread lifetimes.

The JSON format has `schema_version: 1`, UTC generation time, Python runtime metadata, clock and unit names, and a `scenarios` array. Each scenario includes its label, measured peak, and ordered events:

| Event field | Meaning |
| --- | --- |
| `sequence` | One-based order within this scenario |
| `elapsed_ns` | Integer nanoseconds since this scenario's first event |
| `kind` | Lifecycle transition, such as `awaiter_cancelled` or `worker_finished` |
| `call` | Call `1` or `2`, or `null` for a scenario-wide event |
| `active_calls` | Active blocking calls at this transition |

The demo waits for both workers to finish before exporting. The collector and HTML renderer are in [trace_report.py](trace_report.py).

## Follow the cancellation

There are five steps:

1. The coroutine acquires the semaphore's only permit.
2. `to_thread` submits the blocking function to the default executor.
3. Cancellation reaches the awaiting coroutine.
4. Leaving `async with` releases the permit.
5. The next coroutine acquires it while the first thread is still running.

Python documents that cancelling a `concurrent.futures.Future` cannot cancel a call that is already executing. That explains the thread's behavior. The semaphore also behaves correctly: its context manager exits when the coroutine unwinds. [Future cancellation](https://docs.python.org/3.14/library/concurrent.futures.html#concurrent.futures.Future.cancel), [task cancellation](https://docs.python.org/3.14/library/asyncio-task.html#task-cancellation).

Increasing the semaphore's capacity does not remove this mismatch. Repeated timeouts can leave more blocking calls running than its configured limit, up to the executor's own worker limit.

## Put the execution limit on a shared executor

For an integration that must run at most one blocking call at a time, share a `ThreadPoolExecutor(max_workers=1)` across calls:

```python
import asyncio
import contextvars
from concurrent.futures import ThreadPoolExecutor
from functools import partial

async def offload(pool, function, /, *args, **kwargs):
    context = contextvars.copy_context()
    call = partial(function, *args, **kwargs)
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(pool, context.run, call)
```

`offload.py` contains this helper. Passing the executor explicitly keeps unrelated blocking operations from sharing its capacity. Copying the context preserves request IDs and other context variables, matching an important convenience of `to_thread`. [Custom executors](https://docs.python.org/3.14/library/asyncio-eventloop.html#asyncio.loop.run_in_executor), [to_thread context propagation](https://docs.python.org/3.14/library/asyncio-task.html#asyncio.to_thread).

The pool must be shared across calls. Creating one pool per request would give every request its own worker allowance.

The accompanying tests show that the occupied worker remains occupied after its caller is cancelled. A second call waits in the executor queue. If cancellation reaches that queued job before it starts, it can be prevented from running. Once it starts, the original limitation applies.

## What this fix leaves to the application

A worker limit bounds running calls. The executor's submission queue still needs admission control under sustained load. A busy server should bound incoming work or reject it before that queue grows without limit.

The blocking library also needs its own timeouts. `wait_for` lets an async caller stop waiting; it cannot force a blocking network call to release its socket. A cancelled write may still succeed, so retry decisions need an idempotency or reconciliation policy.

Finally, pool shutdown waits for running work. Keep a shared pool alive for the service lifetime, and drain it during shutdown. Creating and closing it inside a request handler can make the event loop wait synchronously for the very operation that timed out.

## Verification

The original six tests cover running-thread cancellation, real timeout behavior, semaphore overlap, queued cancellation, context and argument propagation, and worker exceptions. The [captured results](verification.json) record those six tests and the original demo passing on CPython 3.11.13, 3.12.11, 3.13.1, and 3.14.6 on macOS arm64.

Five more tests check complete, ordered event traces; agreement between JSON and HTML; escaped report text; unchanged default output; and conflicting output paths. All 11 tests and report generation passed locally on CPython 3.12.13 on Linux. That local check does not establish results for other Python versions. The [GitHub Actions workflow](.github/workflows/tests.yml) runs the suite and report generation on Python 3.11–3.14 when pushed. See [test_offload.py](test_offload.py) and [test_trace_report.py](test_trace_report.py).
