"""
background_tasks.py - Central housekeeping loop for mcp-itop.

A single asyncio task runs all periodic cleanup activities at the interval
configured by CLEANUP_INTERVAL (env var, default 300 s). Every subsystem
that needs periodic maintenance registers its cleanup function here so there
is exactly one interval knob in the environment.

Registered cleanup activities:
  - cache_cleanup()                    : evict stale resolve_key cache entries
  - purge_expired_images()             : delete expired attachment_sessions rows
  - purge_expired_inline_image_refs()  : delete expired inline_image_refs rows

Start the loop from server.py via asyncio.create_task(housekeeping_loop())
after the event loop is running.
"""

from __future__ import annotations

import asyncio

from cache import cache_cleanup
from attachment_store import purge_expired_images, purge_expired_inline_image_refs
from config import CLEANUP_INTERVAL, logger


async def housekeeping_loop() -> None:
    """Run all cleanup functions on every CLEANUP_INTERVAL tick.

    Designed to run as a long-lived asyncio background task. Each activity
    is called in sequence; exceptions are caught and logged so a failure in
    one activity does not abort the others or kill the task.
    """
    logger.info(
        "[housekeeping] loop started, interval=%ds", CLEANUP_INTERVAL
    )
    while True:
        await asyncio.sleep(CLEANUP_INTERVAL)
        logger.debug("[housekeeping] cycle start")

        try:
            cache_cleanup()
        except Exception as exc:
            logger.warning("[housekeeping] cache_cleanup failed: %s", exc)

        try:
            removed = purge_expired_images()
            if removed:
                logger.debug(
                    "[housekeeping] purge_expired_images: removed %d row(s)", removed
                )
        except Exception as exc:
            logger.warning("[housekeeping] purge_expired_images failed: %s", exc)

        try:
            removed = purge_expired_inline_image_refs()
            if removed:
                logger.debug(
                    "[housekeeping] purge_expired_inline_image_refs: removed %d row(s)",
                    removed,
                )
        except Exception as exc:
            logger.warning(
                "[housekeeping] purge_expired_inline_image_refs failed: %s", exc
            )

        logger.debug("[housekeeping] cycle complete")
