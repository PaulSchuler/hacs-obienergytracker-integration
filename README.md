# "OBI Energy Tracker" - HACS Integration
This integration allows you to monitor your **OBI Energy Tracker** device directly within Home Assistant. The OBI Energy Tracker is a cost-effective solution for reading smart energy meters, typically accessed via the heyOBI smartphone application.e.

## Installation

Add this repository, via custom repository: https://www.hacs.xyz/docs/faq/custom_repositories/

## OBI Energy Tracker

<img src="https://bilder.obi.de/d9c6b340-b37f-48fd-92f2-72114bad03ad/prZZK/image.jpeg" width="200" alt="Energy Tracker Device">

The "OBI Energy Tracker" is a low cost device to read out smart energy meters. In default you can access the data in the "heyOBI" application on our smartphone.
I extracted the API Calls from the backend of the application, and created this "Home Assistant" Integration.

## Configuration

During setup, you'll need:

- **Email**: Your "OBI" account email address
- **Password**: Your "OBI" account password
- **Country**: Country code (default: DE for Germany)

## API Details

The integration retrieves:

- Meter Reading
- Feed-In Meter Reading
- Live Power, via the live mode switch (see below)
- Current Power / Current Feed-in Power (derived, see below)
- Battery Level
- Online Status
- Connection Strength
- Last Record Received At

### Live mode

The heyOBI app shows a real-time power value, and this integration can do the
same. It is exposed as a **switch**, not as an always-on sensor, because live
mode works by lowering the sensor's upload interval from 300 to 2 seconds -
and that sensor runs on a battery.

Switch `Live mode` on and the `Live Power` sensor starts updating every two
seconds from a websocket. Switch it off, let it time out, reload the
integration or shut Home Assistant down, and the upload interval goes back to
300 seconds.

The timeout defaults to 10 minutes and can be changed in the integration's
options; `0` disables it. Leaving live mode running permanently will drain the
sensor battery, so do that deliberately.

The protocol behind it is documented in
[docs/live-mode-api.md](docs/live-mode-api.md).

### Derived power

Besides live mode there are two derived power sensors that work without
touching the sensor's upload interval. The historical endpoints expose meter
readings (Wh), not power (W), so these are computed: they take the two
most recent meter readings and divide the energy between them by the time
between them. The value is an average over the device's reporting interval,
not an instantaneous reading, and the `measurement_interval_seconds` attribute
tells you how long that interval was. If only one reading is available, or the
readings are more than an hour apart, the sensors report `unknown`.

The heyOBI app shows a true live value. Static analysis of the app (v26.9.2)
found where it comes from: the app lowers the sensor's `uploadInterval` and
then reads power values off a WebSocket at `api.obi.com`. The protocol is
written up in [docs/live-mode-api.md](docs/live-mode-api.md).

Two tools verify it against the real backend. `tools/live_dashboard.py` serves
a small page on `127.0.0.1:8765` where you enter your credentials and watch
the live watts arrive; `tools/live_mode_probe.py` does the same in the
terminal. Both restore the sensor's original upload interval when they stop,
because the sensor runs on a battery.

`tools/probe_obi_api.py` covers the REST side: it logs in with your account
and dumps the user payload, the raw record shapes, the real reporting cadence
and the response of a list of candidate endpoints:

```bash
pip install aiohttp
OBI_EMAIL=... OBI_PASSWORD=... OBI_COUNTRY=DE python tools/probe_obi_api.py
```

The live view has to be started in the app, and the dongle only streams while
it is open. `--live-session` uses that: it probes every candidate once with
the live view closed, waits for you to open it, probes again and prints what
changed.

```bash
python tools/probe_obi_api.py --live-session
```

If nothing answers differently during a session, the app uses a push channel
and the traffic has to be captured. A step-by-step guide for doing that with
a rooted Android emulator and mitmproxy is in
[docs/android-emulator-capture.md](docs/android-emulator-capture.md).

Output of the probe or a captured request is welcome in an issue - please
redact the token and personal data.

## Bruno

Unofficial API for the "heyOBI" backend, as used in this repository. The endpoints are not officially documented.

### Procedure

1. Perform **Login** → sets `token` (JWT) and `userId` (from the JWT payload).
2. Perform **Get bridge info** → sets `bridgeId` and `deviceId` based on the first
   linked sensor.
3. After that, **Get hourly data** and **Get meter data** can be called...

---

*Disclaimer: This integration is not affiliated with or endorsed by OBI. Use at your own risk.*
