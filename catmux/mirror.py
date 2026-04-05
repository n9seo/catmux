"""
catmux.mirror - Radio state mirror / cache

Inspired by LP-Bridge's "virtual rig" concept.

The mirror keeps a snapshot of the radio's current state (frequency, mode,
VFO, PTT status, etc.) and answers incoming GET queries from virtual ports
immediately from cache, without forwarding them to the real radio.

Only SET commands and uncached GET commands are forwarded to the real port.
This:
  - Eliminates collisions between multiple polling apps
  - Reduces serial traffic to the radio significantly
  - Allows instant response for the most common queries (FA, FB, MD, IF...)
  - Prevents apps from hammering a slow radio with redundant polls

Thread safety: all public methods are safe to call from multiple threads.
"""

import threading
import time
import logging

log = logging.getLogger(__name__)


class MirrorCache:
    """
    Key-value store for radio state, keyed by command name (e.g. "FA", "MD").

    Each entry stores:
        value  : the last known response bytes for this command
        ts     : timestamp of last update
        stale  : True if we've requested a refresh but not yet received it
    """

    def __init__(self, ttl: float = 1.0):
        """
        ttl: seconds after which a cached value is considered stale
             and will trigger a background re-poll even if the app is
             answered from cache.
        """
        self.ttl = ttl
        self._lock = threading.RLock()
        self._cache: dict[str, dict] = {}

    def update(self, key: str, response: bytes):
        """Store a fresh response for this command key."""
        with self._lock:
            self._cache[key] = {
                "value": response,
                "ts":    time.monotonic(),
                "stale": False,
            }
        log.debug(f"Mirror update: {key!r} = {response!r}")

    def get(self, key: str) -> bytes | None:
        """
        Return cached value if fresh, or None if not cached / too stale.
        Marks entry as stale if TTL exceeded (triggers background re-poll).
        """
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                return None
            age = time.monotonic() - entry["ts"]
            if age > self.ttl:
                entry["stale"] = True
                # Still return the value — a background re-poll will update it
                # Better to return slightly stale data than to block the client
                log.debug(f"Mirror stale ({age:.2f}s): {key!r}")
            return entry["value"]

    def is_stale(self, key: str) -> bool:
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                return True
            return entry.get("stale", False) or \
                   (time.monotonic() - entry["ts"] > self.ttl)

    def mark_stale(self, key: str):
        """Force a key to be re-polled on next access."""
        with self._lock:
            if key in self._cache:
                self._cache[key]["stale"] = True

    def invalidate_all(self):
        """Mark everything stale (e.g. after reconnect)."""
        with self._lock:
            for entry in self._cache.values():
                entry["stale"] = True

    def keys(self) -> list[str]:
        with self._lock:
            return list(self._cache.keys())

    def snapshot(self) -> dict:
        """Return a copy of the full cache for status display."""
        with self._lock:
            return {
                k: {
                    "value": v["value"],
                    "age_s": round(time.monotonic() - v["ts"], 2),
                    "stale": v["stale"],
                }
                for k, v in self._cache.items()
            }


# ---------------------------------------------------------------------------
# Per-family poll schedules
# ---------------------------------------------------------------------------

# Commands polled frequently (fast-changing: freq, mode, tx state)
# Commands polled less often (slow-changing: power, AGC, NR settings)
# Format: {command_key: poll_interval_seconds}

POLL_SCHEDULE_YAESU = {
    "FA":  0.5,   # VFO A frequency
    "FB":  0.5,   # VFO B frequency
    "TX":  0.5,   # TX state
    "IF":  1.0,   # composite info
    "PC":  5.0,   # TX power
    "AG":  5.0,   # AF gain
    "SQ":  5.0,   # squelch
    "SM":  2.0,   # S-meter
    "RA":  5.0,   # attenuator
    "PA":  5.0,   # preamp
    "MD":  2.0,   # mode
}

POLL_SCHEDULE_KENWOOD = {
    "FA":  0.2,
    "FB":  0.2,
    "MD":  0.5,
    "TX":  0.2,
    "IF":  0.5,
    "PC":  2.0,
    "AG":  2.0,
    "SM":  1.0,
}

# Elecraft uses Kenwood-heritage commands — same schedule
POLL_SCHEDULE_ELECRAFT = {
    **POLL_SCHEDULE_KENWOOD,
    "BW":  1.0,   # filter bandwidth (Elecraft extension)
    "DS":  2.0,   # display (Elecraft)
    "TQ":  0.2,   # TX state (Elecraft)
}

# Icom CI-V: keys are symbolic names from CIVFramer.CMD_NAMES
POLL_SCHEDULE_ICOM = {
    "FREQ":  0.2,
    "MODE":  0.5,
    "TX":    0.2,
    "METER": 1.0,
    "LEVEL_01": 2.0,  # AF level
    "LEVEL_02": 2.0,  # RF gain
}

POLL_SCHEDULES = {
    "yaesu":    POLL_SCHEDULE_YAESU,
    "kenwood":  POLL_SCHEDULE_KENWOOD,
    "elecraft": POLL_SCHEDULE_ELECRAFT,
    "icom":     POLL_SCHEDULE_ICOM,
}


class Poller:
    """
    Tracks when each mirrored command was last polled so the
    serial thread knows what to send next during idle time.

    This is a simple priority queue based on next-due timestamps.
    """

    def __init__(self, schedule: dict[str, float]):
        self._lock = threading.Lock()
        # {key: next_due_monotonic}
        self._due: dict[str, float] = {k: 0.0 for k in schedule}
        self._intervals = dict(schedule)

    def due_keys(self) -> list[str]:
        """Return list of keys whose poll interval has elapsed, oldest first."""
        now = time.monotonic()
        with self._lock:
            overdue = [(due, key) for key, due in self._due.items() if due <= now]
        overdue.sort()
        return [key for _, key in overdue]

    def mark_sent(self, key: str):
        """Record that a poll was just sent for this key."""
        with self._lock:
            self._due[key] = time.monotonic() + self._intervals[key]

    def remove(self, key: str):
        """Remove a key from the poll schedule (e.g. radio returned error)."""
        with self._lock:
            self._due.pop(key, None)
            self._intervals.pop(key, None)
        log.debug(f"Poller: removed '{key}' from schedule")

    def next_due_in(self) -> float:
        """Seconds until the next poll is due (0 if something is already overdue)."""
        now = time.monotonic()
        with self._lock:
            if not self._due:
                return 1.0
            earliest = min(self._due.values())
        return max(0.0, earliest - now)
