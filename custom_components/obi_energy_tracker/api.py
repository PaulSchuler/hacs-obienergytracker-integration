"""API client for Obi EnergyTracker."""

from __future__ import annotations

from datetime import datetime, timedelta
import json
import logging
from typing import Any

from aiohttp import ClientError, ClientSession
import jwt

_LOGGER = logging.getLogger(__name__)

# API endpoints
LOGIN_URL = "https://www.obi.de/regi/auth/api/public/login"
# The app moved to api.obi.com, where the energy-tracking API sits behind a
# path prefix. The old host still answers, so it stays as a fallback: a
# request that comes back 404 is retried there and the working base is kept
# for the rest of the session.
ENERGY_TRACKING_URL = "https://api.obi.com/energytracker/api"
LEGACY_ENERGY_TRACKING_URL = (
    "https://energy-tracking-backend.prod-eks.dbs.obi.solutions"
)
API_BASES = (ENERGY_TRACKING_URL, LEGACY_ENERGY_TRACKING_URL)

LIVE_WS_URL = "wss://api.obi.com/energytracker/api-livemode/retrieving"

SENSOR_MEDIA_TYPE = "application/vnd.obi.companion.energy-tracking.sensor.v2+json"
USER_MEDIA_TYPE = "application/vnd.obi.companion.energy-tracking.user.v1+json"


class ObiEnergyTrackerAPI:
    """API client for Obi EnergyTracker."""

    def __init__(
        self,
        session: ClientSession,
        email: str,
        password: str,
        country: str = "DE",
        bridge_id: str | None = None,
        device_id: str | None = None,
    ) -> None:
        """Initialize the API client."""
        self.session = session
        self.email = email
        self.password = password
        self.country = country
        self.token: str | None = None
        self.bridge_id = bridge_id
        self.device_id = device_id
        self._base: str | None = None

    async def async_login(self) -> bool:
        """Authenticate with the Obi EnergyTracker API."""
        try:
            payload = {
                "email": self.email,
                "password": self.password,
                "country": self.country,
            }

            headers = {
                "Accept-Encoding": "gzip",
                "Connection": "Keep-Alive",
                "Content-Type": "application/json",
                "x-app-type": "b2c",
                "x-obi-locale": "de-DE",
                "User-Agent": "heyOBI APP / Android Phone 30",
            }

            async with self.session.post(
                LOGIN_URL, json=payload, headers=headers
            ) as response:
                if response.status != 200:
                    _LOGGER.error(
                        "Login failed with status %d",
                        response.status,
                    )
                    return False

                data = await response.json()
                self.token = data.get("token")

                if not self.token:
                    _LOGGER.error("No token received from login response")
                    return False

                _LOGGER.debug("Successfully authenticated with Obi EnergyTracker")
                return True
        except (OSError, ClientError) as err:
            _LOGGER.error("Login error: %s", err)
            return False

    async def _async_get_json(
        self,
        path: str,
        params: dict[str, str] | None = None,
        accept: str | None = None,
    ) -> Any | None:
        """GET a path, falling back to the legacy gateway on 404."""
        ordered = [self._base] if self._base else []
        ordered += [base for base in API_BASES if base != self._base]

        for base in ordered:
            url = f"{base}{path}"
            try:
                async with self.session.get(
                    url, params=params, headers=self._get_auth_headers(accept)
                ) as response:
                    if response.status == 404:
                        _LOGGER.debug("%s not found on %s, trying next", path, base)
                        continue
                    if response.status != 200:
                        _LOGGER.error(
                            "Request to %s failed: %d", url, response.status
                        )
                        return None
                    if self._base != base:
                        _LOGGER.debug("Using %s as API base", base)
                        self._base = base
                    return await response.json()
            except (OSError, ClientError) as err:
                _LOGGER.debug("Request to %s failed: %s", url, err)
                continue

        _LOGGER.error("No gateway answered for %s", path)
        return None

    async def async_get_bridge_info(self) -> dict[str, Any] | None:
        """Get bridge/device IDs and current device details from user profile."""
        if not self.token:
            return None

        try:
            # Decode JWT to get userId
            decoded_token = jwt.decode(self.token, options={"verify_signature": False})
            user_id = decoded_token.get("accountId")

            if not user_id:
                _LOGGER.error("No accountId found in token")
                return None

            data = await self._async_get_json(
                f"/users/{user_id}",
                accept=USER_MEDIA_TYPE,
            )
            if data is None:
                return None

            bridge = data.get("bridge")
            if not bridge:
                _LOGGER.error("No bridge found in user info")
                return None

            self.bridge_id = bridge.get("id")
            sensors = bridge.get("sensors", [])
            selected_sensor: dict[str, Any] | None = None

            if self.device_id:
                selected_sensor = next(
                    (
                        sensor
                        for sensor in sensors
                        if isinstance(sensor, dict)
                        and sensor.get("id") == self.device_id
                    ),
                    None,
                )

            if selected_sensor is None and sensors:
                first_sensor = sensors[0]
                selected_sensor = (
                    first_sensor if isinstance(first_sensor, dict) else None
                )

            if selected_sensor:
                self.device_id = selected_sensor.get("id")

            if not self.bridge_id or not self.device_id:
                _LOGGER.error("Could not find bridge_id or device_id")
                return None

            return {
                "bridge_id": self.bridge_id,
                "device_id": self.device_id,
                "batteryLevel": selected_sensor.get("batteryLevel")
                if selected_sensor
                else None,
                "isOnline": selected_sensor.get("isOnline")
                if selected_sensor
                else None,
                "connectionStrength": selected_sensor.get("connectionStrength")
                if selected_sensor
                else None,
                "lastRecordReceivedAt": selected_sensor.get("lastRecordReceivedAt")
                if selected_sensor
                else None,
            }
        except (jwt.DecodeError, OSError, ClientError) as err:
            _LOGGER.error("Error getting bridge info: %s", err)
            return None

    async def async_get_device_info(self) -> dict[str, Any] | None:
        """Get current device details from the bridge info response."""
        bridge_info = await self.async_get_bridge_info()
        if not bridge_info:
            return None

        return {
            "batteryLevel": bridge_info.get("batteryLevel"),
            "isOnline": bridge_info.get("isOnline"),
            "connectionStrength": bridge_info.get("connectionStrength"),
            "lastRecordReceivedAt": bridge_info.get("lastRecordReceivedAt"),
        }

    async def async_get_hourly_data(
        self,
        start_date: datetime | None = None,
        num_days: int = 1,
    ) -> dict[str, Any] | None:
        """Get hourly energy data for multiple days.

        Args:
            start_date: Start date for data retrieval (defaults to today)
            num_days: Number of days to fetch (default 1)

        Returns:
            Dictionary containing hourly energy data
        """
        if not self.token or not self.bridge_id or not self.device_id:
            return None

        try:
            if start_date is None:
                start_date = datetime.now()

            # Format as ISO 8601 datetime with Z suffix for UTC
            # The API expects: start_dateT23:00:00Z/PT{days}H format
            # So we use start_date at 23:00 UTC of previous day for 24-hour window
            duration_start = start_date.replace(
                hour=23, minute=0, second=0, microsecond=0
            )
            duration_hours = num_days * 24

            duration_str = f"{duration_start.isoformat()}Z/PT{duration_hours}H"

            path = (
                f"/historical-data/{self.bridge_id}/{self.device_id}/hourly"
            )
            params = {
                "duration": duration_str,
                "measures": "energy,negative_energy",
            }

            return await self._async_get_json(path, params=params)
        except OSError as err:
            _LOGGER.error("Error getting hourly data: %s", err)
            return None

    async def async_get_meter_data(self) -> dict[str, Any] | None:
        """Get meter reading data (Zählerstand)."""
        if not self.token or not self.bridge_id or not self.device_id:
            return None

        try:
            # Dynamic duration: a 6-hour window ending now
            # Meter readings represent the total state at points in time
            now = datetime.now()
            start_time = now - timedelta(hours=6)
            # Format: 2026-01-18T08:55:11.896Z
            start_time_str = start_time.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
            duration_str = f"{start_time_str}/PT6H"

            path = (
                f"/historical-data/{self.bridge_id}/{self.device_id}/meter"
            )
            params = {
                "duration": duration_str,
                "measures": "energy,negative_energy",
            }

            return await self._async_get_json(path, params=params)
        except OSError as err:
            _LOGGER.error("Error getting meter data: %s", err)
            return None

    def _get_auth_headers(self, accept: str | None = None) -> dict[str, str]:
        """Get headers with authorization token."""
        accept_header = accept or (
            "application/vnd.obi.companion.energy-tracking.historical-record.v1+json"
        )
        return {
            "Accept": accept_header,
            "Accept-Encoding": "gzip",
            "User-Agent": "app_client",
            "Authorization": f"Bearer {self.token}",
            "Connection": "Keep-Alive",
        }

    async def async_set_upload_interval(self, interval: int) -> bool:
        """Set the sensor's upload interval, which is what drives live mode.

        The backend accepts only the two values the app uses (2 for live, 300
        for idle) and answers 400 for anything else.
        """
        if not self.token or not self.device_id:
            return False

        base = self._base or ENERGY_TRACKING_URL
        url = f"{base}/sensors/{self.device_id}"
        payload = json.dumps({"id": self.device_id, "uploadInterval": interval})

        for attempt in (1, 2):
            headers = self._get_auth_headers(SENSOR_MEDIA_TYPE)
            headers["Content-Type"] = SENSOR_MEDIA_TYPE
            try:
                async with self.session.patch(
                    url, data=payload, headers=headers
                ) as response:
                    if response.status == 401 and attempt == 1:
                        _LOGGER.debug("Upload interval got 401, logging in again")
                        if await self.async_login():
                            continue
                        return False
                    if response.status >= 300:
                        body = (await response.text())[:200]
                        _LOGGER.error(
                            "Failed to set upload interval to %d: %d %s",
                            interval,
                            response.status,
                            body,
                        )
                        return False
                    _LOGGER.debug("Upload interval set to %d", interval)
                    return True
            except (OSError, ClientError) as err:
                _LOGGER.error("Error setting upload interval: %s", err)
                return False

        return False

    def live_ws_connect(self) -> Any:
        """Return the websocket connection context manager for live data.

        Must be wss: the backend answers plain ws on port 80 with HTTP 400,
        even though the app's own code asks for it.
        """
        params = {"bridgeId": self.bridge_id or "", "sensorId": self.device_id or ""}
        return self.session.ws_connect(
            LIVE_WS_URL,
            params=params,
            headers={
                "Authorization": f"Bearer {self.token}",
                "User-Agent": "app_client",
            },
            heartbeat=30,
        )

    @staticmethod
    def parse_live_frame(raw: str) -> dict[str, Any] | None:
        """Return the data payload of a live frame, or None if unusable.

        Frames look like
        {"event":"mqttMessage","data":{"rssi":-75,"power":506,"battery":56}}
        """
        try:
            parsed = json.loads(raw)
        except ValueError:
            _LOGGER.debug("Live frame is not JSON: %s", raw[:120])
            return None

        if not isinstance(parsed, dict):
            return None

        data = parsed.get("data")
        return data if isinstance(data, dict) else None
