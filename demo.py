"""Show the difference between limiting awaiters and limiting workers."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import threading

from offload import offload


async def measure(use_dedicated_pool):
    loop = asyncio.get_running_loop()
    release = threading.Event()
    started = [asyncio.Event(), asyncio.Event()]
    lock = threading.Lock()
    active = peak = 0
    semaphore = asyncio.Semaphore(1)
    pool = ThreadPoolExecutor(max_workers=1) if use_dedicated_pool else None

    def blocking_call(index):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        loop.call_soon_threadsafe(started[index].set)
        try:
            if not release.wait(5):
                raise TimeoutError("demo worker was not released")
        finally:
            with lock:
                active -= 1

    async def call(index):
        if pool is not None:
            return await offload(pool, blocking_call, index)
        async with semaphore:
            return await asyncio.to_thread(blocking_call, index)

    second = None
    first = asyncio.create_task(call(0))
    try:
        await asyncio.wait_for(started[0].wait(), 2)
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
        release.set()
        if second is not None:
            await asyncio.wait_for(second, 2)
        if pool is not None:
            pool.shutdown(wait=True)
    return peak


async def main():
    naive = await measure(False)
    dedicated = await measure(True)
    assert (naive, dedicated) == (2, 1)
    print(f"Semaphore(1) + to_thread: peak active calls = {naive}")
    print(f"ThreadPoolExecutor(1):    peak active calls = {dedicated}")


if __name__ == "__main__":
    asyncio.run(main())
