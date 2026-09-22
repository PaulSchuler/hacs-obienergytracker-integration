#!/usr/bin/env python3
"""Local live dashboard for the heyOBI energy tracker.

Serves a small web page on 127.0.0.1 where you enter your OBI credentials and
watch the live power values arrive. The browser only ever talks to this local
server; the server does the login, sets the sensor's upload interval and holds
the WebSocket to OBI. A page served from the internet could do neither (CORS
on the login call, mixed content on the ws:// connection).

The protocol it speaks is documented in docs/live-mode-api.md.

Usage::

    pip install aiohttp
    python tools/live_dashboard.py

Then open http://127.0.0.1:8765 (the script tries to do that for you).

The sensor runs on a battery. A one-second upload interval drains it far
faster than the default, so the dashboard restores the previous interval when
you press Stop or close the page, unless you switch that off.

Credentials are kept in memory for the duration of the session and are never
written to disk.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
from datetime import datetime
import json
import logging
import ssl
import sys
import webbrowser
from typing import Any

import aiohttp
from aiohttp import web

_LOGGER = logging.getLogger("live_dashboard")

LOGIN_PATH = "/regi/auth/api/public/login"

# The app picks the login host from the market the account belongs to, and
# sends the matching country code in the body. A German account cannot log in
# against the Austrian portal and vice versa.
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

# On api.obi.com the energy-tracking REST API sits behind a path prefix
# (the app sets it as "energytracker/api"); the older host serves it at the
# root. The live WebSocket is a sibling of that prefix, not below it.
API_BASES = (
    "https://api.obi.com/energytracker/api",
    "https://energy-tracking-backend.prod-eks.dbs.obi.solutions",
)

LIVE_HOST = "api.obi.com"
LIVE_PATH = "/energytracker/api-livemode/retrieving"

SENSOR_MEDIA_TYPE = "application/vnd.obi.companion.energy-tracking.sensor.v2+json"
USER_MEDIA_TYPE = "application/vnd.obi.companion.energy-tracking.user.v1+json"

# How long to wait for an OBI frame before checking whether the browser is
# still there. Keeps a silent socket from hiding a closed page.
IDLE_TIMEOUT = 5.0

# The app sets the sensor's uploadInterval to 2 seconds for live mode and back
# to 300 when it leaves the live screen (dekompiliert: defpackage/r8c.java).
# Values below 2 are rejected with HTTP 400.
LIVE_INTERVAL = 2
IDLE_INTERVAL = 300

# Anything this short is a live-mode leftover, not a value worth restoring -
# putting it back would keep the battery-powered sensor uploading every
# couple of seconds forever.
MIN_SANE_RESTORE = 60


PAGE = """<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>OBI Energy Tracker Live</title>
<style>
  :root {
    --bg: #f4f4f5; --card: #ffffff; --fg: #18181b; --muted: #71717a;
    --line: #e4e4e7; --accent: #1d4ed8; --warn: #b45309; --bad: #b91c1c;
    --ok: #15803d;
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      --bg: #18181b; --card: #27272a; --fg: #fafafa; --muted: #a1a1aa;
      --line: #3f3f46; --accent: #60a5fa; --warn: #fbbf24; --bad: #f87171;
      --ok: #4ade80;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--fg);
    font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif;
    padding: 24px 16px;
  }
  main { max-width: 680px; margin: 0 auto; }
  h1 { font-size: 20px; margin: 0 0 20px; }
  .card {
    background: var(--card); border: 1px solid var(--line);
    border-radius: 12px; padding: 20px; margin-bottom: 16px;
  }
  label { display: block; font-size: 13px; color: var(--muted); margin: 0 0 4px; }
  .row { display: flex; gap: 12px; flex-wrap: wrap; }
  .row > div { flex: 1 1 160px; }
  input[type=text], input[type=password], input[type=number], select {
    width: 100%; padding: 9px 11px; border-radius: 8px;
    border: 1px solid var(--line); background: var(--bg); color: var(--fg);
    font: inherit;
  }
  .field { margin-bottom: 14px; }
  .check { display: flex; align-items: center; gap: 8px; font-size: 14px; }
  .check label { margin: 0; color: var(--fg); }
  button {
    padding: 10px 20px; border-radius: 8px; border: 0; font: inherit;
    font-weight: 600; cursor: pointer; background: var(--accent); color: #fff;
  }
  button.secondary { background: transparent; color: var(--fg);
    border: 1px solid var(--line); }
  button:disabled { opacity: .5; cursor: not-allowed; }
  .buttons { display: flex; gap: 10px; align-items: center; }
  .status { font-size: 13px; color: var(--muted); }
  .status.err { color: var(--bad); }
  .status.ok { color: var(--ok); }
  .reading { text-align: center; padding: 8px 0 4px; }
  .watt { font-size: 64px; font-weight: 700; line-height: 1.1;
    font-variant-numeric: tabular-nums; }
  .watt span { font-size: 22px; font-weight: 600; color: var(--muted); }
  .stamp { font-size: 13px; color: var(--muted); }
  .stats { display: flex; gap: 12px; flex-wrap: wrap; margin-top: 18px; }
  .stat { flex: 1 1 100px; border-top: 1px solid var(--line); padding-top: 8px; }
  .stat b { display: block; font-size: 17px; font-variant-numeric: tabular-nums; }
  .stat span { font-size: 12px; color: var(--muted); }
  svg { width: 100%; height: 110px; display: block; margin-top: 16px; }
  .hint { font-size: 12px; color: var(--muted); margin-top: 14px; }
  pre { font-size: 12px; background: var(--bg); border: 1px solid var(--line);
    border-radius: 8px; padding: 10px; overflow: auto; max-height: 320px;
    margin: 12px 0 0; white-space: pre-wrap; word-break: break-all; }
</style>
</head>
<body>
<main>
  <h1>OBI Energy Tracker &ndash; Live</h1>

  <div class="card" id="form">
    <div class="row">
      <div class="field"><label for="email">E-Mail</label>
        <input id="email" type="text" autocomplete="username"></div>
      <div class="field"><label for="password">Passwort</label>
        <input id="password" type="password" autocomplete="current-password"></div>
    </div>
    <div class="row">
      <div class="field"><label for="country">Land</label>
        <select id="country">
          <option value="DE">DE &ndash; www.obi.de</option>
          <option value="AT">AT &ndash; www.obi.at</option>
          <option value="CH">CH &ndash; www.obi.ch</option>
          <option value="CZ">CZ &ndash; www.obi.cz</option>
          <option value="HU">HU &ndash; www.obi.hu</option>
          <option value="PL">PL &ndash; www.obi.pl</option>
          <option value="SI">SI &ndash; www.obi.si</option>
          <option value="SK">SK &ndash; www.obi.sk</option>
        </select></div>
      <div class="field"><label for="interval">Upload-Intervall in Sekunden
        (App nutzt 2 f&uuml;r Live, 300 normal; Minimum 2)</label>
        <input id="interval" type="number" min="2" max="3600" value="2"></div>
    </div>
    <div class="field"><label for="token">JWT-Token (optional &ndash; wenn gesetzt,
      wird der Login uebersprungen)</label>
      <input id="token" type="text" placeholder="eyJ..."></div>
    <div class="field check">
      <input id="allmarkets" type="checkbox">
      <label for="allmarkets">Bei Fehlschlag alle OBI-L&auml;nder durchprobieren</label>
    </div>
    <div class="field check">
      <input id="restore" type="checkbox" checked>
      <label for="restore">Beim Stoppen auf Normalbetrieb zur&uuml;cksetzen
        (Batterie!)</label>
    </div>
    <div class="field check">
      <input id="setinterval" type="checkbox" checked>
      <label for="setinterval">Intervall &uuml;berhaupt setzen</label>
    </div>
    <div class="buttons">
      <button id="start">Start</button>
      <button id="stop" class="secondary" disabled>Stop</button>
      <span class="status" id="status">bereit</span>
    </div>
  </div>

  <div class="card">
    <div class="reading">
      <div class="watt" id="watt">&ndash;<span> W</span></div>
      <div class="stamp" id="stamp">noch keine Daten</div>
    </div>
    <svg id="chart" viewBox="0 0 600 110" preserveAspectRatio="none">
      <polyline id="line" fill="none" stroke="var(--accent)" stroke-width="2"
                stroke-linejoin="round" points=""></polyline>
    </svg>
    <div class="stats">
      <div class="stat"><b id="min">&ndash;</b><span>Minimum</span></div>
      <div class="stat"><b id="max">&ndash;</b><span>Maximum</span></div>
      <div class="stat"><b id="battery">&ndash;</b><span>Batterie</span></div>
      <div class="stat"><b id="rssi">&ndash;</b><span>RSSI</span></div>
      <div class="stat"><b id="count">0</b><span>Frames</span></div>
    </div>
    <div class="hint">Zugangsdaten gehen nur an das lokale Python-Skript auf
      diesem Rechner, nicht an den Browser-Verlauf und nicht auf die Platte.</div>
  </div>

  <div class="card">
    <div class="status">Protokoll &ndash; dieselben Zeilen stehen auch im
      Python-Fenster</div>
    <pre id="log">bereit</pre>
  </div>
</main>

<script>
(function () {
  const $ = (id) => document.getElementById(id);
  const NL = String.fromCharCode(10);
  let ws = null, values = [], minV = null, maxV = null, frames = 0;
  let lines = [];

  function logLine(text) {
    const t = new Date().toLocaleTimeString("de-DE");
    lines.push(t + "  " + text);
    if (lines.length > 400) lines.shift();
    const el = $("log");
    el.textContent = lines.join(NL);
    el.scrollTop = el.scrollHeight;
  }

  function setStatus(text, cls) {
    const el = $("status");
    el.textContent = text;
    el.className = "status" + (cls ? " " + cls : "");
    logLine(text);
  }

  function fmt(n, digits) {
    return n === null || n === undefined
      ? "\\u2013"
      : n.toLocaleString("de-DE", { minimumFractionDigits: digits,
                                    maximumFractionDigits: digits });
  }

  function draw() {
    if (values.length < 2) { $("line").setAttribute("points", ""); return; }
    const lo = Math.min(...values), hi = Math.max(...values);
    const span = (hi - lo) || 1;
    const step = 600 / (values.length - 1);
    const pts = values.map((v, i) =>
      i * step + "," + (105 - ((v - lo) / span) * 100)).join(" ");
    $("line").setAttribute("points", pts);
  }

  function onData(m) {
    frames += 1;
    $("count").textContent = frames;
    if (typeof m.power === "number") {
      $("watt").innerHTML = fmt(m.power, 1) + "<span> W</span>";
      minV = minV === null ? m.power : Math.min(minV, m.power);
      maxV = maxV === null ? m.power : Math.max(maxV, m.power);
      $("min").textContent = fmt(minV, 1);
      $("max").textContent = fmt(maxV, 1);
      values.push(m.power);
      if (values.length > 150) values.shift();
      draw();
    }
    if (m.battery !== null && m.battery !== undefined)
      $("battery").textContent = m.battery + " %";
    if (m.rssi !== null && m.rssi !== undefined)
      $("rssi").textContent = m.rssi;
    $("stamp").textContent = "aktualisiert " + new Date().toLocaleTimeString("de-DE");
    logLine("frame " + frames + ": " + m.raw);
  }

  function finish(text, cls) {
    setStatus(text, cls);
    $("start").disabled = false;
    $("stop").disabled = true;
    ws = null;
  }

  $("start").onclick = function () {
    values = []; minV = null; maxV = null; frames = 0; lines = []; draw();
    $("start").disabled = true;
    $("stop").disabled = false;
    setStatus("verbinde \\u2026");

    ws = new WebSocket("ws://" + location.host + "/ws");
    ws.onopen = function () {
      ws.send(JSON.stringify({
        email: $("email").value,
        password: $("password").value,
        country: $("country").value || "DE",
        all_markets: $("allmarkets").checked,
        token: $("token").value.trim(),
        interval: parseInt($("interval").value, 10) || 2,
        set_interval: $("setinterval").checked,
        restore: $("restore").checked
      }));
    };
    ws.onmessage = function (ev) {
      const m = JSON.parse(ev.data);
      if (m.type === "data") onData(m);
      else if (m.type === "status") setStatus(m.detail, m.ok ? "ok" : null);
      else if (m.type === "error") {
        // fromCharCode(10) instead of an escaped newline: this string travels
        // through a Python literal, where an escape is easy to mangle.
        const parts = m.detail.split(NL);
        setStatus(parts[0], "err");
        parts.slice(1).forEach(logLine);
      }
    };
    ws.onclose = function () {
      if (ws) finish("Verbindung beendet");
    };
    ws.onerror = function () { setStatus("Verbindungsfehler", "err"); };
  };

  $("stop").onclick = function () {
    if (ws) { const s = ws; ws = null; s.close(); }
    finish("gestoppt");
  };
})();
</script>
</body>
</html>
"""


class Reporter:
    """Send one line to the server console and to the page's log panel."""

    def __init__(self, browser: web.WebSocketResponse) -> None:
        self.browser = browser

    async def __call__(self, detail: str, ok: bool | None = None) -> None:
        _LOGGER.info("%s", detail)
        if not self.browser.closed:
            await self.browser.send_json(
                {"type": "status", "detail": detail, "ok": bool(ok)}
            )


def _client_session() -> aiohttp.ClientSession:
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


async def _login_once(
    session: aiohttp.ClientSession,
    host: str,
    email: str,
    password: str,
    country: str,
) -> tuple[int, str, str | None]:
    """Try one market. Returns (status, detail, token or None)."""
    headers = {
        "Accept-Encoding": "gzip",
        "Connection": "Keep-Alive",
        "Content-Type": "application/json",
        "x-app-type": "b2c",
        "x-obi-locale": "de-DE",
        "User-Agent": "heyOBI APP / Android Phone 30",
    }
    body = {"email": email, "password": password, "country": country}

    try:
        async with session.post(
            f"https://{host}{LOGIN_PATH}", json=body, headers=headers
        ) as response:
            text = (await response.text())[:300].replace("\n", " ").strip()
            if response.status == 200:
                try:
                    return 200, "ok", (await response.json())["token"]
                except (ValueError, KeyError):
                    return 200, f"kein Token in der Antwort: {text}", None
            hint = response.headers.get("WWW-Authenticate", "")
            detail = f"HTTP {response.status} {text} {hint}".strip()
            return response.status, detail, None
    except (OSError, aiohttp.ClientError) as err:
        return 0, f"{type(err).__name__}: {err}", None


async def _login(
    session: aiohttp.ClientSession,
    email: str,
    password: str,
    country: str,
    try_all_markets: bool = False,
) -> tuple[str, str]:
    """Log in and return (token, country that worked)."""
    country = (country or "DE").strip().upper()
    order = [country] if country in MARKETS else list(MARKETS)
    if try_all_markets:
        order = [country] + [c for c in MARKETS if c != country]

    problems = []
    for code in order:
        status, detail, token = await _login_once(
            session, MARKETS[code], email, password, code
        )
        if token:
            return token, code
        problems.append(f"{code} ({MARKETS[code]}): {detail}")
        if status not in (401, 403, 404, 0) and not try_all_markets:
            break

    raise RuntimeError(
        "Login fehlgeschlagen. Versuche:\n"
        + "\n".join(problems)
        + "\n\nHaeufigste Ursachen: falsches Land, oder das OBI-Konto haengt an "
        "Google/Apple-Anmeldung statt an einem Passwort - dann funktioniert "
        "dieser Endpunkt grundsaetzlich nicht."
    )


async def _resolve_device(
    session: aiohttp.ClientSession, token: str, user_id: str
) -> tuple[str, str, str, Any]:
    """Return (base URL, bridgeId, sensorId, current uploadInterval)."""
    errors = []
    for base in API_BASES:
        url = f"{base}/users/{user_id}"
        _LOGGER.info("GET %s", url)
        try:
            async with session.get(
                url, headers=_headers(token, USER_MEDIA_TYPE)
            ) as response:
                _LOGGER.info("GET -> HTTP %s", response.status)
                if response.status != 200:
                    errors.append(f"{base}: HTTP {response.status}")
                    continue
                data = await response.json()
        except (OSError, aiohttp.ClientError) as err:
            errors.append(f"{base}: {type(err).__name__}")
            continue

        bridge = data.get("bridge") or {}
        sensors = bridge.get("sensors") or []
        if not bridge.get("id") or not sensors:
            errors.append(f"{base}: keine Bridge/Sensoren im Profil")
            continue
        sensor = sensors[0]
        _LOGGER.info(
            "bridge=%s sensors=%d outlets=%d",
            bridge.get("id"),
            len(sensors),
            len(bridge.get("outlets") or []),
        )
        _LOGGER.info("sensor[0]=%s", json.dumps(sensor, ensure_ascii=False))
        return base, bridge["id"], sensor["id"], sensor.get("uploadInterval")

    raise RuntimeError("Geraet nicht gefunden - " + "; ".join(errors))


async def _set_interval(
    session: aiohttp.ClientSession,
    token: str,
    base: str,
    sensor_id: str,
    interval: int,
) -> tuple[bool, str]:
    """PATCH the sensor's upload interval. Returns (ok, detail)."""
    url = f"{base}/sensors/{sensor_id}"
    headers = _headers(token, SENSOR_MEDIA_TYPE)
    headers["Content-Type"] = SENSOR_MEDIA_TYPE
    payload = {"id": sensor_id, "uploadInterval": interval}
    _LOGGER.info("PATCH %s  body=%s", url, payload)

    try:
        async with session.patch(url, headers=headers, data=json.dumps(payload)) as r:
            body = (await r.text())[:300]
            _LOGGER.info("PATCH -> HTTP %s  %s", r.status, body)
            return r.status < 300, f"HTTP {r.status} {body}".strip()
    except (OSError, aiohttp.ClientError) as err:
        _LOGGER.warning("PATCH -> %s: %s", type(err).__name__, err)
        return False, f"{type(err).__name__}: {err}"


async def _pump(
    obi_ws: aiohttp.ClientWebSocketResponse,
    browser: web.WebSocketResponse,
    report: Reporter,
) -> None:
    """Forward OBI frames to the browser until either side goes away."""
    frames = 0
    idle = 0.0

    while not browser.closed:
        try:
            msg = await asyncio.wait_for(obi_ws.receive(), timeout=IDLE_TIMEOUT)
        except asyncio.TimeoutError:
            idle += IDLE_TIMEOUT
            # Without this the page just says "verbunden" forever and there is
            # no way to tell a silent socket from a broken one.
            await report(
                f"seit {idle:.0f}s verbunden, noch {frames} Frames empfangen"
            )
            continue

        idle = 0.0

        if msg.type is aiohttp.WSMsgType.TEXT:
            frames += 1
            raw = msg.data
            _LOGGER.info("WS frame %d: %s", frames, raw[:300])
            try:
                parsed = json.loads(raw)
            except ValueError:
                parsed = {}
                await report(f"Frame ist kein JSON: {raw[:120]}")
            data = parsed.get("data") if isinstance(parsed, dict) else None
            data = data if isinstance(data, dict) else {}
            await browser.send_json(
                {
                    "type": "data",
                    "power": data.get("power"),
                    "battery": data.get("battery"),
                    "rssi": data.get("rssi"),
                    "event": parsed.get("event") if isinstance(parsed, dict) else None,
                    "raw": raw[:500],
                }
            )
        elif msg.type is aiohttp.WSMsgType.BINARY:
            frames += 1
            _LOGGER.info("WS binary frame, %d bytes", len(msg.data))
            await browser.send_json(
                {"type": "data", "raw": f"<binary {len(msg.data)} bytes>"}
            )
        else:
            detail = getattr(msg, "data", "")
            _LOGGER.info("WS closed by OBI: %s %s", msg.type.name, detail)
            await report(
                f"OBI hat die Verbindung beendet ({msg.type.name} {detail}) "
                f"nach {frames} Frames"
            )
            return


async def _connect_live(
    session: aiohttp.ClientSession,
    token: str,
    bridge_id: str,
    sensor_id: str,
    browser: web.WebSocketResponse,
    report: Reporter,
) -> None:
    """Open the live socket, trying ws:// first and wss:// as a fallback."""
    query = f"?bridgeId={bridge_id}&sensorId={sensor_id}"
    problems = []

    # wss:// works; plain ws:// on port 80 is answered with HTTP 400, even
    # though the app's bytecode asks for it. Measured 2026-09-22.
    for url in (
        f"wss://{LIVE_HOST}{LIVE_PATH}{query}",
        f"ws://{LIVE_HOST}{LIVE_PATH}{query}",
    ):
        await report(f"WebSocket-Versuch: {url}")
        try:
            async with session.ws_connect(
                url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "User-Agent": "app_client",
                },
                heartbeat=30,
            ) as obi_ws:
                await report("WebSocket offen - warte auf Frames", ok=True)
                await _pump(obi_ws, browser, report)
                return
        except aiohttp.WSServerHandshakeError as err:
            problems.append(f"{url} -> HTTP {err.status} {err.message}")
            await report(f"Handshake abgelehnt: HTTP {err.status} {err.message}")
        except (OSError, aiohttp.ClientError) as err:
            problems.append(f"{url} -> {type(err).__name__}: {err}")
            await report(f"Verbindung fehlgeschlagen: {type(err).__name__}: {err}")

    raise RuntimeError("Live-Socket nicht erreichbar: " + "; ".join(problems))


async def ws_handler(request: web.Request) -> web.WebSocketResponse:
    """Drive one dashboard session for one browser tab."""
    browser = web.WebSocketResponse(heartbeat=20)
    await browser.prepare(request)

    first = await browser.receive()
    if first.type is not aiohttp.WSMsgType.TEXT:
        await browser.close()
        return browser

    config = json.loads(first.data)
    report = Reporter(browser)
    restore_to: int | None = None
    base = sensor_id = ""
    _LOGGER.info("--- neue Sitzung ---")

    async with _client_session() as session:
        try:
            token = (config.get("token") or "").strip()
            if token:
                await report("nutze uebergebenen Token")
            else:
                await report("melde an …")
                token, market = await _login(
                    session,
                    config.get("email", ""),
                    config.get("password", ""),
                    config.get("country", "DE"),
                    bool(config.get("all_markets")),
                )
                await report(f"angemeldet ({market})", ok=True)
            user_id = _account_id(token)
            if not user_id:
                raise RuntimeError("Kein accountId im Token")

            await report("suche Geraet …")
            base, bridge_id, sensor_id, original = await _resolve_device(
                session, token, user_id
            )
            await report(
                f"Sensor {sensor_id} an Bridge {bridge_id} ueber {base}, "
                f"aktuelles uploadInterval={original!r}"
            )

            if config.get("set_interval", True):
                interval = max(int(config.get("interval", LIVE_INTERVAL)), 2)
                ok, detail = await _set_interval(
                    session, token, base, sensor_id, interval
                )
                await report(f"Intervall auf {interval} setzen: {detail}", ok=ok)
                if ok and config.get("restore", True):
                    if isinstance(original, int) and original >= MIN_SANE_RESTORE:
                        restore_to = original
                    else:
                        restore_to = IDLE_INTERVAL
                        await report(
                            f"uploadInterval war {original!r} - das ist bereits "
                            f"ein Live-Wert. Beim Stoppen wird auf "
                            f"{IDLE_INTERVAL} gesetzt, nicht zurueck auf "
                            f"{original!r}."
                        )

            await _connect_live(
                session, token, bridge_id, sensor_id, browser, report
            )

        except (RuntimeError, ValueError, KeyError) as err:
            _LOGGER.error("Abbruch: %s", err)
            if not browser.closed:
                await browser.send_json({"type": "error", "detail": str(err)})
        except (OSError, aiohttp.ClientError) as err:
            _LOGGER.error("Netzwerkfehler: %s: %s", type(err).__name__, err)
            if not browser.closed:
                await browser.send_json(
                    {"type": "error", "detail": f"{type(err).__name__}: {err}"}
                )
        finally:
            if restore_to is not None:
                ok, detail = await _set_interval(
                    session, token, base, sensor_id, restore_to
                )
                _LOGGER.info("Intervall auf %s zurueckgesetzt: %s", restore_to, detail)
                if not browser.closed:
                    await report(
                        f"Intervall auf {restore_to} zurueckgesetzt: {detail}", ok=ok
                    )
            if not browser.closed:
                await browser.close()

    return browser


async def index(request: web.Request) -> web.Response:
    """Serve the dashboard page."""
    return web.Response(text=PAGE, content_type="text/html")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--no-browser", action="store_true", help="do not open a browser window"
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")

    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/ws", ws_handler)

    url = f"http://127.0.0.1:{args.port}"
    print(f"\n  Dashboard: {url}\n  Beenden mit Strg+C\n")
    if not args.no_browser:
        webbrowser.open(url)

    # Bound to loopback on purpose: the page takes credentials.
    web.run_app(app, host="127.0.0.1", port=args.port, print=None)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
