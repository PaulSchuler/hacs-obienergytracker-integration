"""HTTP session for the Obi EnergyTracker API."""

from __future__ import annotations

import aiohttp

from homeassistant.core import HomeAssistant, callback
from homeassistant.util import ssl as ssl_util


@callback
def async_create_obi_session(hass: HomeAssistant) -> aiohttp.ClientSession:
    """Return a ClientSession whose TLS handshake advertises no ALPN protocol.

    www.obi.de sits behind a CDN that inspects the TLS ClientHello and answers
    404 to handshakes offering "http/1.1" as the only ALPN protocol. Login then
    fails with "Login failed with status 404" even though the credentials are
    never evaluated. Measured against the live login endpoint with identical
    headers, body and source IP:

        ALPN (none)             -> 401  reaches the real login handler
        ALPN ("http/1.1",)      -> 404  blocked at the edge
        ALPN ("http/1.1", "h2") -> server selects h2, which aiohttp cannot speak

    Home Assistant builds its shared client session with
    ssl_util.SSL_ALPN_HTTP11, so async_get_clientsession() always lands in the
    blocked case. ssl_util.client_context() defaults to SSL_ALPN_NONE; the
    server then falls back to HTTP/1.1 on its own. That context is cached and
    built at import time, so requesting it here does no blocking I/O in the
    event loop.

    async_create_clientsession() cannot be used instead, because it hardcodes
    Home Assistant's shared connector. The caller owns the returned session and
    is responsible for closing it.
    """
    return aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(ssl=ssl_util.client_context())
    )
