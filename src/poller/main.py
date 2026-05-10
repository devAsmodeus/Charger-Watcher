from __future__ import annotations

import asyncio
import signal
import time
from collections.abc import Iterable

import orjson
import redis.asyncio as aioredis
import structlog
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from api.client import (
    FREE_OCPP_STATUSES,
    SUPPORTED_OPERATORS_MVP,
    ProviderClient,
)
from api.models import LocationDetail, LocationStatus, LocationSummary, Operator
from config import get_settings
from db.models import Location, Subscription
from db.session import SessionLocal
from logging_setup import setup_logging

log = structlog.get_logger(__name__)

EVENTS_STREAM = "charger:events"

# Redis keys for restart-safe state. Without these, the first poll/SSE
# heartbeat after a restart is treated as the baseline and any transition
# that happened during downtime is silently lost — a missed notification.
REST_PREV_HASH = "poller:rest:prev"  # field "<operator>:<external_id>" -> status
SSE_FREE_HASH = "poller:sse:last_any_free"  # field "<device_number>" -> "1" / "0"

# Catalog of connector types per location (статика — типы железа). Bot
# читает этот hash в wizard'е подписки чтобы построить keyboard выбора.
# Field = "<location_id>", value = JSON list[str] (typeRu/typeEn).
LOCATION_CONNECTORS_HASH = "location_connectors"


def _all_connector_types(detail: LocationDetail) -> list[str]:
    """Уникальные человеко-читаемые типы коннекторов на локации."""
    seen: set[str] = set()
    out: list[str] = []
    for d in detail.devices:
        for c in d.connectors:
            label = c.typeRu or c.typeEn
            if label and label not in seen:
                seen.add(label)
                out.append(label)
    return out


# ---------- location catalog ----------

async def upsert_locations(operator: Operator, items: list[LocationSummary]) -> dict[str, int]:
    """Upsert rows, return mapping external_id -> internal location id."""
    if not items:
        return {}
    rows = [
        {
            "operator": operator.value,
            "external_id": item.id,
            "name": item.name,
            "address": item.address,
            "latitude": item.latitude,
            "longitude": item.longitude,
            "last_status": item.status.value if item.status else None,
        }
        for item in items
    ]
    async with SessionLocal() as s:
        stmt = pg_insert(Location).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=["operator", "external_id"],
            set_={
                "name": stmt.excluded.name,
                "address": stmt.excluded.address,
                "latitude": stmt.excluded.latitude,
                "longitude": stmt.excluded.longitude,
                "last_status": stmt.excluded.last_status,
                "last_seen_at": func.now(),
            },
        ).returning(Location.external_id, Location.id)
        result = await s.execute(stmt)
        mapping = {row.external_id: row.id for row in result}
        await s.commit()
    return mapping


async def sync_central_catalog(client: ProviderClient) -> None:
    """Fetch Network A location list and upsert into DB.

    Status is not provided by this endpoint — we only cache coordinates/names
    so users can /find and /subscribe to Network A points. Real status
    for subscribed locations is driven by the SSE manager.
    """
    try:
        items = await client.list_locations(Operator.MAIN)
    except Exception as e:
        log.warning("central_catalog_failed", err=str(e))
        return
    await upsert_locations(Operator.MAIN, items)
    log.info("central_catalog_synced", count=len(items))


# ---------- redis events ----------

async def publish_event(redis: aioredis.Redis, payload: dict) -> None:
    await redis.xadd(EVENTS_STREAM, {"data": orjson.dumps(payload).decode()}, maxlen=10_000)


# ---------- REST diff loop (Evika / Battery-fly) ----------

def _prev_field(op: Operator, ext_id: str) -> str:
    return f"{op.value}:{ext_id}"


async def load_rest_prev(
    redis: aioredis.Redis,
) -> dict[tuple[Operator, str], LocationStatus]:
    """Hydrate REST diff state from Redis.

    Without this, after a restart the first poll has empty ``prev``, so
    every transition that happened during downtime is silently lost. We
    persist the last observed status per (operator, external_id) and
    re-load it on boot so the very first diff still detects transitions
    that occurred while the poller was down.
    """
    raw = await redis.hgetall(REST_PREV_HASH)
    out: dict[tuple[Operator, str], LocationStatus] = {}
    valid_ops = {o.value: o for o in Operator}
    for field, value in raw.items():
        if not isinstance(field, str) or ":" not in field:
            continue
        op_str, _, ext_id = field.partition(":")
        op = valid_ops.get(op_str)
        if op is None or not ext_id:
            continue
        try:
            status = LocationStatus(value)
        except ValueError:
            continue
        out[(op, ext_id)] = status
    if out:
        log.info("rest_prev_loaded", entries=len(out))
    return out


async def save_rest_prev(
    redis: aioredis.Redis,
    new_state: dict[tuple[Operator, str], LocationStatus],
    old_state: dict[tuple[Operator, str], LocationStatus],
) -> None:
    """Persist current REST diff state to Redis.

    Writes only what changed and removes keys that disappeared from the
    upstream listing so the hash doesn't grow unbounded.
    """
    pipe = redis.pipeline()
    touched = False
    for key, status in new_state.items():
        if old_state.get(key) is status:
            continue
        op, ext_id = key
        await pipe.hset(REST_PREV_HASH, _prev_field(op, ext_id), status.value)
        touched = True
    # remove fields for locations that disappeared from upstream
    removed = [k for k in old_state if k not in new_state]
    for op, ext_id in removed:
        await pipe.hdel(REST_PREV_HASH, _prev_field(op, ext_id))
        touched = True
    if touched:
        await pipe.execute()


async def _free_types_on_transition(
    client: ProviderClient, op: Operator, external_id: str
) -> list[str]:
    """Дёрнуть detail и вернуть человеко-читаемые типы свободных коннекторов.

    Вызывается ТОЛЬКО на transition AVAILABLE — не на каждом поллинге, а
    только при detection факта освобождения. Стоимость ~один HTTP-вызов
    на transition; при типичных нагрузках это десятки/час, не тысячи.
    """
    try:
        detail = await client.get_location_detail(op, external_id)
    except Exception as e:  # noqa: BLE001
        log.warning(
            "rest_detail_failed_on_transition",
            op=op.value,
            ext=external_id,
            err=str(e),
        )
        return []
    return detail.free_connector_types()


async def rest_diff_once(
    client: ProviderClient,
    redis: aioredis.Redis,
    prev: dict[tuple[Operator, str], LocationStatus],
) -> dict[tuple[Operator, str], LocationStatus]:
    now = int(time.time())
    new_state: dict[tuple[Operator, str], LocationStatus] = {}

    for op in SUPPORTED_OPERATORS_MVP:
        try:
            items = await client.list_locations(op)
        except Exception as e:
            log.warning("list_locations_failed", operator=op.value, err=str(e))
            continue

        ext_to_id = await upsert_locations(op, items)

        for item in items:
            if item.status is None:
                continue
            key = (op, item.id)
            new_state[key] = item.status

            old = prev.get(key)
            if old is None or old == item.status:
                continue

            location_id = ext_to_id.get(item.id)
            if location_id is None:
                continue

            # Transition AVAILABLE — выясняем какие именно типы коннекторов
            # сейчас свободны, чтобы notifier мог отфильтровать подписки
            # по connector_type. Для FULLY_USED/UNAVAILABLE список не нужен.
            free_types: list[str] = []
            if item.status is LocationStatus.AVAILABLE:
                free_types = await _free_types_on_transition(client, op, item.id)

            await publish_event(
                redis,
                {
                    "ts": now,
                    "operator": op.value,
                    "location_id": location_id,
                    "external_id": item.id,
                    "from_status": old.value,
                    "to_status": item.status.value,
                    "became_available": item.status is LocationStatus.AVAILABLE,
                    "free_connector_types": free_types,
                    "name": item.name,
                    "address": item.address,
                    "lat": item.latitude,
                    "lon": item.longitude,
                },
            )
            log.info(
                "status_change",
                operator=op.value,
                ext=item.id,
                name=item.name,
                from_=old.value,
                to=item.status.value,
            )

    # Persist for restart-safety. Done in one shot at end of the cycle so
    # we're not paying a roundtrip per location.
    await save_rest_prev(redis, new_state, prev)
    return new_state


# ---------- SSE manager for Network A ----------

async def _get_central_device_info(
    client: ProviderClient, external_id: str
) -> dict[int, dict[int, str]]:
    """Discover devices on a Network A location and their connector maps.

    Returns: device_number -> {codeByProtocol -> human-readable typeRu/typeEn}.
    SSE-фрейм даёт только ``codeByProtocol``; чтобы в payload event'а класть
    нормальные русские названия типов коннекторов (для фильтра подписок),
    нам нужен этот map. Получаем его одним detail-вызовом на старте
    SSE worker'а.
    """
    try:
        detail = await client.get_location_detail(Operator.MAIN, external_id)
    except Exception as e:
        log.warning("central_detail_failed", ext=external_id, err=str(e))
        return {}
    out: dict[int, dict[int, str]] = {}
    for d in detail.devices:
        try:
            dn = int(d.number)  # type: ignore[arg-type]
        except Exception:
            continue
        cmap: dict[int, str] = {}
        for c in d.connectors:
            if c.codeByProtocol is None:
                continue
            label = c.typeRu or c.typeEn
            if label:
                cmap[c.codeByProtocol] = label
        out[dn] = cmap
    return out


async def _sse_worker(
    client: ProviderClient,
    redis: aioredis.Redis,
    device_number: int,
    location_id: int,
    external_id: str,
    name: str,
    address: str,
    lat: float,
    lon: float,
    connector_map: dict[int, str],
) -> None:
    """Long-lived task: consume SSE, detect 'any connector Available' transitions.

    The remote emits a heartbeat every ~2s with the current state of every
    connector on this device — we diff locally.

    ``connector_map`` (codeByProtocol -> typeRu) used to populate
    ``free_connector_types`` in the published event so notifier can filter
    by user's connector preference.
    """
    log.info("sse_open", device=device_number, ext=external_id)
    # per-connector last seen OCPP status
    last: dict[int, str] = {}
    # derived: is *any* connector of this device currently Available.
    # Bootstrap from Redis so a station that was already free at restart
    # still fires when the next heartbeat differs from the persisted state.
    last_any_free: bool | None = None
    persisted = await redis.hget(SSE_FREE_HASH, str(device_number))
    if persisted == "1":
        last_any_free = True
    elif persisted == "0":
        last_any_free = False
    backoff = 1.0  # exponential reconnect backoff (seconds), cap at 60

    while True:
        try:
            async for frame in client.stream_device_status(device_number):
                backoff = 1.0  # connection healthy — reset backoff
                status = frame.get("status")
                code = frame.get("codeByProtocol")
                if not isinstance(status, str) or not isinstance(code, int):
                    continue
                last[code] = status
                any_free = any(s in FREE_OCPP_STATUSES for s in last.values())

                if last_any_free is None:
                    last_any_free = any_free
                    # Seed Redis on first frame so that across restarts we
                    # always have a baseline to diff against.
                    await redis.hset(
                        SSE_FREE_HASH, str(device_number), "1" if any_free else "0"
                    )
                    continue
                if any_free == last_any_free:
                    continue

                last_any_free = any_free
                # Persist the new baseline so a restart between this frame
                # and the next one does not lose the transition signal.
                await redis.hset(
                    SSE_FREE_HASH, str(device_number), "1" if any_free else "0"
                )
                if any_free:
                    # location transitioned to having at least one free connector
                    now = int(time.time())
                    free_types: list[str] = []
                    seen_types: set[str] = set()
                    for c, st in last.items():
                        if st not in FREE_OCPP_STATUSES:
                            continue
                        label = connector_map.get(c)
                        if label and label not in seen_types:
                            seen_types.add(label)
                            free_types.append(label)
                    await publish_event(
                        redis,
                        {
                            "ts": now,
                            "operator": Operator.MAIN.value,
                            "location_id": location_id,
                            "external_id": external_id,
                            "from_status": "FULLY_USED",
                            "to_status": "AVAILABLE",
                            "became_available": True,
                            "free_connector_types": free_types,
                            "name": name,
                            "address": address,
                            "lat": lat,
                            "lon": lon,
                            "device_number": device_number,
                        },
                    )
                    log.info(
                        "sse_available",
                        device=device_number,
                        ext=external_id,
                        connector=code,
                    )
        except asyncio.CancelledError:
            log.info("sse_cancelled", device=device_number)
            raise
        except Exception as e:  # noqa: BLE001
            log.warning(
                "sse_worker_error",
                device=device_number,
                err=str(e),
                sleep=round(backoff, 1),
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)


async def _subscribed_central_locations() -> list[Location]:
    """Locations of Network A that are targeted by at least one live subscription."""
    async with SessionLocal() as s:
        rows = (
            await s.execute(
                select(Location)
                .join(Subscription, Subscription.location_id == Location.id)
                .where(Location.operator == Operator.MAIN.value)
                .distinct()
            )
        ).scalars().all()
    return list(rows)


class SseManager:
    """Reconciles live SSE workers against subscribed Network A locations.

    Every `sse_sync_interval_sec` it:
      - loads the set of subscribed locations (from DB)
      - ensures each location's devices have a running SSE worker
      - cancels workers for devices no longer referenced by any subscription
    """

    def __init__(self, client: ProviderClient, redis: aioredis.Redis) -> None:
        self._client = client
        self._redis = redis
        # device_number -> SSE worker task
        self._workers: dict[int, asyncio.Task[None]] = {}
        # location_id -> {device_number -> {codeByProtocol -> typeRu}}
        # Кэш статичных метаданных устройства/коннекторов: получаем один раз
        # detail-вызовом, переиспользуем во всех последующих ticks.
        self._loc_devices: dict[int, dict[int, dict[int, str]]] = {}

    async def tick(self) -> None:
        locs = await _subscribed_central_locations()
        # device_number -> (Location row, connector_map)
        wanted: dict[int, tuple[Location, dict[int, str]]] = {}

        for loc in locs:
            if loc.id not in self._loc_devices:
                self._loc_devices[loc.id] = await _get_central_device_info(
                    self._client, loc.external_id
                )
            for dn, cmap in self._loc_devices[loc.id].items():
                wanted[dn] = (loc, cmap)

        # open new workers
        for dn, (loc, cmap) in wanted.items():
            if dn in self._workers and not self._workers[dn].done():
                continue
            task = asyncio.create_task(
                _sse_worker(
                    self._client,
                    self._redis,
                    dn,
                    loc.id,
                    loc.external_id,
                    loc.name,
                    loc.address,
                    loc.latitude,
                    loc.longitude,
                    cmap,
                ),
                name=f"sse:{dn}",
            )
            self._workers[dn] = task

        # cancel stale workers
        stale = [dn for dn in self._workers if dn not in wanted]
        for dn in stale:
            self._workers[dn].cancel()
            self._workers.pop(dn, None)

        if stale or wanted:
            log.debug("sse_tick", active=len(self._workers), wanted=len(wanted), stale=len(stale))

    async def close(self) -> None:
        for t in self._workers.values():
            t.cancel()
        await asyncio.gather(*self._workers.values(), return_exceptions=True)
        self._workers.clear()


# ---------- connectors catalog (для wizard'а bot-а) ----------

async def connectors_catalog_tick(
    client: ProviderClient, redis: aioredis.Redis
) -> None:
    """Раз в N часов обходим все известные локации и сохраняем в Redis
    список их типов коннекторов. Bot читает этот кэш в wizard'е подписки.

    Стоимость: ~1 detail-вызов на локацию × N локаций. Под пару тысяч
    локаций и pacing 0.1с между запросами — около 3-4 минут на проход,
    лежит на грани приемлемой нагрузки на upstream API. Если каталог
    вырастет, поднять ``connectors_sync_interval_sec`` или добавить
    bulk-эндпоинт.
    """
    async with SessionLocal() as s:
        rows = (await s.execute(select(Location))).scalars().all()
    if not rows:
        return
    written = 0
    pipe = redis.pipeline()
    for loc in rows:
        try:
            op = Operator(loc.operator)
        except ValueError:
            continue
        try:
            detail = await client.get_location_detail(op, loc.external_id)
        except Exception as e:  # noqa: BLE001
            log.warning(
                "connectors_detail_failed",
                op=loc.operator,
                ext=loc.external_id,
                err=str(e),
            )
            continue
        types = _all_connector_types(detail)
        if types:
            await pipe.hset(
                LOCATION_CONNECTORS_HASH,
                str(loc.id),
                orjson.dumps(types).decode(),
            )
            written += 1
        # Gentle pacing: provider — public API, не злоупотребляем.
        await asyncio.sleep(0.1)
    if written:
        await pipe.execute()
    log.info("connectors_catalog_synced", written=written, total=len(rows))


# ---------- runner ----------

async def _periodic(stop: asyncio.Event, interval: float, fn) -> None:
    """Run coroutine `fn` every `interval` sec until `stop` is set."""
    while not stop.is_set():
        t0 = time.perf_counter()
        try:
            await fn()
        except Exception as e:
            log.exception("periodic_failed", err=str(e))
        dt = time.perf_counter() - t0
        sleep_for = max(0.0, interval - dt)
        try:
            await asyncio.wait_for(stop.wait(), timeout=sleep_for)
        except TimeoutError:
            pass


async def _runner() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)
    log.info(
        "poller_start",
        interval_sec=settings.poll_interval_sec,
        operators=[o.value for o in SUPPORTED_OPERATORS_MVP],
    )

    stop = asyncio.Event()

    def _handle_sig(*_: object) -> None:
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_sig)
        except NotImplementedError:
            pass

    redis = aioredis.from_url(settings.redis_url, decode_responses=True)

    async with ProviderClient(
        settings.api_base,
        timeout=settings.http_timeout_sec,
        proxy_url=settings.http_proxy_url,
        origin=settings.api_origin or None,
        user_agent=settings.api_user_agent,
    ) as client:
        sse_mgr = SseManager(client, redis)
        # Restore last-known REST state from Redis so the very first diff
        # after a restart can still detect transitions that happened
        # while the poller was down.
        prev: dict[tuple[Operator, str], LocationStatus] = await load_rest_prev(redis)

        async def rest_tick() -> None:
            nonlocal prev
            prev = await rest_diff_once(client, redis, prev)

        async def catalog_tick() -> None:
            await sync_central_catalog(client)

        async def sse_tick() -> None:
            await sse_mgr.tick()

        async def connectors_tick() -> None:
            await connectors_catalog_tick(client, redis)

        # prime catalog once at start (including connectors so wizard works
        # immediately on boot, не ждёт первого 6-часового тика).
        await catalog_tick()
        await connectors_tick()

        tasks = [
            asyncio.create_task(_periodic(stop, settings.poll_interval_sec, rest_tick)),
            asyncio.create_task(_periodic(stop, settings.catalog_sync_interval_sec, catalog_tick)),
            asyncio.create_task(_periodic(stop, settings.sse_sync_interval_sec, sse_tick)),
            asyncio.create_task(
                _periodic(stop, settings.connectors_sync_interval_sec, connectors_tick)
            ),
        ]
        await stop.wait()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await sse_mgr.close()

    await redis.aclose()
    log.info("poller_stopped")


def run() -> None:
    asyncio.run(_runner())


if __name__ == "__main__":
    run()
