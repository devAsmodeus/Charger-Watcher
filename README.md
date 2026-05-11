# Charger-Watcher

Telegram-бот, уведомляющий о свободных ЭЗС всех трёх сетей, агрегированных под [customer.example.com](https://customer.example.com/): **Primary network**, **Evika**, **Battery-fly**.

## Как это работает

Гибридная модель источников статуса:

| Сеть | Способ | Частота |
|---|---|---|
| Evika / Battery-fly | REST bulk `/{op}/locations/map` | раз в `POLL_INTERVAL_SEC` (10 c) |
| Primary network | SSE `/devices/status-stream?deviceNumber=N` | push real-time |

1. **Поллер** держит:
   - REST-дифф-воркер для Evika/Battery-fly: сравнивает предыдущее состояние с новым, пишет дельты в Redis Stream `charger:events`.
   - Catalog sync: раз в `CATALOG_SYNC_INTERVAL_SEC` подтягивает список локаций Primary network (для поиска/подписок).
   - SSE-manager: каждые `SSE_SYNC_INTERVAL_SEC` читает активные подписки из БД, открывает SSE на каждый `deviceNumber` подписанной Primary network-локации, закрывает стримы на бесхозные устройства. **Lazy**: нет подписок → нет SSE-коннектов.
2. **Бот** (aiogram 3) принимает команды + читает `charger:events`.
3. **Приоритет уведомлений**: paid — мгновенно; free — через `FREE_TIER_NOTIFY_DELAY_SEC` сек (по умолчанию 120).
4. **Лимиты подписок**: free — `FREE_TIER_MAX_SUBSCRIPTIONS`, paid — `PAID_TIER_MAX_SUBSCRIPTIONS`.
5. **Оплата**: Telegram Stars (`XTR`), без провайдера — `/upgrade`.

## Стек

- Python 3.12
- [aiogram 3](https://docs.aiogram.dev/) — Telegram bot
- [httpx](https://www.python-httpx.org/) — HTTP
- SQLAlchemy 2 async + asyncpg, Alembic для миграций
- PostgreSQL 16, Redis 7
- docker-compose для локального стека

## Требования

- **Сервер в Беларуси** (VPS в BY или прокси через BY-узел). API-шлюз провайдера отвечает только с BY-IP — извне таймаутит. Если поднимаешь не в РБ, укажи `HTTP_PROXY_URL` в `.env`.
- Python 3.12+, Docker, Docker Compose.
- Telegram-бот, созданный через [@BotFather](https://t.me/BotFather).

## Быстрый старт

```bash
git clone https://github.com/devAsmodeus/Charger-Watcher.git
cd Charger-Watcher
cp .env.example .env
# впиши TG_BOT_TOKEN; если сервер не в РБ — HTTP_PROXY_URL

docker compose up -d postgres redis
# один раз — миграции
docker compose run --rm poller alembic upgrade head
# запуск
docker compose up -d poller bot
docker compose logs -f poller bot
```

Локально без Docker:
```bash
python -m venv .venv && . .venv/Scripts/activate  # PowerShell: .venv\Scripts\Activate.ps1
pip install -e ".[dev]"
# подними PG+Redis любым способом, укажи DATABASE_URL / REDIS_URL в .env
alembic upgrade head
# src должен быть на PYTHONPATH (alembic уже знает через prepend_sys_path):
#   PowerShell: $env:PYTHONPATH = "src"
#   bash:       export PYTHONPATH=src
python -m poller.main   # терминал 1
python -m bot.main      # терминал 2
```

## Команды бота

- `/start` — регистрация и шпаргалка
- `/nearby` — запрашивает геолокацию, возвращает 10 ближайших станций с inline-кнопкой «🔔 Подписаться»
- `/find <запрос>` — поиск по названию/адресу, 10 совпадений с той же кнопкой
- `/list` — мои подписки
- `/unsubscribe <id>` — снять подписку
- `/status` — мой тариф и срок действия
- `/upgrade` — купить платный тариф (Telegram Stars)

## Переменные окружения

См. [.env.example](.env.example). Ключевое:

| Переменная | Значение |
|---|---|
| `TG_BOT_TOKEN` | Токен бота от @BotFather |
| `DATABASE_URL` | PG URL в формате asyncpg |
| `REDIS_URL` | Redis URL |
| `POLL_INTERVAL_SEC` | Интервал опроса bulk-статусов, сек (по умолчанию 10) |
| `HTTP_PROXY_URL` | Опциональный прокси к API (если хост не в РБ) |
| `FREE_TIER_NOTIFY_DELAY_SEC` | Задержка нотификации free-юзерам, сек |
| `FREE_TIER_MAX_SUBSCRIPTIONS` | Лимит подписок free |
| `PAID_TIER_MAX_SUBSCRIPTIONS` | Лимит подписок paid |

## Структура репо

```
Charger-Watcher/
├── src/
│   ├── api/                   # httpx-клиент + pydantic-модели ответов
│   ├── bot/                   # aiogram-бот + notifier consumer
│   ├── poller/                # долго живущий поллер статусов
│   ├── db/                    # SQLAlchemy-модели и сессия
│   ├── config.py              # pydantic-settings из .env
│   └── logging_setup.py
├── alembic/                   # миграции
├── docker-compose.yml
├── Dockerfile
└── pyproject.toml
```

## CI / Релиз / Деплой

От коммита до прода без ручных шагов на сервере — три GitHub Actions workflow-а:

1. **CI** (`.github/workflows/ci.yml`) — на каждый PR/push в `main`: `Lint` (ruff), `Test` (pytest), `Dependency audit` (pip-audit по `pyproject.toml`). Branch protection требует зелёные чеки, иначе мерж заблокирован.
2. **Release Please** (`.github/workflows/release-please.yml`) — на push в `main` парсит conventional-commits с момента последнего тега и открывает / обновляет PR `chore(main): release X.Y.Z` с бампом версии в `pyproject.toml` и автогенерированным `CHANGELOG.md`. После мержа этого PR создаёт git-тег `vX.Y.Z` и GitHub Release.
3. **Deploy** (`.github/workflows/deploy.yml`) — слушает `release.published` (и `workflow_dispatch` для ручного запуска). SSH на прод-сервер → `git checkout --detach <tag>` → `alembic upgrade head` → `docker compose up -d --build`. Detached HEAD на тег обходит расхождение server-local `main` с `origin/main`.

Ручной деплой любого ref-а:
```bash
gh workflow run deploy.yml -f ref=v0.1.0   # конкретный тег
gh workflow run deploy.yml -f ref=main     # текущий main
gh workflow run deploy.yml -f ref=<sha>    # коммит
```

### Conventional Commits — обязательно

| Префикс | Bump | В CHANGELOG |
|---|---|---|
| `feat: …` | minor | Features |
| `fix: …` | patch | Bug Fixes |
| `feat!: …` или `BREAKING CHANGE:` в теле | major | ⚠ Breaking |
| `chore:`, `docs:`, `refactor:`, `ci:`, `test:` | — | не попадает |

Если коммит не подходит под префикс, release-please его проигнорит — для технического долга это норма, для фичи это баг (не попадёт в релиз).

### Required repository secrets

| Secret | Назначение |
|---|---|
| `RELEASE_PLEASE_TOKEN` | fine-grained PAT (`contents: write`, `pull-requests: write`). Без него релиз-PR создаётся от `GITHUB_TOKEN`, на который CI не запускается → встанет колом под branch protection |
| `DEPLOY_HOST` / `DEPLOY_USER` / `DEPLOY_SSH_KEY` | SSH-доступ к прод-серверу. Ключ — **отдельный deploy-key**, не личный SSH-ключ |

## Roadmap

- [x] MVP: Evika + Battery-fly bulk-мониторинг, подписки на локации
- [x] Primary network через публичный SSE `/devices/status-stream` (lazy per-device subs)
- [x] Геопоиск `/nearby` (bounding-box + haversine, сортировка по расстоянию)
- [x] Платный тариф через Telegram Stars (`/upgrade`, `/status`)
- [x] Дедуп + cooldown через `notification_log`
- [x] Отмена delayed-уведомлений free-юзерам, если локация снова стала не-AVAILABLE
- [x] Auto-downgrade paid → free по истечении `paid_until`
- [x] Rate-limit Telegram (`aiolimiter`) против Telegram flood
- [x] Отписка inline-кнопкой прямо из уведомления
- [x] `/privacy` и `/delete_me`
- [x] SSE backoff с экспоненциальной реконнект-стратегией
- [x] Фильтр по типу коннектора в подписке (через subscribe-wizard)
- [x] Информативные уведомления — типы свободных коннекторов в пуше
- [x] CI + автоматический релиз и деплой на тег
- [ ] Адресный поиск через Nominatim (`/nearby <адрес>`)
- [ ] Webhook-режим бота и тесты (pytest + respx)

## Лицензия

MIT
