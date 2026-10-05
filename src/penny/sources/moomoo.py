"""Moomoo OpenAPI adapter: signed REST calls, snapshot, movers, candles, short data.

Signing follows the documented scheme:
    Authorization = Base64(sign("{ts_ms}\\n{METHOD}\\n{path}\\n{query}\\n{sha256_hex(body)}"))
Confirmed against open.moomoo.com/api during build (see docs/FINDINGS.md, C1/C2).
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import time
from pathlib import Path
from typing import Any, Iterable, Optional

from ..util import epoch_ms, from_epoch_ms, now_et, scrub, session_for
from .base import BaseClient, RateLimiter, SourceError, fnum

log = logging.getLogger(__name__)

SNAPSHOT_PATH = "/api/v1.0/quote/snapshot"
PLATE_STOCK_PATH = "/api/v1.0/quote/plate-stock"
PLATE_LIST_PATH = "/api/v1.0/quote/plate-list"
SCREEN_PATH = "/api/v1.0/quote/stock-screen"
TRADING_DAYS_PATH = "/api/v1.0/quote/trading-days"
SERVER_TIME_PATH = "/api/v1.0/server-time"
KLINE_PATH = "/api/v1.0/quote/history/kline"
SHORT_INTEREST_PATH = "/api/v1.0/quote/short-interest"
SHORT_VOLUME_PATH = "/api/v1.0/quote/short-volume"

CLOCK_ERROR_CODE = -12006


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _nonce(n: int = 24) -> str:
    alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
    return "".join(secrets.choice(alphabet) for _ in range(n))


class MoomooClient(BaseClient):
    name = "moomoo"

    def __init__(self, cfg, secrets: Optional[Iterable[str]] = None):
        super().__init__(cfg, secrets)
        self.base = str(cfg.raw("MOOMOO_BASE_URL") or "https://webapi.moomoo.com").rstrip("/")
        self.api_key = cfg.raw("MOOMOO_API_KEY")
        self.algo = (cfg.raw("MOOMOO_ALGO") or "ED25519").upper()
        self.key_path = cfg.raw("MOOMOO_PRIVATE_KEY_PATH")
        self._private_key = None
        self._clock_offset_ms = 0
        # C4 default: at most one snapshot call per second.
        self.limiter = RateLimiter(rate=1, per_seconds=1.0)
        self._snapshot_cache: tuple[float, list[dict]] = (0.0, [])

    # -- keys ---------------------------------------------------------------
    def _load_key(self):
        if self._private_key is not None:
            return self._private_key
        if not self.key_path:
            raise SourceError("moomoo: MOOMOO_PRIVATE_KEY_PATH is not set")
        path = Path(self.key_path)
        if not path.exists():
            raise SourceError(f"moomoo: private key file not found: {path}")
        from cryptography.hazmat.primitives import serialization
        data = path.read_bytes()
        self._private_key = serialization.load_pem_private_key(data, password=None)
        return self._private_key

    def _sign(self, method: str, path: str, query: str, body: bytes) -> str:
        body_hash = hashlib.sha256(body).hexdigest()
        message = f"{self._timestamp_ms()}\n{method.upper()}\n{path}\n{query}\n{body_hash}"
        payload = message.encode("utf-8")
        key = self._load_key()
        if self.algo.startswith("ED"):
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
            if not isinstance(key, Ed25519PrivateKey):
                raise SourceError("moomoo: private key is not Ed25519 but MOOMOO_ALGO says ED25519")
            return _b64(key.sign(payload))
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        return _b64(key.sign(payload, padding.PKCS1v15(), hashes.SHA256()))

    def _timestamp_ms(self) -> int:
        return epoch_ms() + self._clock_offset_ms

    def sync_clock(self) -> int:
        """Read server time and store the offset; Moomoo rejects >5 s drift."""
        try:
            resp = self.request("GET", f"{self.base}{SERVER_TIME_PATH}", timeout=10.0,
                                retries=1)
            data = resp.json()
            server_ms = self._extract_server_time(data)
            if server_ms:
                local_ms = epoch_ms()
                self._clock_offset_ms = server_ms - local_ms
                log.info("moomoo clock offset: %d ms", self._clock_offset_ms)
                return self._clock_offset_ms
        except Exception as exc:  # noqa: BLE001
            log.warning("moomoo clock sync failed: %s", self.error_text(exc))
        return 0

    @staticmethod
    def _extract_server_time(data: Any) -> Optional[int]:
        if not isinstance(data, dict):
            return None
        for key in ("data", "server_time", "timestamp", "ts"):
            v = data.get(key)
            if isinstance(v, (int, float)) and v > 1_000_000_000_000:
                return int(v)
            if isinstance(v, dict):
                inner = MoomooClient._extract_server_time(v)
                if inner:
                    return inner
            if isinstance(v, str) and v.isdigit() and len(v) == 13:
                return int(v)
        return None

    # -- signed request -----------------------------------------------------
    def _call(self, method: str, path: str, *, query: Optional[dict] = None,
              body: Optional[Any] = None, timeout: float = 20.0, retries: int = 2) -> Any:
        if not self.api_key:
            raise SourceError("moomoo: MOOMOO_API_KEY is not set")
        query = query or {}
        query_string = "&".join(f"{k}={v}" for k, v in query.items() if v is not None)
        body_bytes = b""
        if body is not None:
            body_bytes = json.dumps(body, separators=(",", ":")).encode("utf-8")

        headers = {
            "X-Api-Key": self.api_key,
            "X-Timestamp": str(self._timestamp_ms()),
            "X-Nonce": _nonce(),
            "Content-Type": "application/json",
        }
        headers["Authorization"] = self._sign(method, path, query_string, body_bytes)

        url = f"{self.base}{path}"
        if query_string:
            url = f"{url}?{query_string}"

        last_exc: Optional[Exception] = None
        for attempt in range(retries + 1):
            headers["X-Timestamp"] = str(self._timestamp_ms())
            headers["X-Nonce"] = _nonce()
            headers["Authorization"] = self._sign(method, path, query_string, body_bytes)
            try:
                if method.upper() == "GET":
                    resp = self.request("GET", url, headers=headers, timeout=timeout,
                                        retries=0)
                else:
                    resp = self.request("POST", url, headers=headers, data=body_bytes,
                                        timeout=timeout, retries=0)
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if attempt >= retries:
                    raise SourceError(f"moomoo: {self.error_text(exc)}") from exc
                time.sleep(1.0 * (2 ** attempt))
                continue

            if resp.status_code == 429:
                wait = float(resp.headers.get("Retry-After") or 2 ** attempt)
                if attempt >= retries:
                    raise SourceError("moomoo: rate limited (429)")
                time.sleep(wait)
                continue
            if resp.status_code >= 400:
                if attempt >= retries:
                    raise SourceError(f"moomoo: HTTP {resp.status_code}")
                time.sleep(1.0 * (2 ** attempt))
                continue

            try:
                data = resp.json()
            except ValueError as exc:
                raise SourceError("moomoo: invalid JSON") from exc

            code = data.get("ret_code", data.get("code", 0))
            if code == CLOCK_ERROR_CODE:
                log.warning("moomoo: clock drift detected, resyncing")
                self.sync_clock()
                if attempt >= retries:
                    raise SourceError("moomoo: clock drift (-12006)")
                continue
            if code not in (0, None):
                msg = data.get("ret_msg") or data.get("message") or ""
                raise SourceError(f"moomoo: ret_code {code} {scrub(str(msg), self.secrets)}")
            return data.get("data", data)
        raise SourceError(f"moomoo: {last_exc}")

    # -- snapshot -----------------------------------------------------------
    @staticmethod
    def code(symbol: str) -> str:
        s = symbol.upper().strip()
        if s.startswith("US."):
            return s
        return f"US.{s}"

    @staticmethod
    def ticker(code: str) -> str:
        return code.split(".")[-1].upper()

    def snapshot_raw(self, symbols: Iterable[str], batch: int = 400) -> list[dict]:
        out: list[dict] = []
        syms = list(dict.fromkeys(self.code(s) for s in symbols if s))
        for i in range(0, len(syms), batch):
            chunk = syms[i:i + batch]
            self.limiter.acquire()
            data = self._call("POST", SNAPSHOT_PATH, body={"code_list": chunk})
            items = data if isinstance(data, list) else (data.get("data") or data.get("list") or [])
            out.extend(items or [])
        return out

    def snapshot(self, symbols: Iterable[str], session: Optional[str] = None) -> list[dict]:
        """Snapshot normalized into the system's quote shape (6.4)."""
        session = session or session_for()
        raw = self.snapshot_raw(symbols)
        return [self.normalize(item, session) for item in raw]

    def normalize(self, item: dict, session: str) -> dict:
        if not isinstance(item, dict):
            return {}
        code = item.get("code") or item.get("symbol") or ""
        symbol = self.ticker(str(code))

        if session == "pre":
            price = fnum(item.get("pre_price"))
        elif session == "post":
            price = fnum(item.get("after_price"))
        else:
            price = fnum(item.get("last_price"))
        if not price or price <= 0:
            price = fnum(item.get("last_price"))

        cum = 0.0
        for key in ("pre_volume", "volume", "after_volume"):
            v = fnum(item.get(key))
            if v:
                cum += v

        update_time = item.get("update_time")
        ts = time.time()
        if isinstance(update_time, (int, float)) and update_time > 1_000_000_000_000:
            ts = from_epoch_ms(int(update_time)).timestamp()

        suspension = item.get("suspension")
        sec_status = str(item.get("sec_status") or "").lower()
        halted = bool(suspension) or sec_status in {"suspended", "halted"}

        return {
            "symbol": symbol,
            "code": str(code),
            "ts": ts,
            "price": price,
            "prev_close": fnum(item.get("prev_close_price")),
            "cum_volume": cum,
            "open_price": fnum(item.get("open_price")),
            "high_price": fnum(item.get("high_price")),
            "low_price": fnum(item.get("low_price")),
            "bid": fnum(item.get("bid_price")),
            "ask": fnum(item.get("ask_price")),
            "bid_vol": fnum(item.get("bid_vol")),
            "ask_vol": fnum(item.get("ask_vol")),
            "market_cap": fnum(item.get("total_market_val")),
            "float_shares": fnum(item.get("outstanding_shares")),
            "issued_shares": fnum(item.get("issued_shares")),
            "turnover": fnum(item.get("turnover")),
            "volume_ratio": fnum(item.get("volume_ratio")),
            "pre_price": fnum(item.get("pre_price")),
            "pre_volume": fnum(item.get("pre_volume")),
            "pre_change_rate": fnum(item.get("pre_change_rate")),
            "after_price": fnum(item.get("after_price")),
            "after_volume": fnum(item.get("after_volume")),
            "after_change_rate": fnum(item.get("after_change_rate")),
            "halted": halted,
            "session": session,
            "source": "moomoo",
        }

    # -- movers -------------------------------------------------------------
    SORT_FIELD_BY_SESSION = {
        "pre": "PRE_CHANGE_RATE",
        "regular": "CHANGE_RATE",
        "post": "AFTER_CHANGE_RATE",
    }
    PRICE_TYPE_BY_SESSION = {
        "pre": "BEFORE",
        "regular": "NORMAL",
        "post": "AFTER",
    }

    def plate_list(self, market: str = "US", plate_class: str = "ALL") -> list[dict]:
        data = self._call("GET", PLATE_LIST_PATH,
                          query={"market": market, "plate_class": plate_class})
        if isinstance(data, list):
            return data
        return data.get("list") or data.get("plate_list") or []

    def plate_stock(self, plate_code: str = "", sort_field: str = "CHANGE_RATE",
                    ascend: int = 0, price_type: str = "NORMAL", limit: int = 200,
                    next_key: str = "") -> dict:
        query = {
            "plate_code": plate_code,
            "sort_field": sort_field,
            "ascend": ascend,
            "price_type": price_type,
            "limit": limit,
            "next_key": next_key or None,
        }
        data = self._call("GET", PLATE_STOCK_PATH, query=query)
        if isinstance(data, list):
            return {"list": data}
        return data or {}

    def movers(self, session: Optional[str] = None, top_n: int = 200,
               plate: str = "") -> list[str]:
        """L1: reproduce Moomoo Market Movers - Top Gainers for the session (6.3)."""
        session = session or session_for()
        if session == "closed":
            return []
        sort_field = self.SORT_FIELD_BY_SESSION.get(session, "CHANGE_RATE")
        price_type = self.PRICE_TYPE_BY_SESSION.get(session, "NORMAL")
        found: list[str] = []
        seen: set[str] = set()

        plans = [
            (sort_field, top_n),
            ({"pre": "PRE_VOLUME", "regular": "VOLUME", "post": "AFTER_VOLUME"}.get(session, "VOLUME"), top_n),
            ("VOLUME_RATIO", top_n),
        ]
        for field, limit in plans:
            try:
                data = self.plate_stock(plate_code=plate, sort_field=field, ascend=0,
                                        price_type=price_type, limit=min(limit, 1000))
            except SourceError as exc:
                log.warning("moomoo movers (%s) failed: %s", field, self.error_text(exc))
                continue
            rows = data.get("list") or data.get("data") or []
            for row in rows:
                code = row.get("code") or row.get("symbol") or ""
                sym = self.ticker(str(code))
                if sym and sym not in seen:
                    seen.add(sym)
                    found.append(sym)
                if len(found) >= top_n * 3:
                    break
        return found

    # -- screener -----------------------------------------------------------
    def stock_screen(self, screen_queries: list[dict], retrieve: list[str] | None = None,
                     sort: Optional[dict] = None, limit: int = 300,
                     next_key: str = "") -> dict:
        body: dict[str, Any] = {
            "screen_queries": screen_queries,
            "retrieve_queries": retrieve or [],
            "limit": limit,
        }
        if sort:
            body["sort"] = sort
        if next_key:
            body["next_key"] = next_key
        data = self._call("POST", SCREEN_PATH, body=body)
        return data if isinstance(data, dict) else {"list": data}

    # -- calendar -----------------------------------------------------------
    def trading_days(self, start: str = "", end: str = "", market: str = "US") -> list[str]:
        data = self._call("GET", TRADING_DAYS_PATH,
                          query={"market": market, "start": start, "end": end})
        if isinstance(data, list):
            return [str(x) for x in data]
        return [str(x) for x in (data.get("trading_days") or data.get("list") or [])]

    def market_state(self, market: str = "US") -> str:
        try:
            data = self._call("GET", "/api/v1.0/quote/market-state", query={"market": market},
                              retries=0)
            if isinstance(data, dict):
                return str(data.get("market_state") or data.get("state") or "")
        except SourceError:
            pass
        return ""

    # -- candles ------------------------------------------------------------
    def kline(self, symbol: str, start_ms: int, end_ms: int, ktype: str = "K_1M",
              extended_time: int = 1, max_count: int = 1000) -> list[dict]:
        """History K-Line. extended_time: 0 regular, 1 pre+post, 2 overnight (C7)."""
        data = self._call("GET", KLINE_PATH, query={
            "code": self.code(symbol),
            "start_time": start_ms,
            "end_time": end_ms,
            "ktype": ktype,
            "extended_time": extended_time,
            "max_count": max_count,
            "autype": "NONE",
        })
        rows = data.get("list") or data.get("klines") or (data if isinstance(data, list) else [])
        out = []
        for r in rows or []:
            try:
                ts = int(r.get("time_key") or r.get("timestamp") or r.get("time") or 0)
                if ts > 10_000_000_000:
                    ts = ts // 1000
                out.append({
                    "time": ts,
                    "open": fnum(r.get("open") or r.get("open_price")),
                    "high": fnum(r.get("high") or r.get("high_price")),
                    "low": fnum(r.get("low") or r.get("low_price")),
                    "close": fnum(r.get("close") or r.get("close_price")),
                    "volume": fnum(r.get("volume")) or 0.0,
                })
            except (TypeError, ValueError):
                continue
        out.sort(key=lambda x: x["time"])
        return out

    # -- short data ---------------------------------------------------------
    def short_interest(self, symbol: str) -> Optional[dict]:
        try:
            data = self._call("GET", SHORT_INTEREST_PATH, query={"code": self.code(symbol)},
                              retries=0)
        except SourceError as exc:
            log.debug("moomoo short interest failed for %s: %s", symbol, self.error_text(exc))
            return None
        if isinstance(data, list):
            data = data[0] if data else None
        if not isinstance(data, dict):
            return None
        return {
            "shares_short": fnum(data.get("short_interest") or data.get("shares_short")),
            "date": data.get("date") or data.get("timestamp"),
            "ratio": fnum(data.get("short_ratio") or data.get("ratio")),
            "source": "moomoo",
        }

    def short_volume(self, symbol: str) -> Optional[dict]:
        try:
            data = self._call("GET", SHORT_VOLUME_PATH, query={"code": self.code(symbol)},
                              retries=0)
        except SourceError as exc:
            log.debug("moomoo short volume failed for %s: %s", symbol, self.error_text(exc))
            return None
        if isinstance(data, list):
            data = data[0] if data else None
        if not isinstance(data, dict):
            return None
        return {
            "short_volume": fnum(data.get("short_volume")),
            "total_volume": fnum(data.get("total_volume")),
            "date": data.get("date") or data.get("timestamp"),
            "source": "moomoo",
        }

    # -- health -------------------------------------------------------------
    def health(self) -> tuple[bool, str]:
        try:
            self.sync_clock()
            quotes = self.snapshot(["SPY"])
            if quotes and quotes[0].get("price"):
                return True, f"snapshot ok (SPY {quotes[0]['price']})"
            return False, "snapshot returned no usable price"
        except Exception as exc:  # noqa: BLE001
            return False, self.error_text(exc)


def generate_keypair(algo: str = "ED25519") -> tuple[str, str]:
    """Create a key pair for the Moomoo dashboard. Returns (private_pem, public_pem)."""
    from cryptography.hazmat.primitives import serialization
    algo = algo.upper()
    if algo.startswith("ED"):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        key = Ed25519PrivateKey.generate()
    else:
        from cryptography.hazmat.primitives.asymmetric import rsa
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    return private_pem, public_pem


def write_private_key(path: str | os.PathLike, pem: str) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(pem, encoding="utf-8")
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
