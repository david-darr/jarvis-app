"""The channel outbox (roadmap phase 4, 2026-10-06; spec: the vault note
"Durable Work - Phase 4 (Build Spec)").

Every message Kairos sends to a comms channel by itself - a task's output,
an agent's question, report or result, a trigger asking for approval - is
queued here instead of sent on the spot, then sent from the queue:

- A send that fails is tried again after 1, 5, 15 and 60 minutes, then
  hourly, and given up after a day: the row is marked failed and the activity
  feed says so. Before this, a failed send was never retried, and agent and
  trigger messages failed without a trace.
- A row found mid-send when Kairos starts is marked unknown and never sent
  again. The send may well have landed; a second copy is worse than a gap.
  (Hermes Agent's cron/delivery_queue.py makes the same call.)
- The same message queued twice (one `key`) is one row, sent once.

The rows live in the session store (core/session_manager_store.py
`deliveries`, schema v5). A delivered row's text is cleared; the newest
KEEP_FINISHED finished rows are kept.
"""
import asyncio
import logging
import time
import uuid
from typing import Optional

from core import session_manager_store as store

logger = logging.getLogger(__name__)

RETRY_MINUTES = (1, 5, 15, 60)  # then hourly
GIVE_UP_SECONDS = 24 * 3600
KEEP_FINISHED = 1000
BATCH = 20
FINISHED = ("delivered", "failed", "unknown")

# One per event loop: an asyncio lock belongs to the loop it was used on.
_send_locks: dict = {}


def _row(row) -> Optional[dict]:
    return dict(row) if row is not None else None


def enqueue(key: str, channel: str, text: str, label: str = "") -> str:
    """Queue one message; its id. The same key again returns the first id and
    queues nothing. Sending starts at once when there is a running loop."""
    now = time.time()
    with store.transaction() as conn:
        found = conn.execute("SELECT id FROM deliveries WHERE key = ?", (key,)).fetchone()
        if found:
            return found["id"]
        delivery_id = uuid.uuid4().hex[:12]
        conn.execute("INSERT INTO deliveries (id, key, channel, text, label, status, attempts, next_try_at, created_at) "
                     "VALUES (?, ?, ?, ?, ?, 'pending', 0, ?, ?)", (delivery_id, key, channel, text, label, now, now))
    kick()
    return delivery_id


def get(delivery_id: str) -> Optional[dict]:
    with store.transaction() as conn:
        return _row(conn.execute("SELECT * FROM deliveries WHERE id = ?", (delivery_id,)).fetchone())


def public(delivery: Optional[dict]) -> Optional[dict]:
    """What a run's history shows: no message text."""
    if delivery is None:
        return None
    return {k: delivery[k] for k in ("id", "channel", "status", "attempts", "next_try_at", "last_error",
                                      "created_at", "finished_at")}


def _retry_delay(attempts: int) -> float:
    minutes = RETRY_MINUTES[attempts - 1] if attempts <= len(RETRY_MINUTES) else RETRY_MINUTES[-1]
    return minutes * 60


def _lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    for old in [l for l in _send_locks if l is not loop and l.is_closed()]:
        del _send_locks[old]
    return _send_locks.setdefault(loop, asyncio.Lock())


async def send_due(now: Optional[float] = None) -> int:
    """Send every message that is due; how many went. One sender at a time,
    so a message is never in two sends at once."""
    from core.channels import registry
    sent = 0
    async with _lock():
        now = time.time() if now is None else now
        with store.transaction() as conn:
            due = [dict(r) for r in conn.execute(
                "SELECT * FROM deliveries WHERE status = 'pending' AND next_try_at <= ? ORDER BY created_at LIMIT ?",
                (now, BATCH)).fetchall()]
        for row in due:
            with store.transaction() as conn:
                # Marked before the send: a crash from here on leaves it
                # "sending", which the next start reads as unknown.
                conn.execute("UPDATE deliveries SET status = 'sending', attempts = attempts + 1 WHERE id = ?",
                             (row["id"],))
            attempts = row["attempts"] + 1
            try:
                ok = await registry.send_to_channel(row["channel"], row["text"])
                error = None if ok else "the channel did not accept it (is it connected and set up?)"
            except Exception as e:  # a send must never take the loop down
                ok, error = False, f"{type(e).__name__}: {e}"
            finished = time.time()
            if ok:
                sent += 1
                _finish(row["id"], "delivered", None, finished, clear_text=True)
            elif finished - row["created_at"] >= GIVE_UP_SECONDS:
                _finish(row["id"], "failed", error, finished)
                _report_failure(row, error)
            else:
                with store.transaction() as conn:
                    conn.execute("UPDATE deliveries SET status = 'pending', next_try_at = ?, last_error = ? WHERE id = ?",
                                 (finished + _retry_delay(attempts), error, row["id"]))
                logger.warning("outbox: a message to %s was not delivered (attempt %d); trying again later",
                               row["channel"], attempts)
        if due:
            _prune()
    return sent


def _finish(delivery_id: str, status: str, error: Optional[str], at: float, clear_text: bool = False) -> None:
    with store.transaction() as conn:
        conn.execute("UPDATE deliveries SET status = ?, last_error = ?, finished_at = ?, next_try_at = NULL, "
                     "text = CASE WHEN ? THEN '' ELSE text END WHERE id = ?",
                     (status, error, at, int(clear_text), delivery_id))


def _report_failure(row: dict, error: Optional[str]) -> None:
    from core import events
    what = row.get("label") or "A message"
    events.emit("delivery.failed", f"{what}: delivery to {row['channel']} failed for a day and was given up ({error})",
                level="error", delivery_id=row["id"], channel=row["channel"])


def _prune() -> None:
    with store.transaction() as conn:
        conn.execute(f"DELETE FROM deliveries WHERE id IN (SELECT id FROM deliveries WHERE status IN "
                     f"({', '.join('?' for _ in FINISHED)}) ORDER BY finished_at DESC LIMIT -1 OFFSET ?)",
                     (*FINISHED, KEEP_FINISHED))


def recover() -> int:
    """At startup nothing is sending yet: a row still marked sending was cut
    off mid-send, and is marked unknown rather than sent again."""
    now = time.time()
    with store.transaction() as conn:
        cut = conn.execute("UPDATE deliveries SET status = 'unknown', finished_at = ?, next_try_at = NULL, "
                           "last_error = 'Kairos closed while sending it; it may or may not have arrived' "
                           "WHERE status = 'sending'", (now,)).rowcount
    if cut:
        logger.warning("outbox: %d message(s) were cut off mid-send by Kairos closing; not sent again", cut)
    return cut


def retry(delivery_id: str) -> dict:
    """A person asks for a failed or unknown message to be sent again."""
    delivery = get(delivery_id)
    if delivery is None:
        raise KeyError(delivery_id)
    if delivery["status"] not in ("failed", "unknown"):
        raise ValueError("only a failed or unknown delivery can be sent again")
    now = time.time()
    with store.transaction() as conn:
        # A fresh day of retries from now.
        conn.execute("UPDATE deliveries SET status = 'pending', next_try_at = ?, created_at = ?, finished_at = NULL "
                     "WHERE id = ?", (now, now, delivery_id))
    kick()
    return get(delivery_id)


def kick() -> None:
    """Send what is due now, in the background, when there is a running
    loop; otherwise the task loop's next pass sends it."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(send_due())
