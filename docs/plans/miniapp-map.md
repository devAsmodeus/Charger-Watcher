# Mini-app «Карта ЭЗС» — план реализации

> Статус: **draft, до начала кода**. План синтезирован из 3 параллельных
> ресёрч-агентов (frontend / backend / deploy). Принятые решения отмечены
> ✓; вопросы, требующие ответа до старта — в секции «Открытые вопросы».

## 1. Цель и скоуп

Telegram Mini-app, открывается из меню бота `@chargerwatcherbot`. Показывает
все известные ЭЗС Беларуси на карте с цветовой индикацией статуса.

### Версии

**v1 (MVP)** — read-only карта:
- Все станции как маркеры (`🟢 AVAILABLE` / `🔴 FULLY_USED` / `⚪ UNAVAILABLE`).
- Кластеризация в зуме «вся БРБ»; на близких зумах — отдельные точки.
- Тап на маркер → попап: название, адрес, сеть, число коннекторов,
  «last seen at» статуса.
- Адаптация под тему Telegram (тёмная/светлая).
- Загрузка по viewport bbox (не «все 500+ за раз»).

**v2** — действия из карты:
- Кнопка «Подписаться» в попапе, шлёт `Telegram.WebApp.sendData` → бот
  открывает обычный wizard (выбор коннектора, лимит).
- Список «мои подписки» — отдельный layer, выделяет уже подписанные.

**v3 (опционально, не сейчас)**:
- Build-route в стороннее приложение (Я.Карты / Google).
- Filter по типу коннектора (CCS / CHAdeMO / Type2).
- Push-уведомление об открытии WebApp при доставке.

## 2. Архитектура — высокоуровневая

```
                  Internet :443
                       │
                       ▼
                 ┌──────────┐
                 │  Caddy   │ TLS termination, Let's Encrypt auto
                 └────┬─────┘
                      │ http://web:8080
                      ▼
              ┌────────────────┐
              │  web (FastAPI) │ — статика /  +  /api/*
              └───┬────────┬───┘
                  │        │
            ┌─────▼──┐  ┌──▼──────┐
            │postgres│  │ redis   │
            └────────┘  └─────────┘

   ┌────────┐   ┌──────────────────────────────┐
   │  bot   │──►│  gluetun (FRA exit)          │──► api.telegram.org
   └────────┘   └──────────────────────────────┘

   ┌────────┐   direct RU IP
   │ poller │──►──────────────────────────────►──► apigateway.malankabn.by
   └────────┘
```

**Ключевое решение ✓**: новый сервис `web` живёт на default-bridge, **не внутри**
`network_mode: service:gluetun`. Gluetun по умолчанию блокирует ingress, а
Mini-app должен принимать входящий HTTPS из любых клиентских сетей
Telegram. Бот по-прежнему ходит наружу через gluetun — это не меняется.

## 3. Frontend

### Stack ✓

- **MapLibre GL JS** (BSD-3, WebGL).
  - Тайлы: **MapTiler «Streets v2» + «Streets Dark v2»** на free-tier
    (100k tile loads/мес). Fallback при росте трафика — self-hosted
    `tileserver-gl` на этом же VPS, тот же MapLibre-код, меняется только
    URL стиля.
  - **Не использовать**: дефолтный `tile.openstreetmap.org` (запрещён ToS
    для production), Yandex Maps (proprietary, обязательное брендирование,
    лимит 25k загрузок/день).
- **Vite + vanilla TypeScript, без UI-фреймворка**.
  - Один экран, один canvas — React/Preact ничего не дают.
  - Production bundle: ~80 KB app code + ~220 KB MapLibre = ~300 KB gzip.
- **TG WebApp SDK 8.x — минимальный surface для v1**:
  - `initData` / `initDataUnsafe` — auth payload в бэк.
  - `ready()` + `expand()` — без этого окно открывается на пол-экрана.
  - `themeParams` + `themeChanged` event — синхронизация цветов.
  - `viewport_height` / `viewportChanged` — карта `resize()` при
    появлении on-screen-клавы.
  - `BackButton` — показываем при открытом попапе, прячем на голой карте.
  - `HapticFeedback.impactOccurred('light')` на тап маркера.
- Скип в v1: `MainButton`, `sendData`, `CloudStorage`, `closingConfirmation`.
  `sendData` приходит в v2 (subscribe-из-карты).

### Theme integration

`themeParams` → CSS custom properties на `<html>`, перерисовка карты по
событию `themeChanged`:

```js
const tg = window.Telegram.WebApp;
function applyTheme() {
  const r = document.documentElement.style;
  for (const [k, v] of Object.entries(tg.themeParams))
    r.setProperty(`--tg-${k.replace(/_/g, '-')}`, v);
  map.setStyle(tg.colorScheme === 'dark' ? STYLE_DARK : STYLE_LIGHT);
}
tg.onEvent('themeChanged', applyTheme);
applyTheme();
```

Кнопки/попапы используют `var(--tg-bg-color)`, `var(--tg-text-color)`,
`var(--tg-button-color)`, `var(--tg-hint-color)`. Без фреймворка.

## 4. Backend

### Stack ✓

- **FastAPI** (pydantic v2 уже в зависимостях, Starlette под капотом, нативный
  asyncpg-стиль через `Depends`). +~2 МБ wheel-ов — приемлемо.
- Не aiohttp (ergonomics проигрывают), не litestar (бус-фактор), не raw
  Starlette (терять DI на initData — больно).

### Сервис

Отдельный compose-сервис `web`, тот же image (тот же `Dockerfile`,
точкой входа `python -m api.main` или uvicorn). Live на default bridge,
с `depends_on: postgres, redis` (healthy). Получает `TG_BOT_TOKEN` из
`.env` для верификации `initData`.

### initData HMAC validation ✓

Обязательный код — если облажаемся, любой может дёргать API от чужого
имени. Полный вариант:

```python
import hmac, hashlib, json, time
from urllib.parse import parse_qsl


class InitDataError(Exception):
    pass


def verify_init_data(init_data: str, bot_token: str, max_age_sec: int = 3600) -> dict:
    pairs = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=True))
    received_hash = pairs.pop("hash", None)
    if not received_hash:
        raise InitDataError("missing hash")
    data_check_string = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    computed = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(computed, received_hash):
        raise InitDataError("bad signature")
    try:
        auth_date = int(pairs.get("auth_date", "0"))
    except ValueError:
        raise InitDataError("bad auth_date")
    if auth_date <= 0 or time.time() - auth_date > max_age_sec:
        raise InitDataError("stale initData")
    user_raw = pairs.get("user")
    if not user_raw:
        raise InitDataError("missing user")
    return json.loads(user_raw)  # {id, first_name, username, language_code, ...}
```

`max_age_sec=3600` (1 час) — Telegram дефолт 24h слишком расслаблен для
data-API. Подключаем как `Depends`, читаем `Authorization: tma <initData>`.

### v1 API spec

`GET /api/stations?bbox=south,west,north,east[&status=AVAILABLE,FULLY_USED][&limit=500]`

- `bbox` обязателен, формат `lat1,lon1,lat2,lon2`. Reject если
  `north <= south`, `east <= west`, или площадь > ~5° (анти-«отдай всё»).
- `status` опционально, CSV из `{AVAILABLE, FULLY_USED, UNAVAILABLE}`.
- `limit` — default 500, hard cap 1000.

Response (200):

```ts
type StationsResponse = {
  stations: Array<{
    id: number;
    name: string;
    address: string;
    operator: string;
    lat: number;
    lon: number;
    status: "AVAILABLE" | "FULLY_USED" | "UNAVAILABLE" | null;
    last_status_at: string;  // ISO-8601
  }>;
  truncated: boolean;
};
```

Error envelope:

```ts
type ErrorResponse = { error: { code: string; message: string } };
```

Коды: `400 bad_bbox | bad_status | bbox_too_large`, `401 invalid_init_data | stale_init_data`,
`429 rate_limited`, `503 db_unavailable`.

SQL — на существующем `ix_location_geo` (composite lat/lon), миграции не
нужны.

### v2 — endpoints для subscribe-из-карты

(всё за `verify_init_data`, реализуем во второй PR-серии)

- `POST /api/subscriptions {location_id, connector_type?, notify_limit?}` → 201
  - 409 `tier_limit_reached` | `already_subscribed`
- `GET /api/subscriptions/me` → список + join `Location` для отображения
- `DELETE /api/subscriptions/{id}` → 204 (404 если не владелец)
- `GET /api/stations/{id}/connectors` → читает Redis hash
  `location_connectors`, тот же, что bot wizard

**Важно**: перед v2 — рефактор логики подписки из `bot/main.py` (handlers
`on_wlim_callback` и др.) в `src/services/subscriptions.py`. Эта же логика
будет дёргаться из HTTP-хэндлеров. Без рефактора v2 = code-duplication, и
любой fix tier-лимита придётся дублировать.

### Caching & rate-limit

- Cache `GET /api/stations` в Redis под ключом
  `stations:bbox:<rounded_bbox>:<status_csv>`, TTL = **15s** (≈
  `sse_sync_interval_sec=15`). Координаты bbox округлять до 3 знаков
  (~110 м), иначе pan/zoom миссит кэш на каждый пиксель.
- Не кэшируем при `limit > 500` или нестандартном `status` фильтре.
- Rate-limit: **30 req / 10 sec, burst 10** per `tg_id` из initData (НЕ
  по IP — Telegram WebView шарит NAT). Реализация — `aiolimiter` (уже
  есть в deps) с key = `tg_id`.

## 5. Deployment

### Hosting ✓

Статика **отдаётся из того же FastAPI**, без отдельного nginx-контейнера
для static:

- Bundle ~300 KB — CDN-edge даёт <30 ms выигрыша, при том что `/api/stations`
  всё равно ходит в наш VPS (100-300 ms). Не стоит.
- Нет CORS (origin тот же), нет split-deploy desync, нет двух Dockerfile'ов.

Caddy впереди — терминирует TLS, проксирует всё в `web:8080`.

### TLS & domain ✓

- **Домен**: нужен реальный публичный (`app.chargerwatcher.app` /
  `app.chargerwatcher.by` / любой свой). Telegram WebApp **отказывается**
  работать с self-signed и duckdns-подобными. `.by` требует резидентский
  паспорт; `.app` / `.com` — инстант. ~$10/год.
- **TLS**: **Caddy auto-TLS** (Let's Encrypt HTTP-01). Один бинарь,
  4-строчный Caddyfile, авто-продление. Без cron, без certbot.
- DNS: A-запись `<subdomain> → 178.72.178.9`, TTL 300.

### docker-compose — новые сервисы

```yaml
  web:
    build: .
    restart: unless-stopped
    command: uvicorn api.main:app --host 0.0.0.0 --port 8080
    env_file: .env
    environment:
      - TG_BOT_TOKEN=${TG_BOT_TOKEN}
      - MINIAPP_STATIC_DIR=/app/static
    # порты НЕ публикуем — caddy ходит по docker-bridge
    depends_on:
      postgres: { condition: service_healthy }
      redis:    { condition: service_healthy }

  caddy:
    image: caddy:2-alpine
    restart: unless-stopped
    ports:
      - "0.0.0.0:80:80"   # ACME HTTP-01 + 80→443 redirect
      - "0.0.0.0:443:443"
    volumes:
      - ./caddy/Caddyfile:/etc/caddy/Caddyfile:ro
      - caddy_data:/data
      - caddy_config:/config
    depends_on: [web]

volumes:
  pg_data:
  caddy_data:
  caddy_config:
```

Caddyfile (4 строки):

```
app.chargerwatcher.app {
    encode zstd gzip
    reverse_proxy web:8080
}
```

Правило CLAUDE.md «биндить только на 127.0.0.1» соблюдено для всего, кроме
:80/:443 — они и обязаны быть публичными, в этом смысл reverse-proxy.

### Multi-stage Dockerfile ✓

Фронт билдится **внутри того же Dockerfile**, артефакт копируется в
Python-образ. Без коммита `dist/` в git:

```dockerfile
FROM node:20-alpine AS frontend
WORKDIR /build
COPY miniapp/package*.json ./
RUN npm ci
COPY miniapp/ ./
RUN npm run build   # → /build/dist

FROM python:3.14-slim
# ... существующий python setup ...
COPY --from=frontend /build/dist /app/static
```

**Риск**: npm-registry с RU-IP иногда тротлится. Если `npm ci` начнёт
падать — переключаемся на `npm_config_registry=https://registry.npmmirror.com`,
крайний вариант — билд фронта в GH Actions с rsync `dist/` на сервер.
Default — build-в-Dockerfile, проверим эмпирически.

## 6. CI/CD

Существующий цикл (CLAUDE.md):

```
git pull && docker compose build <service> && docker compose up -d
```

Не меняется концептуально. Просто:

```
git pull
docker compose build web caddy   # web — пересобирает и фронт
docker compose up -d web caddy
docker compose logs --tail=30 web caddy
```

Frontend пересобирается прозрачно (multi-stage), артефакт оседает в
`/app/static` внутри `web` контейнера.

## 7. BotFather регистрация

В `@BotFather`:

1. `/mybots` → выбрать `@chargerwatcherbot` → **Bot Settings** →
   **Menu Button** → **Configure menu button** → URL
   `https://app.chargerwatcher.app/` → label `🗺 Карта`.
2. (опционально, для t.me/...-ссылок) `/newapp` → выбрать бота → title,
   short description, иконка 512×512 PNG → Web App URL та же →
   short-name `map`.
3. `setMyDescription` / `setMyShortDescription` — пока через BotFather
   (как договорились), позже можно перевести на API.

## 8. Поэтапный rollout (v1)

| Шаг | Артефакт | Кто блокирует |
|-----|----------|---------------|
| 8.1 | Зарегистрировать домен + DNS A-запись | юзер (выбор домена) |
| 8.2 | `src/api/` — FastAPI приложение, `verify_init_data`, `/api/stations` | — |
| 8.3 | `miniapp/` — Vite-проект, MapLibre, тема, popup | MapTiler key |
| 8.4 | Multi-stage Dockerfile + compose-сервисы `web` + `caddy` | домен |
| 8.5 | Caddyfile + deploy на сервер | open :80/:443 у провайдера |
| 8.6 | BotFather → menu button → URL | домен |
| 8.7 | Smoke: открыть Mini-app из бота, увидеть карту, тапнуть маркер | — |

Параллельно сейчас идёт Phase 2 проекта (quiet hours, refund, рефералка).
Mini-app не зависит от них, кроме общего рефактора `services/` под v2.

## 9. Открытые вопросы

1. **Домен** — какой адрес купить / использовать? Без этого вся секция 5
   стоит. `.app` от Google ($14/год) или используем уже зарегистрированный?
2. **MapTiler аккаунт** — кто владеет, на чей email? Key должен быть
   domain-restricted на адрес Mini-app, иначе анти-абуз тривиально бьётся.
3. **VPS firewall на :80/:443** — открыты ли на уровне провайдера, не
   только iptables? Некоторые RU-VPS блокируют :80 by default, тогда
   ACME HTTP-01 не сработает → fallback на DNS-01 (Caddy умеет, но нужен
   API-токен регистратора).
4. **i18n** — RU-only для v1 или сразу EN-fallback? Решили «нет EN/BE»
   в основном боте — наследуем сюда.
5. **Иконки операторов** — есть brand-safe glyphs для Маланки / Evika /
   Battery-fly, или нейтральный pin с status-color? Юридически
   проще нейтральный — никаких знаков чужого бренда (см. `docs/legal/`).
6. **Аналитика** — логировать ли открытия Mini-app и тапы маркеров?
   Если да — endpoint `/api/app/event` + новая таблица `app_events`. Для
   v1 можно отложить.
7. **Connector catalog для v2** — берём из Redis hash `location_connectors`
   (как сейчас bot wizard) или экспонируем через отдельный endpoint?
8. **Mini-app session lifetime** — `max_age_sec=3600` на initData означает
   401 после часа panning. Принимаем (юзер reopens) или строим
   short-lived JWT? v1 — принять, v2 — JWT.
