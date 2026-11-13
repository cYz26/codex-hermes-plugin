"""Bound a serial native review without allowing a timed-out fork to outlive its owner."""
from __future__ import annotations

import logging
import os
import time
from contextlib import closing
from threading import Event, Lock, Thread

from .store import Store

REVIEW_DEADLINE = "review_deadline"
CANCEL_GRACE_SECONDS = 10
DEADLINE_EXIT = 124
logger = logging.getLogger(__name__)


def run_review(settings, api, handle, intent, attempt_id):
    finished, expired, cancelled = Event(), Event(), Event()
    lock = Lock()
    limit = settings.review_timeout_seconds
    error = f"Learning review reached its {limit:g}s wall-clock limit"

    def cancel():
        try:
            # Record before cancelling so a restarted worker can explain the
            # stop without changing an uncertain native attempt into a failure.
            try:
                with closing(Store(settings.state)) as recorder:
                    recorder.set("review-deadline:" + attempt_id, {"seconds": limit, "error": error})
            except Exception as exc:
                logger.warning("Could not record the review deadline (%s)", type(exc).__name__)
            api.cancel()
        except Exception as exc:
            logger.warning("Could not cancel the timed-out review (%s)", type(exc).__name__)
        finally:
            cancelled.set()

    def watchdog():
        if finished.wait(limit):
            return
        with lock:
            if finished.is_set():
                return
            expired.set()

        cutoff = time.monotonic() + CANCEL_GRACE_SECONDS
        Thread(target=cancel, name="learning-review-cancel", daemon=True).start()
        # Do not start another review while the old fork or its cancellation
        # can still touch the runtime. Native startup marks running attempts unknown.
        for event in (finished, cancelled):
            if not event.wait(max(0, cutoff - time.monotonic())):
                os._exit(DEADLINE_EXIT)

    monitor = Thread(target=watchdog, name="learning-review-deadline", daemon=True)
    monitor.start()
    try:
        result = api.run_review(handle, intent, attempt_id=attempt_id)
    finally:
        with lock:
            finished.set()
        monitor.join()
    if expired.is_set() and result["run_status"] in {"interrupted", "failed", "unknown"}:
        return {**result, "failure_reason": REVIEW_DEADLINE, "error": result.get("error") or error}
    return result
