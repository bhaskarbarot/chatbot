"""
Async batch metrics writer.

Events are enqueued (non-blocking) and written to JSONL files by a single
background thread in batches.  Under load, multiple events are flushed in one
file.write() call, reducing I/O overhead from O(n) opens to O(batch) opens.

The queue has a hard cap (MAX_QUEUE_SIZE) — events are silently dropped when
the queue is full rather than blocking the request path.
"""
from __future__ import annotations

import json
import os
import queue
import threading
from datetime import datetime
from typing import Any, Dict

from config import LOG_DIR

MAX_QUEUE_SIZE = 10_000
FLUSH_INTERVAL_S = 2.0

_event_queue: queue.Queue = queue.Queue(maxsize=MAX_QUEUE_SIZE)
_writer_started = False
_start_lock = threading.Lock()


def _writer_loop() -> None:
    """Background writer: drain queue and batch-write to daily JSONL file."""
    while True:
        # Block until at least one event arrives
        events = []
        try:
            events.append(_event_queue.get(timeout=FLUSH_INTERVAL_S))
        except queue.Empty:
            continue

        # Drain remaining events without additional blocking
        while True:
            try:
                events.append(_event_queue.get_nowait())
            except queue.Empty:
                break

        if not events:
            continue

        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            day = datetime.now().strftime("%Y%m%d")
            filepath = os.path.join(LOG_DIR, f"metrics_{day}.jsonl")
            lines = "\n".join(json.dumps(e, default=str) for e in events) + "\n"
            with open(filepath, "a", encoding="utf-8") as fh:
                fh.write(lines)
        except Exception:
            # Metrics must never crash the application
            pass


def _ensure_writer() -> None:
    global _writer_started
    if _writer_started:
        return
    with _start_lock:
        if not _writer_started:
            t = threading.Thread(
                target=_writer_loop,
                daemon=True,
                name="metrics-writer",
            )
            t.start()
            _writer_started = True


def record_event(event_type: str, payload: Dict[str, Any]) -> None:
    """
    Enqueue one structured event for async writing (never blocks the caller).
    Silently drops events when the queue is at capacity.
    """
    _ensure_writer()
    try:
        event: Dict[str, Any] = {
            "ts": datetime.now().isoformat(),
            "event_type": event_type,
            **payload,
        }
        _event_queue.put_nowait(event)
    except (queue.Full, Exception):
        pass
