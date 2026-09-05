"""Offload blocking calls to a caller-owned, shared thread pool."""

import asyncio
import contextvars
from functools import partial


async def offload(pool, function, /, *args, **kwargs):
    """Preserve context variables while using the pool's worker limit.

    Cancellation cannot stop a function already running in a thread.
    The pool limits running threads; it does not bound its submission queue.
    """
    context = contextvars.copy_context()
    call = partial(function, *args, **kwargs)
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(pool, context.run, call)
