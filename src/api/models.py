from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class Operator(StrEnum):
    EVIKA = "evika"
    BATTERY_FLY = "battery-fly"
    MAIN = "central"


class LocationStatus(StrEnum):
    """Aggregate status of a location as returned in bulk list endpoints."""

    AVAILABLE = "AVAILABLE"
    FULLY_USED = "FULLY_USED"
    UNAVAILABLE = "UNAVAILABLE"


class LocationSummary(BaseModel):
    """One item from `/{operator}/locations/map` bulk list."""

    model_config = ConfigDict(extra="ignore")

    id: str
    name: str
    address: str
    latitude: float
    longitude: float
    status: LocationStatus | None = None  # Primary network omits this


class ConnectorDetail(BaseModel):
    """Unified shape across operators. Different operators use different key
    names — we accept any and leave the rest as optional."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    # Evika/Battery-fly use `id`; Primary network uses `connectorId` — accept both.
    id: str | None = Field(default=None, validation_alias="id")
    connectorId: str | None = None
    status: str | None = None  # OCPP: Available/Preparing/Charging/Finishing/SuspendedEV/SuspendedEVSE/Faulted/Reserved/Unavailable
    availabilityStatus: str | None = None  # ACTIVE | ...
    typeEn: str | None = None
    typeRu: str | None = None
    maxPower: float | None = None
    rate: float | None = None
    codeByProtocol: int | None = None


class DeviceDetail(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    number: str | int | None = None
    status: str | None = None
    maxPower: float | None = None
    connectors: list[ConnectorDetail] = Field(default_factory=list)


class LocationDetail(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    name: str | None = None
    address: str | None = None  # Primary network omits it (uses street/house/city)
    latitude: float | None = None
    longitude: float | None = None
    description: str | None = None
    # Primary network provides these split-out fields instead of `address`.
    street: str | None = None
    house: str | None = None
    city: str | None = None
    devices: list[DeviceDetail] = Field(default_factory=list)

    def free_connector_types(self) -> list[str]:
        """Human-readable types of currently Available connectors."""
        seen: set[str] = set()
        out: list[str] = []
        for dev in self.devices:
            for c in dev.connectors:
                if c.status == "Available":
                    label = c.typeRu or c.typeEn or "?"
                    if label not in seen:
                        seen.add(label)
                        out.append(label)
        return out


class ConnectorType(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    nameRu: str
    nameEn: str
