"""Run with: python3 -m unittest -v

Events coordinate worker lifetimes. Timeouts only bound a hung test, except
the one test that explicitly checks asyncio.wait_for's timeout behavior.
"""

import asyncio
import contextvars
from concurrent.futures import ThreadPoolExecutor
import threading
import unittest

from offload import offload


class Gate:
    def __init__(self):
        self.loop = asyncio.get_running_loop()
        self.started = asyncio.Event()
        self.finished = asyncio.Event()
        self.release = threading.Event()

    def run(self):
        self.loop.call_soon_threadsafe(self.started.set)
        try:
            if not self.release.wait(5):
                raise TimeoutError("test did not release worker")
            return 42
        finally:
            self.loop.call_soon_threadsafe(self.finished.set)

    async def wait_started(self):
        await asyncio.wait_for(self.started.wait(), 2)

    async def drain(self):
        self.release.set()
        await asyncio.wait_for(self.finished.wait(), 2)


class OffloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelling_awaiter_leaves_started_thread_running(self):
        gate = Gate()
        task = asyncio.create_task(asyncio.to_thread(gate.run))
        try:
            await gate.wait_started()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertFalse(gate.finished.is_set())
        finally:
            await gate.drain()

    async def test_timeout_leaves_started_thread_running(self):
        gate = Gate()
        task = asyncio.create_task(asyncio.to_thread(gate.run))
        try:
            await gate.wait_started()
            with self.assertRaises(TimeoutError):
                await asyncio.wait_for(task, 0.01)
            self.assertFalse(gate.finished.is_set())
        finally:
            await gate.drain()

    async def test_semaphore_releases_capacity_before_thread_finishes(self):
        semaphore = asyncio.Semaphore(1)
        first, second = Gate(), Gate()

        async def limited(gate):
            async with semaphore:
                return await asyncio.to_thread(gate.run)

        first_task = asyncio.create_task(limited(first))
        second_task = None
        try:
            await first.wait_started()
            first_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first_task
            second_task = asyncio.create_task(limited(second))
            await second.wait_started()
            # Both blocking calls are alive despite Semaphore(1).
            self.assertFalse(first.finished.is_set())
            self.assertFalse(second.finished.is_set())
        finally:
            first.release.set()
            second.release.set()
            await first.drain()
            if second_task is not None:
                await asyncio.wait_for(second_task, 2)

    async def test_pool_keeps_queued_cancelled_work_from_running(self):
        first = Gate()
        second_ran = threading.Event()
        with ThreadPoolExecutor(max_workers=1) as pool:
            first_task = asyncio.create_task(offload(pool, first.run))
            try:
                await first.wait_started()
                first_task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await first_task
                # The first physical worker remains occupied. The next job
                # enters the executor queue, where cancellation can stop it.
                queued = asyncio.get_running_loop().run_in_executor(
                    pool, second_ran.set
                )
                self.assertFalse(second_ran.is_set())
                queued.cancel()
                # Let wrap_future propagate cancellation to the executor.
                await asyncio.sleep(0)
                await first.drain()
                # A sentinel proves that the executor advanced past job two.
                await asyncio.get_running_loop().run_in_executor(pool, lambda: None)
                self.assertFalse(second_ran.is_set())
            finally:
                first.release.set()

    async def test_offload_preserves_context_arguments_and_result(self):
        request_id = contextvars.ContextVar("request_id", default="unset")
        token = request_id.set("request-17")
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                result = await offload(
                    pool, lambda n, *, factor: (request_id.get(), n * factor),
                    6, factor=7,
                )
                self.assertEqual(result, ("request-17", 42))
        finally:
            request_id.reset(token)

    async def test_worker_exception_reaches_caller(self):
        def broken():
            raise ValueError("worker failed")

        with ThreadPoolExecutor(max_workers=1) as pool:
            with self.assertRaisesRegex(ValueError, "worker failed"):
                await offload(pool, broken)


if __name__ == "__main__":
    unittest.main()
