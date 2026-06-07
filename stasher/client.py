"""HTTP client for the PoE2 trade search/fetch endpoints.

Every request goes through the shared :class:`RateLimiter` and is logged to the
``query_log`` table for the UI. Credentials (account, POESESSID, league) are read
from the DB settings at call time, falling back to the Config seed, so the UI can
change them without restarting the worker.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote

import httpx

from .config import Config
from .ratelimit import RateLimiter
from .store import Store

FETCH_BATCH = 10  # API hard limit: max ids per /fetch call
MAX_ATTEMPTS = 6


class TradeAPIError(RuntimeError):
    pass


@dataclass
class Credentials:
    account: str
    poesessid: str
    league: str


def credentials(store: Store, config: Config) -> Credentials:
    return Credentials(
        account=store.get_setting("account_name", config.account_name) or "",
        poesessid=store.get_setting("poesessid", config.poesessid) or "",
        league=store.get_setting("league", config.league) or config.league,
    )


class TradeClient:
    def __init__(self, config: Config, store: Store, limiter: RateLimiter):
        self.config = config
        self.store = store
        self.limiter = limiter
        self._http = httpx.Client(
            timeout=config.request_timeout, follow_redirects=False
        )

    def close(self) -> None:
        self._http.close()

    # --- credentials / headers ------------------------------------------

    def creds(self) -> Credentials:
        return credentials(self.store, self.config)

    def _headers(self) -> dict[str, str]:
        creds = self.creds()
        headers = {
            "User-Agent": self.config.user_agent,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Origin": self.config.base_url,
            "Referer": f"{self.config.base_url}/trade2/search/{self.config.realm}",
        }
        if creds.poesessid:
            headers["Cookie"] = f"POESESSID={creds.poesessid}"
        return headers

    # --- endpoints ------------------------------------------------------

    def search(self, extra_filters: dict | None = None, target: str = "account") -> dict:
        """Run an account-scoped trade search. Returns {id, result, total}."""
        creds = self.creds()
        if not creds.account:
            raise TradeAPIError("No account_name configured")
        league = quote(creds.league, safe="")
        url = f"{self.config.base_url}/api/trade2/search/{self.config.realm}/{league}"
        body = _build_query(creds.account, extra_filters, self.config.status)
        data = self._request("search", "POST", url, target, json=body)
        return {
            "id": data.get("id"),
            "result": data.get("result", []) or [],
            "total": data.get("total", 0),
        }

    def fetch_batch(self, hashes: list[str], query_id: str) -> list[dict]:
        """Fetch details for up to FETCH_BATCH item hashes from a given query."""
        if not hashes:
            return []
        if len(hashes) > FETCH_BATCH:
            raise TradeAPIError(f"fetch_batch accepts at most {FETCH_BATCH} hashes")
        ids = ",".join(hashes)
        url = f"{self.config.base_url}/api/trade2/fetch/{ids}"
        params = {"query": query_id, "realm": self.config.realm}
        target = f"{len(hashes)} ids"
        data = self._request("fetch", "GET", url, target, params=params)
        return [r for r in (data.get("result") or []) if r]

    # --- request plumbing ----------------------------------------------

    def _request(self, policy: str, method: str, url: str, target: str, **kwargs) -> dict:
        last_exc: Exception | None = None
        for attempt in range(MAX_ATTEMPTS):
            self.limiter.before(policy)
            try:
                resp = self._http.request(
                    method, url, headers=self._headers(), **kwargs
                )
            except httpx.HTTPError as exc:
                last_exc = exc
                self.store.log_query(policy, target, "error", None, str(exc)[:200])
                continue

            self.limiter.after(policy, resp.status_code, resp.headers)

            if resp.status_code == 429:
                delay = self.limiter.note_429(policy, attempt, resp.headers)
                self.store.log_query(
                    policy, target, "rate_limited", 429, f"retry in ~{delay:.0f}s"
                )
                continue
            if resp.status_code >= 400:
                self.store.log_query(
                    policy, target, "error", resp.status_code, resp.text[:200]
                )
                raise TradeAPIError(f"{policy} {resp.status_code}: {resp.text[:200]}")

            self.store.log_query(policy, target, "ok", resp.status_code)
            try:
                return resp.json()
            except ValueError as exc:
                raise TradeAPIError(f"Bad JSON from {policy}: {exc}") from exc

        raise TradeAPIError(
            f"{policy} failed after {MAX_ATTEMPTS} attempts"
            + (f": {last_exc}" if last_exc else " (rate limited)")
        )


def _build_query(account: str, extra_filters: dict | None, status: str = "any") -> dict:
    """Assemble the trade search payload with the seller-account filter.

    ``status`` is the listing-type filter ("any" captures offline / non-buyout
    listings, unlike the site default of "securable" = Instant Buyout). The account
    name must include the #discriminator, e.g. "Name#1234".
    """
    filters: dict = {
        "trade_filters": {"filters": {"account": {"input": account}}},
    }
    if extra_filters:
        for group, value in extra_filters.items():
            if group == "trade_filters":
                filters["trade_filters"]["filters"].update(
                    value.get("filters", {})
                )
            else:
                filters[group] = value
    return {
        "query": {"status": {"option": status}, "filters": filters},
        "sort": {"price": "asc"},
    }
