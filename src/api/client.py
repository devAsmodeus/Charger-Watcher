from __future__ import annotations

from typing import TYPE_CHECKING, Self

import httpx
import orjson
import structlog

from api.models import (
    ConnectorType,
    LocationDetail,
    LocationSummary,
    Operator,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

log = structlog.get_logger(__name__)

# Operator -> URL segment used by provider API gateway.
# For Network A the segment is empty (the path stays /central-system/api/v1/locations/...).
_OPERATOR_SEGMENT: dict[Operator, str] = {
    Operator.EVIKA: "/evika",
    Operator.BATTERY_FLY: "/battery-fly",
    Operator.MAIN: "",
}

# Operators that expose per-location OCPP status in public API.
# Network A uses SSE (see ProviderClient.stream_device_status).
SUPPORTED_OPERATORS_MVP: tuple[Operator, ...] = (Operator.EVIKA, Operator.BATTERY_FLY)

# OCPP statuses that mean the connector is effectively free.
FREE_OCPP_STATUSES = frozenset({"Available"})


class ProviderClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 15.0,
        proxy_url: str | None = None,
        origin: str | None = None,
        user_agent: str = "charger-watcher/0.1",
    ) -> None:
        headers = {
            "Accept": "application/json, text/plain, */*",
            "User-Agent": user_agent,
        }
        if origin:
            headers["Origin"] = origin
            headers["Referer"] = origin.rstrip("/") + "/map"
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            proxy=proxy_url or None,
            headers=headers,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def list_locations(self, operator: Operator) -> list[LocationSummary]:
        """GET /central-system/api/v1{segment}/locations/map"""
        segment = _OPERATOR_SEGMENT[operator]
        url = f"/central-system/api/v1{segment}/locations/map"
        resp = await self._client.get(url, params={"": ""})
        resp.raise_for_status()
        data = resp.json()
        return [LocationSummary.model_validate(item) for item in data]

    async def get_location_detail(
        self, operator: Operator, location_id: str
    ) -> LocationDetail:
        """Per-location detail. URL shape differs per operator."""
        if operator is Operator.MAIN:
            url = "/central-system/api/v1/locations/map/info"
            params = {"locationId": location_id}
        else:
            segment = _OPERATOR_SEGMENT[operator]
            url = f"/central-system/api/v1{segment}/locations/{location_id}/map"
            params = None
        resp = await self._client.get(url, params=params)
        resp.raise_for_status()
        return LocationDetail.model_validate(resp.json())

    async def list_connector_types(self) -> list[ConnectorType]:
        resp = await self._client.get("/central-system/api/v1/connectors/types")
        resp.raise_for_status()
        return [ConnectorType.model_validate(i) for i in resp.json()]

    async def stream_device_status(
        self, device_number: int | str
    ) -> AsyncIterator[dict]:
        """Long-lived SSE stream for Network A per-device status.

        Yields {"status": "<OCPP>", "codeByProtocol": <int>} frames. The server
        re-emits the full state of every connector every ~2s (heartbeat), so
        the caller is responsible for diffing against its last-seen snapshot.
        """
        url = "/central-system/api/v1/devices/status-stream"
        params = {"deviceNumber": device_number}
        async with self._client.stream(
            "GET", url, params=params, timeout=None
        ) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if not payload:
                    continue
                try:
                    yield orjson.loads(payload)
                except Exception as e:  # noqa: BLE001
                    log.warning("sse_parse_failed", err=str(e), line=line[:200])
