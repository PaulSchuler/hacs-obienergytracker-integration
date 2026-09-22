#!/usr/bin/env python3
"""Verify the live-mode API extracted from the heyOBI app.

The protocol is documented in docs/live-mode-api.md. It was read out of the
app's bytecode, not off the wire, so this script confirms it against the real
backend:

  1. logs in and resolves bridgeId / sensorId,
  2. remembers the sensor's current uploadInterval,
  3. sets the upload interval to --interval,
  4. opens the live WebSocket and prints every frame it receives,
  5. restores the original upload interval.

Step 5 matters: the sensor runs on a battery, and leaving it on a one-second
upload interval will drain it. The script restores the old value even when
interrupted with Ctrl+C.

Usage::

    pip install aiohttp
    OBI_EMAIL=... OBI_PASSWORD=... python tools/live_mode_probe.py

    # only listen, do not touch the sensor at all:
    python tools/live_mode_probe.py --no-set-interval
"""

from __future__ import annotations

import argparse
import asyncio
import base64
from datetime import datetime
import json
import os
import ssl
import sys
from typing import Any

import aiohttp

LOGIN_PATH = "/regi/auth/api/public/login"

# The login host follows the market the account belongs to.
MARKETS = {
    "DE": "www.obi.de",
    "AT": "www.obi.at",
    "CH": "www.obi.ch",
    "CZ": "www.obi.cz",
    "HU": "www.obi.hu",
    "PL": "www.obi.pl",
    "SI": "www.obi.si",
    "SK": "www.obi.sk",
}

# On api.obi.com the energy-tracking REST API sits behind a path prefix; the
# older host serves it at the root. Both are tried.
API_BASES = (
    "https://api.obi.com/energytracker/api",
    "https://energy-tracking-backend.prod-eks.dbs.obi.solutions",
)

# The app uses 2 seconds for live mode and 300 when it leaves the live screen.
LIVE_INTERVAL = 2
IDLE_INTERVAL = 300

LIVE_HOST = "api.obi.com"
LIVE_PATH = "/energytracker/api-livemode/retrieving"

SENSOR_MEDIA_TYPE = "application/vnd.obi.companion.energy-tracking.sensor.v2+json"
USER_MEDIA_TYPE = "application/vnd.obi.companion.energy-tracking.user.v1+json"


def _session() -> aiohttp.ClientSession:
    """Return a session whose TLS handshake advertises no ALPN protocol."""
    context = ssl.create_default_context()
    return aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=context))


def _account_id(token: str) -> str | None:
    """Return the accountId claim of the JWT without verifying it."""
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload)).get("accountId")


def _headers(token: str, media_type: str) -> dict[str, str]:
    return {
        "Accept": media_type,
        "Accept-Encoding": "gzip",
        "User-Agent": "app_client",
        "Authorization": f"Bearer {token}",
    }


async def _login(session: aiohttp.ClientSession, args: Any) -> str:
    headers = {
        "Content-Type": "application/json",
        "x-app-type": "b2c",
        "x-obi-locale": "de-DE",
        "User-Agent": "heyOBI APP / Android Phone 30",
    }
    country = (args.country or "DE").strip().upper()
    host = MARKETS.get(country, MARKETS["DE"])
    body = {"email": args.email, "password": args.password, "country": country}
    url = f"https://{host}{LOGIN_PATH}"
    async with session.post(url, json=body, headers=headers) as response:
        if response.status != 200:
            detail = (await response.text())[:200].strip()
            markets = ", ".join(MARKETS)
            raise SystemExit(
                f"Login failed at {url} with status {response.status} {detail}"
                f" -- wrong market? Known: {markets}"
            )
        return (await response.json())["token"]


async def _resolve_device(
    session: aiohttp.ClientSession, token: str, user_id: str
) -> tuple[str, str, str, Any]:
    """Return (base URL, bridgeId, sensorId, current uploadInterval)."""
    for base in API_BASES:
        url = f"{base}/users/{user_id}"
        try:
            async with session.get(
                url, headers=_headers(token, USER_MEDIA_TYPE)
            ) as response:
                print(f"GET {url} -> {response.status}")
                if response.status != 200:
                    continue
                data = await response.json()
        except (OSError, aiohttp.ClientError) as err:
            print(f"GET {url} -> {type(err).__name__}: {err}")
            continue

        bridge = data.get("bridge") or {}
        sensors = bridge.get("sensors") or []
        if not bridge.get("id") or not sensors:
            continue
        sensor = sensors[0]
        print(f"  bridge={bridge['id']} sensor={sensor.get('id')}")
        print(f"  sensor payload: {json.dumps(sensor, ensure_ascii=False)}")
        return base, bridge["id"], sensor["id"], sensor.get("uploadInterval")

    raise SystemExit("Could not resolve bridge/sensor on any known host")


async def _set_interval(
    session: aiohttp.ClientSession,
    token: str,
    base: str,
    sensor_id: str,
    interval: int,
) -> bool:
    """PATCH the sensor's upload interval. Returns True on success."""
    url = f"{base}/sensors/{sensor_id}"
    headers = _headers(token, SENSOR_MEDIA_TYPE)
    headers["Content-Type"] = SENSOR_MEDIA_TYPE
    payload = {"id": sensor_id, "uploadInterval": interval}

    try:
        async with session.patch(url, headers=headers, data=json.dumps(payload)) as r:
            text = (await r.text())[:200]
            print(f"PATCH {url} uploadInterval={interval} -> {r.status} {text}")
            return r.status < 300
    except (OSError, aiohttp.ClientError) as err:
        print(f"PATCH {url} -> {type(err).__name__}: {err}")
        return False


async def _listen(
    session: aiohttp.ClientSession,
    token: str,
    bridge_id: str,
    sensor_id: str,
    seconds: int,
) -> bool:
    """Open the live WebSocket and print frames. Returns True if one arrived."""
    query = f"?bridgeId={bridge_id}&sensorId={sensor_id}"
    # wss:// works; plain ws:// on port 80 answers HTTP 400, even though the
    # app's bytecode asks for it. Measured 2026-09-22.
    candidates = [
        f"wss://{LIVE_HOST}{LIVE_PATH}{query}",
        f"ws://{LIVE_HOST}{LIVE_PATH}{query}",
    ]

    for url in candidates:
        print(f"\nconnecting: {url}")
        try:
            async with session.ws_connect(
                url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "User-Agent": "app_client",
                },
                heartbeat=30,
            ) as ws:
                print("connected - waiting for frames (Ctrl+C to stop)")
                got_frame = False
                deadline = asyncio.get_running_loop().time() + seconds

                while True:
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        break
                    try:
                        msg = await asyncio.wait_for(ws.receive(), timeout=remaining)
                    except asyncio.TimeoutError:
                        break

                    stamp = f"{datetime.now():%H:%M:%S}"
                    if msg.type is aiohttp.WSMsgType.TEXT:
                        got_frame = True
                        print(f"{stamp}  {msg.data[:300]}")
                    elif msg.type is aiohttp.WSMsgType.BINARY:
                        got_frame = True
                        print(f"{stamp}  <binary {len(msg.data)} bytes>")
                    elif msg.type in (
                        aiohttp.WSMsgType.CLOSE,
                        aiohttp.WSMsgType.CLOSED,
                        aiohttp.WSMsgType.ERROR,
                    ):
                        print(f"{stamp}  closed: {msg.type.name} {msg.data}")
                        break

                if got_frame:
                    return True
                print("no frames received on this URL")
        except aiohttp.WSServerHandshakeError as err:
            print(f"handshake failed: HTTP {err.status} {err.message}")
        except (OSError, aiohttp.ClientError) as err:
            print(f"{type(err).__name__}: {err}")

    return False


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", default=os.environ.get("OBI_EMAIL"))
    parser.add_argument("--password", default=os.environ.get("OBI_PASSWORD"))
    parser.add_argument("--country", default=os.environ.get("OBI_COUNTRY", "DE"))
    parser.add_argument(
        "--interval",
        type=int,
        default=2,
        help="uploadInterval in seconds (app uses 2 for live, 300 idle; min 2)"
    )
    parser.add_argument(
        "--seconds", type=int, default=60, help="how long to listen (default 60)"
    )
    parser.add_argument(
        "--no-set-interval",
        action="store_true",
        help="do not touch the sensor, only open the WebSocket",
    )
    args = parser.parse_args()

    if not args.email or not args.password:
        parser.error("set OBI_EMAIL and OBI_PASSWORD, or pass --email/--password")

    async with _session() as session:
        token = await _login(session, args)
        user_id = _account_id(token)
        if not user_id:
            raise SystemExit("No accountId in token")
        print(f"Logged in, accountId={user_id}\n")

        base, bridge_id, sensor_id, original = await _resolve_device(
            session, token, user_id
        )
        print(f"\ncurrent uploadInterval: {original!r}")

        changed = False
        try:
            if not args.no_set_interval:
                changed = await _set_interval(
                    session, token, base, sensor_id, args.interval
                )
                if not changed:
                    print("setting the interval failed - listening anyway")

            got = await _listen(
                session, token, bridge_id, sensor_id, args.seconds
            )
            print(
                "\nframes received - the protocol works as documented"
                if got
                else "\nno frames - see docs/live-mode-api.md for the open points"
            )
        finally:
            if changed and isinstance(original, int) and original >= 60:
                print(f"\nrestoring uploadInterval={original}")
                await _set_interval(session, token, host, sensor_id, original)
            elif changed:
                print(
                    "\nWARNING: original uploadInterval unknown, the sensor is still "
                    f"uploading every {args.interval} - reset it in the app!"
                )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(130)
