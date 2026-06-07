"""Live capture via the trade live-search websocket.

After registering a search (which also gives us the items currently listed, for gap
fill), we connect to ``wss://.../api/trade2/live/{realm}/{league}/{queryId}``. The
server pushes ``{"new": [hash, ...]}`` as items get listed; we hand those to the fetch
pipeline. The websocket library handles ping keepalive; we reconnect with backoff and
re-register on drop, and check ``stop_event`` so the worker can shut us down.
"""

from __future__ import annotations

import asyncio
import json
import threading
from typing import Callable
from urllib.parse import quote

import websockets

from .client import TradeAPIError, TradeClient
from .pipeline import Pipeline

StatusFn = Callable[..., None]
RECV_POLL = 1.0
MAX_BACKOFF = 60.0


def run_live(
    client: TradeClient,
    pipeline: Pipeline,
    stop_event: threading.Event,
    status: StatusFn | None = None,
) -> None:
    """Blocking. Run until ``stop_event`` is set. Call from a dedicated thread."""
    asyncio.run(_run(client, pipeline, stop_event, status or (lambda **_: None)))


async def _run(
    client: TradeClient,
    pipeline: Pipeline,
    stop_event: threading.Event,
    status: StatusFn,
) -> None:
    loop = asyncio.get_running_loop()
    backoff = 1.0
    while not stop_event.is_set():
        try:
            status(mode="reconnecting", live_connected=False)
            search = await loop.run_in_executor(None, client.search)
            query_id = search["id"]
            if not query_id:
                raise TradeAPIError("live search returned no query id")
            # Catch-up: archive whatever is currently listed before streaming.
            await loop.run_in_executor(
                None, pipeline.submit_hashes, search["result"], query_id
            )

            uri = _ws_uri(client, query_id)
            async with _connect(uri, client) as ws:
                backoff = 1.0
                status(mode="live", live_connected=True)
                await _consume(ws, pipeline, query_id, loop, stop_event, status)
        except Exception as exc:  # noqa: BLE001 - resilience: log and retry
            client.store.log_query("live", "connect", "error", None, str(exc)[:160])
            status(mode="reconnecting", live_connected=False, last_error=str(exc)[:160])
            if stop_event.is_set():
                break
            await asyncio.sleep(min(backoff, MAX_BACKOFF))
            backoff = min(backoff * 2, MAX_BACKOFF)
    status(mode="idle", live_connected=False)


async def _consume(ws, pipeline, query_id, loop, stop_event, status) -> None:
    while not stop_event.is_set():
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=RECV_POLL)
        except asyncio.TimeoutError:
            continue
        try:
            msg = json.loads(raw)
        except (TypeError, ValueError):
            continue
        ids = msg.get("new") if isinstance(msg, dict) else None
        if ids:
            stored = await loop.run_in_executor(
                None, pipeline.submit_hashes, ids, query_id
            )
            status(mode="live", live_connected=True, last_push=len(ids), last_new=stored)


def _ws_uri(client: TradeClient, query_id: str) -> str:
    base = client.config.base_url.replace("https://", "wss://").replace("http://", "ws://")
    creds = client.creds()
    league = quote(creds.league, safe="")
    return f"{base}/api/trade2/live/{client.config.realm}/{league}/{query_id}"


def _connect(uri: str, client: TradeClient):
    creds = client.creds()
    headers = {
        "Origin": client.config.base_url,
        "User-Agent": client.config.user_agent,
    }
    if creds.poesessid:
        headers["Cookie"] = f"POESESSID={creds.poesessid}"
    # websockets renamed extra_headers -> additional_headers in v14; support both.
    try:
        return websockets.connect(
            uri, additional_headers=headers, ping_interval=30, ping_timeout=15
        )
    except TypeError:
        return websockets.connect(
            uri, extra_headers=headers, ping_interval=30, ping_timeout=15
        )
