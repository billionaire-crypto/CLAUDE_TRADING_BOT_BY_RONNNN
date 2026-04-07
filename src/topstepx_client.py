import json
import os
import queue
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib import error, request

import websocket


DEFAULT_API_BASE_URL = os.getenv(
    "TOPSTEPX_API_BASE_URL",
    "https://api.thefuturesdesk.projectx.com",
)
DEFAULT_USER_HUB_URL = os.getenv(
    "TOPSTEPX_USER_HUB_URL",
    "wss://rtc.thefuturesdesk.projectx.com/hubs/user",
)


class TopstepXAPIError(RuntimeError):
    pass


@dataclass
class TopstepXConfig:
    username: str
    api_key: str
    api_base_url: str = DEFAULT_API_BASE_URL
    account_name: str = ""
    contract_search_text: str = "MNQ"
    topstep_account_size_usd: int = 50_000
    topstep_account_stage: str = "combine"
    live_data: bool = False
    dry_run: bool = True
    enable_order_routing: bool = False
    enable_user_hub: bool = True

    @classmethod
    def from_env(cls) -> "TopstepXConfig":
        return cls(
            username=os.getenv("TOPSTEPX_USERNAME", "").strip(),
            api_key=os.getenv("TOPSTEPX_API_KEY", "").strip(),
            api_base_url=os.getenv("TOPSTEPX_API_BASE_URL", DEFAULT_API_BASE_URL).rstrip("/"),
            account_name=os.getenv("TOPSTEPX_ACCOUNT_NAME", "").strip(),
            contract_search_text=os.getenv("TOPSTEPX_CONTRACT", "MNQ").strip(),
            topstep_account_size_usd=int(os.getenv("TOPSTEP_ACCOUNT_SIZE_USD", "50000").strip()),
            topstep_account_stage=os.getenv("TOPSTEP_ACCOUNT_STAGE", "combine").strip().lower(),
            live_data=os.getenv("TOPSTEPX_LIVE_DATA", "false").strip().lower() == "true",
            dry_run=os.getenv("TOPSTEPX_DRY_RUN", "true").strip().lower() != "false",
            enable_order_routing=os.getenv("TOPSTEPX_ENABLE_ORDER_ROUTING", "false").strip().lower() == "true",
            enable_user_hub=os.getenv("TOPSTEPX_ENABLE_USER_HUB", "true").strip().lower() != "false",
        )


class TopstepXClient:
    def __init__(self, config: TopstepXConfig):
        self.config = config
        self.token: Optional[str] = None

    def _post(self, path: str, payload: Dict[str, Any], auth_required: bool = True) -> Dict[str, Any]:
        url = f"{self.config.api_base_url}{path}"
        headers = {
            "accept": "text/plain",
            "Content-Type": "application/json",
        }
        if auth_required:
            if not self.token:
                raise TopstepXAPIError("Session token missing. Authenticate first.")
            headers["Authorization"] = f"Bearer {self.token}"

        req = request.Request(
            url=url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )

        try:
            with request.urlopen(req, timeout=20) as resp:
                body = resp.read().decode("utf-8")
        except error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise TopstepXAPIError(f"HTTP {exc.code} for {path}: {body}") from exc
        except error.URLError as exc:
            raise TopstepXAPIError(f"Network error for {path}: {exc}") from exc

        try:
            data = json.loads(body)
        except json.JSONDecodeError as exc:
            raise TopstepXAPIError(f"Non-JSON response for {path}: {body[:300]}") from exc

        if isinstance(data, dict) and data.get("success") is False:
            raise TopstepXAPIError(
                f"{path} failed: errorCode={data.get('errorCode')} errorMessage={data.get('errorMessage')}"
            )
        return data

    def authenticate(self) -> Dict[str, Any]:
        if not self.config.username or not self.config.api_key:
            raise TopstepXAPIError("TOPSTEPX_USERNAME or TOPSTEPX_API_KEY is missing.")
        data = self._post(
            "/api/Auth/loginKey",
            {"userName": self.config.username, "apiKey": self.config.api_key},
            auth_required=False,
        )
        token = data.get("token", "")
        if not token:
            raise TopstepXAPIError("Authentication succeeded but no token was returned.")
        self.token = token
        return data

    def validate_session(self) -> Dict[str, Any]:
        data = self._post("/api/Auth/validate", {})
        new_token = data.get("newToken")
        if new_token:
            self.token = new_token
        return data

    def search_accounts(self, only_active_accounts: bool = True) -> List[Dict[str, Any]]:
        data = self._post("/api/Account/search", {"onlyActiveAccounts": only_active_accounts})
        return data.get("accounts", []) or data.get("results", []) or []

    def search_contract_by_id(self, contract_id: str) -> Optional[Dict[str, Any]]:
        data = self._post("/api/Contract/searchById", {"contractId": contract_id})
        contract = data.get("contract")
        if isinstance(contract, dict):
            return contract
        contracts = data.get("contracts", []) or data.get("results", []) or []
        return contracts[0] if contracts else None

    def find_account(self, account_name: str) -> Optional[Dict[str, Any]]:
        for account in self.search_accounts(True):
            if str(account.get("name", "")).strip().lower() == account_name.strip().lower():
                return account
        return None

    def available_contracts(self, live: Optional[bool] = None) -> List[Dict[str, Any]]:
        live_flag = self.config.live_data if live is None else live
        data = self._post("/api/Contract/available", {"live": live_flag})
        return data.get("contracts", []) or data.get("results", []) or []

    def search_contracts(self, search_text: str, live: Optional[bool] = None) -> List[Dict[str, Any]]:
        live_flag = self.config.live_data if live is None else live
        data = self._post("/api/Contract/search", {"searchText": search_text, "live": live_flag})
        return data.get("contracts", []) or data.get("results", []) or []

    def resolve_contract(self, search_text: Optional[str] = None, live: Optional[bool] = None) -> Optional[Dict[str, Any]]:
        target = (search_text or self.config.contract_search_text).strip().upper()
        contracts = self.search_contracts(target, live=live)
        if not contracts:
            return None
        exact = [c for c in contracts if target in str(c.get("name", "")).upper() or target in str(c.get("symbol", "")).upper()]
        return exact[0] if exact else contracts[0]

    def search_open_orders(self, account_id: int) -> List[Dict[str, Any]]:
        data = self._post("/api/Order/searchOpen", {"accountId": account_id})
        return data.get("orders", []) or data.get("results", []) or []

    def search_open_positions(self, account_id: int) -> List[Dict[str, Any]]:
        data = self._post("/api/Position/searchOpen", {"accountId": account_id})
        return data.get("positions", []) or data.get("results", []) or []

    def search_orders(
        self,
        account_id: int,
        start_timestamp: str,
        end_timestamp: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        payload: Dict[str, Any] = {
            "accountId": account_id,
            "startTimestamp": start_timestamp,
        }
        if end_timestamp is not None:
            payload["endTimestamp"] = end_timestamp
        data = self._post("/api/Order/search", payload)
        return data.get("orders", []) or data.get("results", []) or []

    def search_trades(
        self,
        account_id: int,
        start_timestamp: str,
        end_timestamp: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        payload: Dict[str, Any] = {
            "accountId": account_id,
            "startTimestamp": start_timestamp,
        }
        if end_timestamp is not None:
            payload["endTimestamp"] = end_timestamp
        data = self._post("/api/Trade/search", payload)
        return data.get("trades", []) or data.get("results", []) or []

    def retrieve_bars(
        self,
        contract_id: str,
        start_time: str,
        end_time: str,
        *,
        live: Optional[bool] = None,
        unit: int = 2,
        unit_number: int = 5,
        limit: int = 2500,
        include_partial_bar: bool = False,
    ) -> List[Dict[str, Any]]:
        payload: Dict[str, Any] = {
            "contractId": contract_id,
            "live": self.config.live_data if live is None else live,
            "startTime": start_time,
            "endTime": end_time,
            "unit": unit,
            "unitNumber": unit_number,
            "limit": limit,
            "includePartialBar": include_partial_bar,
        }
        data = self._post("/api/History/retrieveBars", payload)
        return data.get("bars", []) or data.get("results", []) or []

    def place_order(
        self,
        account_id: int,
        contract_id: str,
        side: int,
        size: int,
        order_type: int = 2,
        limit_price: Optional[float] = None,
        stop_price: Optional[float] = None,
        custom_tag: Optional[str] = None,
        stop_loss_bracket: Optional[Dict[str, Any]] = None,
        take_profit_bracket: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "accountId": account_id,
            "contractId": contract_id,
            "type": order_type,
            "side": side,
            "size": size,
        }
        if limit_price is not None:
            payload["limitPrice"] = limit_price
        if stop_price is not None:
            payload["stopPrice"] = stop_price
        if custom_tag is not None:
            payload["customTag"] = custom_tag
        if stop_loss_bracket is not None:
            payload["stopLossBracket"] = stop_loss_bracket
        if take_profit_bracket is not None:
            payload["takeProfitBracket"] = take_profit_bracket
        return self._post("/api/Order/place", payload)

    def cancel_order(self, account_id: int, order_id: int) -> Dict[str, Any]:
        return self._post("/api/Order/cancel", {"accountId": account_id, "orderId": order_id})

    def close_contract(self, account_id: int, contract_id: str) -> Dict[str, Any]:
        return self._post("/api/Position/closeContract", {"accountId": account_id, "contractId": contract_id})


class TopstepXUserHubStream:
    _RECORD_SEPARATOR = "\x1e"

    def __init__(
        self,
        token: str,
        account_id: int,
        user_hub_url: str = DEFAULT_USER_HUB_URL,
    ):
        self.token = token
        self.account_id = int(account_id)
        self.user_hub_url = user_hub_url
        self._queue: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._connected = threading.Event()
        self._last_message_at: Optional[str] = None
        self._last_error: str = ""

    @property
    def last_message_at(self) -> Optional[str]:
        return self._last_message_at

    @property
    def last_error(self) -> str:
        return self._last_error

    @property
    def is_connected(self) -> bool:
        return self._connected.is_set()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="TopstepXUserHubStream", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        self._connected.clear()

    def drain_events(self) -> List[Dict[str, Any]]:
        events: List[Dict[str, Any]] = []
        while True:
            try:
                events.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return events

    def get_event(self, timeout: float = 0.0) -> Optional[Dict[str, Any]]:
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def _queue_event(self, event_type: str, payload: Optional[Dict[str, Any]] = None) -> None:
        self._queue.put(
            {
                "event_type": event_type,
                "payload": payload or {},
                "logged_at": datetime.utcnow().isoformat() + "Z",
            }
        )

    def _send(self, ws, payload: Dict[str, Any]) -> None:
        ws.send(json.dumps(payload) + self._RECORD_SEPARATOR)

    def _subscribe(self, ws) -> None:
        for target, arguments in (
            ("SubscribeAccounts", []),
            ("SubscribeOrders", [self.account_id]),
            ("SubscribePositions", [self.account_id]),
            ("SubscribeTrades", [self.account_id]),
        ):
            self._send(ws, {"type": 1, "target": target, "arguments": arguments})

    def _handle_frame(self, frame: str) -> None:
        if not frame:
            return
        parts = [part for part in frame.split(self._RECORD_SEPARATOR) if part.strip()]
        for part in parts:
            message = json.loads(part)
            self._last_message_at = datetime.utcnow().isoformat() + "Z"
            message_type = message.get("type")
            if message_type == 6:
                self._queue_event("user_hub_ping")
                continue
            if message_type == 7:
                raise TopstepXAPIError(f"User hub closed connection: {message}")
            if message_type != 1:
                continue
            target = str(message.get("target", ""))
            arguments = message.get("arguments", []) or []
            payload = arguments[0] if arguments else {}
            if target in {"GatewayUserAccount", "GatewayUserOrder", "GatewayUserPosition", "GatewayUserTrade"}:
                self._queue_event(target, payload)

    def _run(self) -> None:
        backoff = 5
        while not self._stop_event.is_set():
            ws = None
            try:
                ws = websocket.create_connection(
                    f"{self.user_hub_url}?access_token={self.token}",
                    timeout=20,
                    enable_multithread=True,
                )
                ws.send('{"protocol":"json","version":1}' + self._RECORD_SEPARATOR)
                handshake = ws.recv()
                self._handle_frame(handshake)
                self._subscribe(ws)
                self._connected.set()
                self._queue_event("user_hub_connected", {"accountId": self.account_id})
                backoff = 5

                while not self._stop_event.is_set():
                    frame = ws.recv()
                    if frame is None:
                        raise TopstepXAPIError("User hub websocket returned no data.")
                    self._handle_frame(frame)
            except Exception as exc:  # pragma: no cover - operational stream guard
                self._last_error = str(exc)
                self._connected.clear()
                self._queue_event("user_hub_error", {"message": str(exc)})
                if self._stop_event.wait(backoff):
                    break
                backoff = min(backoff * 2, 60)
            finally:
                self._connected.clear()
                if ws is not None:
                    try:
                        ws.close()
                    except Exception:
                        pass
