"""Consent is allowed only inside an explicitly invoked operation, never a probe."""

from contextvars import ContextVar

requests_allowed = ContextVar("permission_requests_allowed", default=False)


async def wait_for_grant(probe, *, seconds=60):
    """Resume only the waiting operation; never replay a scenario or submitted action."""
    import asyncio
    import time

    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        result = await probe()
        if result.get("automation") == "granted":
            return result
        await asyncio.sleep(1)
    return await probe()
