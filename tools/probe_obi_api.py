#!/usr/bin/env python3
"""Probe the undocumented heyOBI energy-tracking backend for live data.

The integration only knows the endpoints that were extracted from older app
traffic. The app meanwhile shows a live power value, which has to come from
either an endpoint this repository does not know yet, or from meter records
that arrive faster than the ones we look at. This script answers both
questions against your own account:

  1. dumps the raw ``/users/{accountId}`` payload (feature flags, sensor
     capabilities, supported measures),
  2. dumps raw meter and hourly records so their real shape and cadence
     become visible,
  3. polls the meter endpoint repeatedly to measure how often the device
     actually reports,
  4. probes candidate live endpoints and candidate measures and prints the
     status code of each,
  5. with --live-session: takes that probe twice, once before and once while
     the live view is open in the app, and prints what changed.

Usage::

    pip install aiohttp
    OBI_EMAIL=... OBI_PASSWORD=... OBI_COUNTRY=DE python tools/probe_obi_api.py

    # with the phone in hand:
    python tools/probe_obi_api.py --live-session

Steps 4 and 5 send roughly 40 requests per pass to OBI's backend. Run them
once, not on a timer. The output may contain personal data (name, address,
device ids) - check it before pasting it into an issue.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
from datetime import datetime, timedelta, timezone
import json
import os
import ssl
import sys
import time
from typing import Any

import aiohttp

LOGIN_URL = "https://www.obi.de/regi/auth/api/public/login"
BASE_URL = "https://energy-tracking-backend.prod-eks.dbs.obi.solutions"

RECORD_ACCEPT = (
    "application/vnd.obi.companion.energy-tracking.historical-record.v1+json"
)

# Path segments the app could plausibly use for a live reading, appended to
# /historical-data/{bridge}/{device}/.
CANDIDATE_GRANULARITIES = (
    "meter",
    "hourly",
    "daily",
    "minutely",
    "minute",
    "quarter-hourly",
    "raw",
    "live",
    "latest",
    "current",
    "realtime",
    "real-time",
)

# Alternative top-level paths, formatted with the bridge and device id.
CANDIDATE_PATHS = (
    "/live-data/{bridge}/{device}",
    "/live-data/{bridge}/{device}/latest",
    "/realtime-data/{bridge}/{device}",
    "/real-time-data/{bridge}/{device}",
    "/current-data/{bridge}/{device}",
    "/live/{bridge}/{device}",
    "/measurements/{bridge}/{device}",
    "/telemetry/{bridge}/{device}",
    "/bridges/{bridge}/sensors/{device}/live",
    "/bridges/{bridge}/sensors/{device}/current",
    "/bridges/{bridge}/sensors/{device}",
    "/sensors/{device}/live",
    "/sensors/{device}/current",
)

# Measures the backend might expose besides the two we already use.
CANDIDATE_MEASURES = (
    "energy",
    "negative_energy",
    "power",
    "negative_power",
    "active_power",
    "instantaneous_power",
    "current_power",
    "current",
    "voltage",
)


def _session() -> aiohttp.ClientSession:
    """Return a session whose TLS handshake advertises no ALPN protocol.

    www.obi.de answers 404 to handshakes that offer only "http/1.1"; see
    custom_components/obi_energy_tracker/session.py for the measurements.
    """
    context = ssl.create_default_context()
    return aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=context))


def _account_id(token: str) -> str | None:
    """Return the accountId claim of the JWT without verifying it."""
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    claims = json.loads(base64.urlsafe_b64decode(payload))
    return claims.get("accountId")


def _auth_headers(token: str, accept: str = RECORD_ACCEPT) -> dict[str, str]:
    """Return the headers the app sends on energy-tracking requests."""
    return {
        "Accept": accept,
        "Accept-Encoding": "gzip",
        "User-Agent": "app_client",
        "Authorization": f"Bearer {token}",
        "Connection": "Keep-Alive",
    }


def _duration(hours: float) -> str:
    """Return an ISO-8601 interval of the given length, ending now."""
    start = datetime.now(timezone.utc) - timedelta(hours=hours)
    start_str = start.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    minutes = round(hours * 60)
    return f"{start_str}/PT{minutes}M"


async def _login(session: aiohttp.ClientSession, args: Any) -> str:
    """Authenticate and return the bearer token."""
    headers = {
        "Accept-Encoding": "gzip",
        "Connection": "Keep-Alive",
        "Content-Type": "application/json",
        "x-app-type": "b2c",
        "x-obi-locale": "de-DE",
        "User-Agent": "heyOBI APP / Android Phone 30",
    }
    payload = {
        "email": args.email,
        "password": args.password,
        "country": args.country,
    }
    async with session.post(LOGIN_URL, json=payload, headers=headers) as response:
        if response.status != 200:
            raise SystemExit(f"Login failed with status {response.status}")
        return (await response.json())["token"]


async def _get(
    session: aiohttp.ClientSession,
    url: str,
    token: str,
    *,
    params: dict[str, str] | None = None,
    accept: str = RECORD_ACCEPT,
) -> tuple[int, Any]:
    """GET a URL and return (status, parsed body or text snippet)."""
    try:
        async with session.get(
            url, params=params, headers=_auth_headers(token, accept)
        ) as response:
            text = await response.text()
            try:
                return response.status, json.loads(text)
            except ValueError:
                return response.status, text[:200]
    except (OSError, aiohttp.ClientError) as err:
        return 0, f"{type(err).__name__}: {err}"


def _section(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


async def _dump_user(
    session: aiohttp.ClientSession, token: str, user_id: str
) -> dict[str, Any]:
    """Print the full user payload; it lists the bridge and its sensors."""
    _section("1. User / bridge payload")
    accept = "application/vnd.obi.companion.energy-tracking.user.v1+json"
    status, body = await _get(
        session, f"{BASE_URL}/users/{user_id}", token, accept=accept
    )
    print(f"status {status}")
    print(json.dumps(body, indent=2, ensure_ascii=False)[:8000])
    return body if isinstance(body, dict) else {}


async def _dump_records(
    session: aiohttp.ClientSession, token: str, bridge: str, device: str
) -> None:
    """Print raw records so their field names and cadence are visible."""
    _section("2. Raw record shapes")
    for granularity, hours in (("meter", 1), ("hourly", 6)):
        url = f"{BASE_URL}/historical-data/{bridge}/{device}/{granularity}"
        params = {"duration": _duration(hours), "measures": "energy,negative_energy"}
        status, body = await _get(session, url, token, params=params)
        count = len(body) if isinstance(body, list) else "n/a"
        print(
            f"\n--- {granularity} (last {hours}h) -> status {status}, records {count}"
        )
        print(json.dumps(body, indent=2, ensure_ascii=False)[:4000])


async def _measure_cadence(
    session: aiohttp.ClientSession,
    token: str,
    bridge: str,
    device: str,
    polls: int,
    interval: int,
) -> None:
    """Poll the meter endpoint to see how fast new records appear."""
    _section(f"3. Reporting cadence ({polls} polls, {interval}s apart)")
    url = f"{BASE_URL}/historical-data/{bridge}/{device}/meter"
    params = {"duration": _duration(0.5), "measures": "energy"}
    seen: list[str] = []

    for poll in range(polls):
        if poll:
            await asyncio.sleep(interval)
        status, body = await _get(session, url, token, params=params)
        records = body if isinstance(body, list) else []
        last = json.dumps(records[-1], ensure_ascii=False) if records else "none"
        marker = " (unchanged)" if seen and last == seen[-1] else " NEW"
        print(
            f"{datetime.now():%H:%M:%S}  status {status}  "
            f"n={len(records)}  {last}{marker}"
        )
        seen.append(last)

    changes = sum(1 for a, b in zip(seen, seen[1:]) if a != b)
    print(
        f"\n{changes} change(s) over {polls * interval}s -> this is the best "
        "resolution a derived power sensor can reach via this endpoint."
    )


def _targets(bridge: str, device: str) -> list[tuple[str, str, dict[str, str] | None]]:
    """Return (label, url, params) for every candidate the probe tries.

    The durations are rebuilt on every call, so two snapshots taken minutes
    apart both ask for a window ending now and stay comparable.
    """
    window = {"duration": _duration(0.25), "measures": "energy"}
    targets: list[tuple[str, str, dict[str, str] | None]] = []

    for granularity in CANDIDATE_GRANULARITIES:
        targets.append(
            (
                f"historical-data/../{granularity}",
                f"{BASE_URL}/historical-data/{bridge}/{device}/{granularity}",
                dict(window),
            )
        )

    for template in CANDIDATE_PATHS:
        path = template.format(bridge=bridge, device=device)
        label = template.replace("{bridge}", "..").replace("{device}", "..")
        targets.append((label, f"{BASE_URL}{path}", None))
        targets.append((f"{label} ?duration", f"{BASE_URL}{path}", dict(window)))

    meter = f"{BASE_URL}/historical-data/{bridge}/{device}/meter"
    for measure in CANDIDATE_MEASURES:
        targets.append(
            (
                f"meter measures={measure}",
                meter,
                {"duration": _duration(0.25), "measures": measure},
            )
        )

    return targets


def _summarise(status: int, body: Any) -> str:
    """Condense a response into one line that two snapshots can be diffed on."""
    if isinstance(body, list):
        return f"{status} list[{len(body)}]"
    if isinstance(body, dict):
        return f"{status} dict{sorted(body)[:6]}"
    return f"{status} {str(body)[:80]}"


async def _snapshot(
    session: aiohttp.ClientSession, token: str, bridge: str, device: str
) -> dict[str, str]:
    """Call every candidate once and return label -> summary."""
    result: dict[str, str] = {}
    for label, url, params in _targets(bridge, device):
        status, body = await _get(session, url, token, params=params)
        result[label] = _summarise(status, body)
    return result


async def _probe_endpoints(
    session: aiohttp.ClientSession, token: str, bridge: str, device: str
) -> None:
    """Try candidate routes and measures and report what the backend says."""
    _section("4. Endpoint probe (404 = no such route, 200 = jackpot)")
    snapshot = await _snapshot(session, token, bridge, device)
    for label, summary in snapshot.items():
        print(f"  {summary:<26} {label}")


async def _live_session(
    session: aiohttp.ClientSession,
    token: str,
    bridge: str,
    device: str,
    interval: float,
    seconds: int,
) -> None:
    """Diff every endpoint against itself while the app streams live data.

    The live view is started on the phone and the dongle only streams while it
    is open, so the interesting question is what the backend answers during a
    session that it did not answer before. Anything that shows up here is the
    endpoint the integration would have to call.
    """
    _section("5. Live session diff")
    print("Leave the live view CLOSED for now - taking a baseline ...")
    before = await _snapshot(session, token, bridge, device)
    print(f"Baseline taken over {len(before)} candidates.\n")

    await asyncio.to_thread(
        input, ">>> Now start the live view in the heyOBI app, then press Enter: "
    )

    after = await _snapshot(session, token, bridge, device)

    print("\n--- responses that changed while live data is streaming")
    changed = 0
    for label, summary in after.items():
        if before.get(label) != summary:
            changed += 1
            print(f"  {label}")
            print(f"      before: {before.get(label)}")
            print(f"      during: {summary}")
    if not changed:
        print(
            "  none - no known-shaped endpoint behaves differently during a\n"
            "  live session, so the app most likely uses a push channel\n"
            "  (WebSocket/MQTT) and the traffic has to be captured."
        )

    print(f"\n--- meter endpoint, every {interval}s for {seconds}s (keep live open!)")
    url = f"{BASE_URL}/historical-data/{bridge}/{device}/meter"
    deadline = time.monotonic() + seconds
    previous: str | None = None

    while time.monotonic() < deadline:
        params = {"duration": _duration(5 / 60), "measures": "energy,negative_energy"}
        status, body = await _get(session, url, token, params=params)
        records = body if isinstance(body, list) else []
        last = json.dumps(records[-1], ensure_ascii=False) if records else "none"
        marker = "  NEW" if last != previous else ""
        print(
            f"{datetime.now():%H:%M:%S}  status {status}  "
            f"n={len(records)}{marker}  {last[:120]}"
        )
        previous = last
        await asyncio.sleep(interval)

    print(
        "\nIf records arrive every few seconds here, no new endpoint is needed:\n"
        "the existing meter endpoint carries the live stream while a session\n"
        "is open, and the integration only has to start one and poll faster."
    )


async def main() -> None:
    """Run the probe steps selected on the command line."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", default=os.environ.get("OBI_EMAIL"))
    parser.add_argument("--password", default=os.environ.get("OBI_PASSWORD"))
    parser.add_argument("--country", default=os.environ.get("OBI_COUNTRY", "DE"))
    parser.add_argument(
        "--polls", type=int, default=10, help="cadence samples (step 3)"
    )
    parser.add_argument(
        "--poll-interval", type=int, default=30, help="seconds between samples"
    )
    parser.add_argument(
        "--skip-cadence", action="store_true", help="skip the slow step 3"
    )
    parser.add_argument(
        "--skip-probe", action="store_true", help="skip the endpoint probe"
    )
    parser.add_argument(
        "--live-session",
        action="store_true",
        help="diff all endpoints against a running live session in the app",
    )
    parser.add_argument(
        "--live-seconds", type=int, default=120, help="live poll duration"
    )
    parser.add_argument(
        "--live-interval", type=float, default=5.0, help="live poll interval"
    )
    args = parser.parse_args()

    if not args.email or not args.password:
        parser.error("set OBI_EMAIL and OBI_PASSWORD, or pass --email/--password")

    async with _session() as session:
        token = await _login(session, args)
        user_id = _account_id(token)
        if not user_id:
            raise SystemExit("No accountId in token")
        print(f"Logged in, accountId={user_id}")

        user = await _dump_user(session, token, user_id)
        bridge_data = user.get("bridge") or {}
        bridge = bridge_data.get("id")
        sensors = bridge_data.get("sensors") or []
        device = sensors[0].get("id") if sensors else None
        if not bridge or not device:
            raise SystemExit("Could not determine bridge/device id")

        await _dump_records(session, token, bridge, device)

        if args.live_session:
            # The diff already probes every candidate twice; running the
            # standalone cadence and probe steps on top would only add noise.
            await _live_session(
                session,
                token,
                bridge,
                device,
                args.live_interval,
                args.live_seconds,
            )
            return

        if not args.skip_cadence:
            await _measure_cadence(
                session, token, bridge, device, args.polls, args.poll_interval
            )

        if not args.skip_probe:
            await _probe_endpoints(session, token, bridge, device)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(130)
