"""
Independent dead-bot watchdog. Runs as its OWN process (separate from the bot),
so it can detect and alert when the bot dies or hangs.

Mechanism: the bot rewrites src/exports/v29_topstep_runtime_state.json every loop
(~once/min). If that file's `updated_at` goes stale beyond STALE_THRESHOLD_MIN,
the bot is down or hung -> Telegram alert. Alerts only on state TRANSITIONS
(down / recovered), so no spam. Deliberately minimal: only stdlib + dotenv, no
import of the bot's code, so a broken bot can't break the watchdog.
"""
import json
import os
import time
from datetime import datetime, timezone
from urllib import parse, request, error

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
except Exception:
    pass

STATE_PATH = os.path.join(os.path.dirname(__file__), "src", "exports", "v29_topstep_runtime_state.json")
STALE_THRESHOLD_MIN = 6      # bot updates ~every minute; 6 min stale = down/hung
CHECK_INTERVAL_SEC = 120     # how often the watchdog checks
HALT_PATH = os.path.join(os.path.dirname(__file__), "src", "exports", "HALT.txt")


def _send_telegram(text):
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat:
        return
    data = parse.urlencode({"chat_id": chat, "text": text}).encode("utf-8")
    req = request.Request(f"https://api.telegram.org/bot{token}/sendMessage",
                          data=data, method="POST")
    for _ in range(3):
        try:
            with request.urlopen(req, timeout=15):
                return
        except (error.URLError, TimeoutError, OSError):
            time.sleep(2.0)


def _minutes_stale():
    """Minutes since the bot last wrote its state, or None if unreadable.

    The bot writes the state file non-atomically (truncate-then-write), so a
    read can occasionally catch a torn/partial file. Retry once before giving
    up so a transient torn read doesn't fire a false 'BOT DOWN' alert.
    """
    for attempt in range(2):
        try:
            with open(STATE_PATH, encoding="utf-8") as fh:
                updated = json.load(fh).get("updated_at")
            if not updated:
                return None
            ts = datetime.fromisoformat(str(updated))
            now = datetime.now(timezone.utc)
            return (now - ts).total_seconds() / 60.0
        except FileNotFoundError:
            return None
        except Exception:
            if attempt == 0:
                time.sleep(0.5)  # let an in-flight write finish, then retry
                continue
            return None


def main():
    print(f"Watchdog started. Watching {STATE_PATH}")
    _send_telegram("🐕 Watchdog started — I'll alert you if the bot goes down.")
    healthy = True  # assume healthy until proven otherwise
    while True:
        try:
            stale = _minutes_stale()
            halted = os.path.exists(HALT_PATH)
            fresh = stale is not None and stale <= STALE_THRESHOLD_MIN
            # A manual HALT is intentional, not a failure -> don't raise a DOWN
            # alarm on it. But RECOVERED must require GENUINE freshness, not just
            # "not down" — otherwise halting an already-crashed bot would falsely
            # report it recovered.
            down = (not fresh) and not halted
            if down and healthy:
                detail = "state file missing" if stale is None else f"no update for {stale:.0f} min"
                _send_telegram(f"🔴 BOT DOWN — {detail}. It may have crashed or hung. "
                               f"Check the PC / restart if needed.")
                print(f"[ALERT] bot down: {detail}")
                healthy = False
            elif fresh and not healthy:
                _send_telegram("🟢 Bot RECOVERED — it's updating again. All good.")
                print("[ALERT] bot recovered")
                healthy = True
        except Exception as exc:
            print(f"watchdog loop error (continuing): {exc}")
        time.sleep(CHECK_INTERVAL_SEC)


if __name__ == "__main__":
    main()
