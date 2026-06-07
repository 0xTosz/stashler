"""Live capture via the trade live-search websocket.

NOTE (PoE2): this path is NOT used for capture and is not exposed in the UI. On PoE2 the
server no longer pushes plain ``{"new": [hash, ...]}`` frames — new-item notifications
arrive as an *encrypted* JWT payload (``{"result": "<jwt>"}`` whose ``d`` field is an
opaque, ~7.5 bits/byte blob with a fixed ``de f5 02 00`` header; the official web client
decrypts it client-side with a key we don't have). So the socket connects and
authenticates, but a third-party tool can't read the item ids from it. Near-live capture
is done with the light poll + periodic backfill instead (see ``backfill.run_light_poll``
and ``runtime.Worker`` auto-loop). This module is kept for reference, the CLI ``live``
command, and in case the protocol ever returns to a readable format.

Original behaviour (still true on PoE1): after registering a search we connect to
``wss://.../api/trade2/live/{realm}/{league}/{queryId}``; the server pushes
``{"new": [hash, ...]}`` as items get listed; we hand those to the fetch pipeline,
reconnect with backoff, and check ``stop_event`` so the worker can shut us down.
"""

from __future__ import annotations

import asyncio
import json
import threading
from typing import Callable
from urllib.parse import quote

import websockets
from websockets.exceptions import ConnectionClosed

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
        connected_at: float | None = None
        try:
            status(mode="reconnecting", live_connected=False)
            search = await loop.run_in_executor(None, client.search)
            query_id = search["id"]
            if not query_id:
                raise TradeAPIError("live search returned no query id")

            listed = len(search["result"])
            client.store.log_query(
                "live", f"qid={query_id} · {listed} listed now", "ok"
            )
            uri = _ws_uri(client, query_id)
            async with _connect(uri, client) as ws:
                connected_at = loop.time()
                backoff = 1.0
                status(mode="live", live_connected=True)
                # Catch-up *after* the socket is open: a slow, rate-limited backfill of
                # currently-listed items must not let the query id expire before we
                # connect (a stale id is closed by the server with 1008).
                stored = await loop.run_in_executor(
                    None, pipeline.submit_hashes, search["result"], query_id
                )
                client.store.log_query(
                    "live", f"catch-up stored {stored} new · listening", "ok"
                )
                await _consume(ws, pipeline, query_id, loop, stop_event, status, client)
        except Exception as exc:  # noqa: BLE001 - resilience: log and retry
            detail = _error_detail(exc)
            if connected_at is not None:
                detail = f"{detail} (after {loop.time() - connected_at:.0f}s connected)"
            client.store.log_query("live", "connect", "error", None, detail[:200])
            status(mode="reconnecting", live_connected=False, last_error=detail[:240])
            if stop_event.is_set():
                break
            await asyncio.sleep(min(backoff, MAX_BACKOFF))
            backoff = min(backoff * 2, MAX_BACKOFF)
    status(mode="idle", live_connected=False)


def probe_live(client: TradeClient, timeout: float = 4.0) -> dict:
    """Open the live socket once, report whether it was accepted, then disconnect.

    Returns ``{ok, stage, close_code, detail, query_id}`` — a diagnostic that exercises
    the same search + handshake as real capture without starting a continuous stream.
    """
    try:
        return asyncio.run(_probe(client, timeout))
    except Exception as exc:  # noqa: BLE001 - report any failure to the UI
        return {"ok": False, "stage": "open", "detail": _error_detail(exc)}


async def _probe(client: TradeClient, timeout: float) -> dict:
    loop = asyncio.get_running_loop()
    try:
        search = await loop.run_in_executor(None, client.search)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "stage": "search", "detail": _error_detail(exc)}
    query_id = search.get("id")
    if not query_id:
        return {"ok": False, "stage": "search", "detail": "search returned no query id"}

    uri = _ws_uri(client, query_id)
    try:
        async with _connect(uri, client) as ws:
            try:
                await asyncio.wait_for(ws.recv(), timeout=timeout)
            except asyncio.TimeoutError:
                pass  # open and idle for the whole window = healthy
            except ConnectionClosed as exc:
                return _closed_result(exc, "stream", query_id)
            return {
                "ok": True,
                "stage": "stream",
                "query_id": query_id,
                "detail": "connected — live socket accepted the session",
            }
    except ConnectionClosed as exc:
        return _closed_result(exc, "open", query_id)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "stage": "open", "query_id": query_id, "detail": _error_detail(exc)}


def _closed_result(exc: ConnectionClosed, stage: str, query_id: str) -> dict:
    return {
        "ok": False,
        "stage": stage,
        "close_code": _close_code(exc),
        "query_id": query_id,
        "detail": _error_detail(exc),
    }


def _close_code(exc: ConnectionClosed) -> int | None:
    for frame in (getattr(exc, "rcvd", None), getattr(exc, "sent", None)):
        if frame is not None and getattr(frame, "code", None) is not None:
            return frame.code
    return None


def _error_detail(exc: Exception) -> str:
    """Human-friendlier message, especially for the opaque 1008 policy close."""
    msg = str(exc)
    if "1008" in msg:
        return (
            "live socket rejected (1008 policy): refresh your POESESSID, confirm the "
            "League matches the trade site exactly, and close any other live search "
            "(browser or another stasher) on this session, then retry"
        )
    return msg


async def _consume(ws, pipeline, query_id, loop, stop_event, status, client) -> None:
    debug = client.config.live_debug
    while not stop_event.is_set():
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=RECV_POLL)
        except asyncio.TimeoutError:
            continue
        if debug:
            # Full frame (not truncated) so we can measure/decode the payload. Live
            # frames are small; the query_log columns are unbounded TEXT.
            pipeline.store.log_query("live", f"recv[{len(raw)}] {raw}", "ok")
        try:
            msg = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if not isinstance(msg, dict):
            continue
        # PoE2 live opens with a handshake: {"result": <JWT>} then {"auth": true}.
        # Item notifications are a separate frame; PoE1 used {"new": [...]} — log any
        # other shape distinctly so we can confirm the PoE2 format from real traffic.
        if "auth" in msg or "result" in msg:
            continue
        ids = msg.get("new")
        if not ids and debug:
            pipeline.store.log_query("live", f"unhandled frame keys={list(msg)}", "ok")
        if ids:
            stored = await loop.run_in_executor(
                None, pipeline.submit_hashes, ids, query_id
            )
            pipeline.store.log_query("live", f"new ×{len(ids)} → stored {stored}", "ok")
            status(mode="live", live_connected=True, last_push=len(ids), last_new=stored)


def _ws_uri(client: TradeClient, query_id: str) -> str:
    base = client.config.base_url.replace("https://", "wss://").replace("http://", "ws://")
    creds = client.creds()
    league = quote(creds.league, safe="")
    return f"{base}/api/trade2/live/{client.config.realm}/{league}/{query_id}"


def _connect(uri: str, client: TradeClient):
    creds = client.creds()
    league = quote(creds.league, safe="")
    headers = {
        "Origin": client.config.base_url,
        "User-Agent": client.config.user_agent,
        # Mirror the browser/HTTP client: the live endpoint checks the Referer of the
        # search page it was opened from. Without it the handshake can be rejected.
        "Referer": f"{client.config.base_url}/trade2/search/{client.config.realm}/{league}",
    }
    if creds.poesessid:
        headers["Cookie"] = f"POESESSID={creds.poesessid}"
    keepalive = dict(
        ping_interval=client.config.live_ping_interval,
        ping_timeout=client.config.live_ping_timeout,
    )
    # websockets renamed extra_headers -> additional_headers in v14; support both.
    try:
        return websockets.connect(uri, additional_headers=headers, **keepalive)
    except TypeError:
        return websockets.connect(uri, extra_headers=headers, **keepalive)
