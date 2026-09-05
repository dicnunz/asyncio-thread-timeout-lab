# Why Semaphore(1) can leave two Python threads running

I wanted to check whether a semaphore still limits blocking calls after an async timeout. I set its capacity to one, cancelled the first caller, and started another. Two worker threads stayed alive.

That result matters when an async server wraps a blocking database client, file operation, or SDK. A request can time out while the operation it started continues using a connection or modifying data. Retrying adds another operation.

Here is the pattern I tested:

```python
async def limited_call(semaphore, blocking_call):
    async with semaphore:
        return await asyncio.to_thread(blocking_call)
```

It looks reasonable: acquire capacity, run the operation, release capacity. The catch is that the context manager follows the lifetime of the awaiting coroutine. A running thread has its own lifetime.

## Reproduce it without guessing how long a thread takes

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

This makes the observation stronger than measuring a few sleep durations: both blocking calls have reported that they started, and neither has been allowed to finish. A lock protects the active-worker count.

The test suite separately checks `asyncio.wait_for`. It waits until the worker starts, applies a short timeout, then verifies that the worker still has not finished. The small timeout triggers cancellation; it is not used to guess whether the thread started.

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

For a blocking integration that must run at most one call at a time, I would give that integration a shared executor with one worker:

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

My check for this class of bug is simple: after cancelling the caller, inspect the resource doing the work. The useful limit belongs to that resource's lifetime.

## Verification

Six tests cover running-thread cancellation, real timeout behavior, semaphore overlap, queued cancellation, context and argument propagation, and worker exceptions. All six tests and the demo passed on CPython 3.11.13, 3.12.11, 3.13.1, and 3.14.6 on macOS arm64. See [test_offload.py](test_offload.py), [demo.py](demo.py), and the [captured results](verification.json).
