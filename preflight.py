"""
Pre-open smoke test. Run before a trading session to confirm the bot CAN trade:
credentials valid, account reachable, MNQ contract resolves, Telegram alerts land.

Read-only: authenticates and searches, but places NO orders. Never prints secrets.
Run:  python preflight.py
Exit code 0 = all green, 1 = at least one check failed.
"""
import os
import sys
from urllib import parse, request

try:
    sys.stdout.reconfigure(encoding="utf-8")  # Windows console defaults to cp1252
except Exception:
    pass

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
except Exception:
    pass

# Lightweight import — the API client does NOT pull in the heavy strategy module.
from src.topstepx_client import TopstepXConfig, TopstepXClient, TopstepXAPIError

results = []  # (ok: bool, label: str, detail: str)


def check(label):
    """Decorator-ish helper: run fn, record pass/fail, never raise out."""
    def run(fn):
        try:
            detail = fn()
            results.append((True, label, detail or "ok"))
        except Exception as exc:
            results.append((False, label, str(exc)[:200]))
    return run


def _send_telegram(text):
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat:
        raise RuntimeError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing in .env")
    data = parse.urlencode({"chat_id": chat, "text": text}).encode("utf-8")
    req = request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data, method="POST")
    with request.urlopen(req, timeout=15) as resp:
        if resp.status != 200:
            raise RuntimeError(f"Telegram HTTP {resp.status}")


def main():
    print("=== MNQ Bot pre-open smoke test ===\n")
    config = TopstepXConfig.from_env()
    client = TopstepXClient(config)

    # 1) Required env vars present (report presence only, never values)
    @check("Credentials present in .env")
    def _():
        missing = [k for k in ("TOPSTEPX_USERNAME", "TOPSTEPX_API_KEY", "TOPSTEPX_ACCOUNT_NAME")
                   if not os.getenv(k, "").strip()]
        if missing:
            raise RuntimeError(f"missing: {', '.join(missing)}")
        return "username, api_key, account_name all set"

    # 2) Authenticate (the credential that expires)
    @check("API authentication")
    def _():
        client.authenticate()
        client.validate_session()
        return "token obtained + session validated"

    # 3) Account reachable and matches configured name
    @check("Account reachable")
    def _():
        accounts = client.search_accounts(True)
        match = client.find_account(config.account_name)
        if not accounts:
            raise RuntimeError("no active accounts returned")
        if config.account_name and not match:
            names = ", ".join(str(a.get("name", "?")) for a in accounts)
            raise RuntimeError(f"'{config.account_name}' not found among: {names}")
        return f"{len(accounts)} active account(s); configured account found"

    # 4) MNQ contract resolves with the SAME config the bot uses
    @check("MNQ contract resolves")
    def _():
        contract = client.resolve_contract(live=config.live_data)
        if not contract:
            raise RuntimeError(f"'{config.contract_search_text}' returned empty "
                               f"(live_data={config.live_data}) — the pre-market failure")
        return f"{contract.get('name', contract.get('id'))} (live_data={config.live_data})"

    # 5) Telegram alerts actually land on the phone
    @check("Telegram alert delivery")
    def _():
        _send_telegram("✅ Pre-open smoke test — your bot's alerts are working. (This is a TEST.)")
        return "test message sent"

    # ---- report ----
    print(f"{'':2}{'CHECK':<30}{'RESULT'}")
    all_ok = True
    for ok, label, detail in results:
        mark = "[PASS]" if ok else "[FAIL]"
        all_ok = all_ok and ok
        print(f"{mark} {label:<30}{detail}")
    print()
    if all_ok:
        print("ALL GREEN — the bot can trade this session.")
        return 0
    print("NOT READY — fix the ❌ items above before the open.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
