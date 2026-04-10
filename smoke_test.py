#!/usr/bin/env python3
"""Smoke test for the TopstepX API client."""
import sys
import os
import json

sys.path.insert(0, os.path.dirname(__file__))

from src.topstepx_client import TopstepXClient, TopstepXConfig, TopstepXAPIError

USERNAME = os.getenv("TOPSTEPX_USERNAME", "")
API_KEY = os.getenv("TOPSTEPX_API_KEY", "")


def ok(label):
    print(f"  [PASS] {label}")


def fail(label, err):
    print(f"  [FAIL] {label}: {err}")


def section(title):
    print(f"\n=== {title} ===")


def run():
    config = TopstepXConfig(username=USERNAME, api_key=API_KEY)
    client = TopstepXClient(config)
    passed = 0
    failed = 0

    # 1. Authenticate
    section("Authentication")
    try:
        data = client.authenticate()
        token_preview = (client.token or "")[:20] + "..."
        ok(f"Authenticated — token: {token_preview}")
        passed += 1
    except TopstepXAPIError as e:
        fail("authenticate()", e)
        failed += 1
        print("\nCannot continue without a token. Exiting.")
        return passed, failed

    # 2. Validate session
    section("Session Validation")
    try:
        val = client.validate_session()
        ok(f"validate_session() => {json.dumps(val)[:120]}")
        passed += 1
    except TopstepXAPIError as e:
        fail("validate_session()", e)
        failed += 1

    # 3. Search accounts
    section("Accounts")
    accounts = []
    try:
        accounts = client.search_accounts()
        ok(f"search_accounts() => {len(accounts)} account(s)")
        for a in accounts:
            print(f"    id={a.get('id')}  name={a.get('name')}  balance={a.get('balance')}")
        passed += 1
    except TopstepXAPIError as e:
        fail("search_accounts()", e)
        failed += 1

    # 4. Search contracts (MNQ, sim)
    section("Contracts (MNQ, sim)")
    contract = None
    try:
        contracts = client.search_contracts("MNQ", live=False)
        ok(f"search_contracts('MNQ') => {len(contracts)} result(s)")
        for c in contracts[:5]:
            print(f"    id={c.get('id')}  name={c.get('name')}  symbol={c.get('symbol')}")
        contract = contracts[0] if contracts else None
        passed += 1
    except TopstepXAPIError as e:
        fail("search_contracts()", e)
        failed += 1

    # 5. Retrieve bars (if contract found)
    section("Historical Bars")
    if contract:
        from datetime import datetime, timedelta, timezone
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=5)
        try:
            bars = client.retrieve_bars(
                contract_id=contract["id"],
                start_time=start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                end_time=end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                live=False,
                unit=2,
                unit_number=5,
                limit=10,
            )
            ok(f"retrieve_bars() => {len(bars)} bar(s)")
            if bars:
                b = bars[-1]
                print(f"    last bar: t={b.get('t') or b.get('timestamp')}  o={b.get('o')}  h={b.get('h')}  l={b.get('l')}  c={b.get('c')}")
            passed += 1
        except TopstepXAPIError as e:
            fail("retrieve_bars()", e)
            failed += 1
    else:
        print("  [SKIP] No contract found — skipping bar retrieval")

    # 6. Open orders / positions (first account)
    if accounts:
        account_id = accounts[0]["id"]
        section(f"Open Orders & Positions (account {account_id})")
        try:
            orders = client.search_open_orders(account_id)
            ok(f"search_open_orders() => {len(orders)} order(s)")
            passed += 1
        except TopstepXAPIError as e:
            fail("search_open_orders()", e)
            failed += 1

        try:
            positions = client.search_open_positions(account_id)
            ok(f"search_open_positions() => {len(positions)} position(s)")
            passed += 1
        except TopstepXAPIError as e:
            fail("search_open_positions()", e)
            failed += 1

    return passed, failed


if __name__ == "__main__":
    if not USERNAME or not API_KEY:
        print("ERROR: TOPSTEPX_USERNAME and TOPSTEPX_API_KEY must be set.")
        sys.exit(1)

    print(f"TopstepX Smoke Test")
    print(f"Username : {USERNAME}")
    print(f"API Base : https://api.thefuturesdesk.projectx.com")

    passed, failed = run()

    print(f"\n{'='*40}")
    print(f"Results: {passed} passed, {failed} failed")
    print('='*40)
    sys.exit(0 if failed == 0 else 1)
