"""
Daily posting scheduler - runs continuously (e.g. inside tmux).

How it works:
- Each day, picks one random time inside every window defined in WINDOWS
  (default: a midday window + an evening window = two posts a day)
- Waits until each slot, then calls the publish function from main.py
- After the day's slots are done, sleeps until just after midnight
- Repeats the next day with new random times
"""

import os
import time
import random
import logging
from datetime import datetime, timedelta

# Publish function and state file from main.py
from main import post_daily_tweet, LOG_FILE

# Edit this list to change the number and times of daily windows (24-hour clock)
WINDOWS = [
    (12, 14),  # Midday window: 12:00 - 14:00
    (20, 23),  # Evening window: 20:00 - 23:00
]

# Logging - separate logger writing to scheduler.log next to the code
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
log = logging.getLogger("scheduler")
if not log.handlers:
    _fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
    _fh = logging.FileHandler(os.path.join(BASE_DIR, "scheduler.log"), encoding="utf-8")
    _fh.setFormatter(_fmt)
    _sh = logging.StreamHandler()
    _sh.setFormatter(_fmt)
    log.addHandler(_fh)
    log.addHandler(_sh)
    log.setLevel(logging.INFO)
    log.propagate = False


def build_day_slots(base_day, only_after=None):
    """
    Build a list of random slots (one per window) for base_day.
    only_after: if given, skip any window that already ended before this time;
    if we are currently inside a window, pick from now rather than the window start.
    Returns a sorted list of datetimes.
    """
    slots = []
    for start_h, end_h in WINDOWS:
        w_start = base_day.replace(hour=start_h, minute=0, second=0, microsecond=0)
        w_end = base_day.replace(hour=end_h, minute=0, second=0, microsecond=0)

        if only_after is not None:
            if only_after >= w_end:
                continue  # This window has fully passed, skip it
            if only_after > w_start:
                w_start = only_after  # We are inside the window now, start from now

        window_seconds = int((w_end - w_start).total_seconds())
        if window_seconds <= 0:
            window_seconds = 60
        
        offset = random.randint(0, window_seconds)
        slots.append(w_start + timedelta(seconds=offset))

    return sorted(slots)


def last_post_time():
    """Time of the last successful post from posted_log.json (or None)."""
    try:
        import json
        with open(LOG_FILE, encoding="utf-8") as f:
            return datetime.fromisoformat(json.load(f)["last_post_at"])
    except Exception:
        return None


def drop_already_posted_windows(slots, day):
    """Drop slots for any window that already has a post today (e.g. after a restart)."""
    last = last_post_time()
    if last is None or last.date() != day.date():
        return slots
    kept = []
    for slot in slots:
        for start_h, end_h in WINDOWS:
            w_start = day.replace(hour=start_h, minute=0, second=0, microsecond=0)
            w_end = day.replace(hour=end_h, minute=0, second=0, microsecond=0)
            if w_start <= slot <= w_end and w_start <= last <= w_end:
                log.info(f"⏭️ Already posted in the {start_h}:00-{end_h}:00 window today ({last:%H:%M}), skipping it.")
                break
        else:
            kept.append(slot)
    return kept


def get_todays_or_tomorrows_slots():
    """Build today's remaining slots if any, otherwise all of tomorrow's."""
    now = datetime.now()
    today_slots = drop_already_posted_windows(build_day_slots(now, only_after=now), now)
    if today_slots:
        return today_slots
    tomorrow = now + timedelta(days=1)
    return build_day_slots(tomorrow)


def run_scheduler():
    windows_desc = " and ".join(f"{s}:00-{e}:00" for s, e in WINDOWS)
    log.info(f"🟢 Scheduler started. Posting {len(WINDOWS)} time(s) a day in windows: {windows_desc}")

    while True:
        slots = get_todays_or_tomorrows_slots()
        log.info("📅 Upcoming slots: " + ", ".join(s.strftime("%Y-%m-%d %H:%M:%S") for s in slots))

        for run_at in slots:
            # Recompute the remaining time each loop to avoid time drift
            while True:
                wait_seconds = (run_at - datetime.now()).total_seconds()
                if wait_seconds <= 0:
                    break
                
                # Log the next slot while more than an hour remains (handy when watching tmux)
                if wait_seconds > 3600:
                    log.info(f"⏰ Next slot: {run_at.strftime('%Y-%m-%d %H:%M:%S')} (in about {wait_seconds/3600:.1f} hours)")
                
                # Sleep at most an hour, then re-evaluate
                time.sleep(min(wait_seconds, 3600))

            try:
                # Publish (retries internally on network errors)
                if post_daily_tweet():
                    log.info(f"✅ Slot {run_at.strftime('%H:%M:%S')}: posted.")
                else:
                    log.error(f"❌ Slot {run_at.strftime('%H:%M:%S')}: posting failed - see poster.log.")
            except Exception as e:
                log.error(f"❌ Error while running the posting cycle: {e}")

            time.sleep(5)  # Short safety pause after posting

        # Prevent a spam loop: once all of today's slots are done,
        # sleep until 00:01 the next day
        now = datetime.now()
        tomorrow_midnight = (now + timedelta(days=1)).replace(hour=0, minute=1, second=0, microsecond=0)
        
        log.info("🌙 Today's slots are done. Sleeping until tomorrow...")
        
        while True:
            wait_for_next_day = (tomorrow_midnight - datetime.now()).total_seconds()
            if wait_for_next_day <= 0:
                break
            time.sleep(min(wait_for_next_day, 3600))


if __name__ == "__main__":
    run_scheduler()
