from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import and_, select

from db.models import Location

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


def _deg_per_km(lat: float) -> tuple[float, float]:
    """Rough degrees-per-km for latitude and longitude at given latitude."""
    lat_km = 1.0 / 111.32
    lon_km = 1.0 / (111.32 * max(math.cos(math.radians(lat)), 0.01))
    return lat_km, lon_km


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


@dataclass(slots=True)
class NearHit:
    location: Location
    distance_km: float


async def find_nearby(
    s: AsyncSession,
    lat: float,
    lon: float,
    radius_km: float = 5.0,
    limit: int = 10,
) -> list[NearHit]:
    """Return the `limit` closest locations within `radius_km` of (lat, lon)."""
    lat_km, lon_km = _deg_per_km(lat)
    lat_delta = radius_km * lat_km
    lon_delta = radius_km * lon_km

    # Bounding-box filter — index-friendly.
    rows = (
        await s.execute(
            select(Location).where(
                and_(
                    Location.latitude.between(lat - lat_delta, lat + lat_delta),
                    Location.longitude.between(lon - lon_delta, lon + lon_delta),
                )
            )
        )
    ).scalars().all()

    hits: list[NearHit] = []
    for loc in rows:
        d = haversine_km(lat, lon, loc.latitude, loc.longitude)
        if d <= radius_km:
            hits.append(NearHit(location=loc, distance_km=d))
    hits.sort(key=lambda h: h.distance_km)
    return hits[:limit]
