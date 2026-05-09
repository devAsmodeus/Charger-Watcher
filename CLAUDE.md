# Charger-Watcher project rules

Дополняет глобальные принципы Karpathy из `~/.claude/CLAUDE.md`.

## Приоритет правил

- Проектные правила (сеть, деплой, file-scope, "не делать") — жёсткие.
  Конфликт с Karpathy Skills решается в их пользу.
- Karpathy Skills — стиль работы по умолчанию для всех правок кода.
- "Известный технический долг" — это TODO, а не dead code.
  Surgical Changes не даёт права его удалять.

## Что это и для кого

Telegram-бот `@chargerwatcherbot`. UVP — одна фича: пуш, когда зарядка рядом
с пользователем (или у его сохранённой локации) освобождается. Всё остальное
вторично. **Тихая потеря сообщения — худший класс багов**; защищать в первую очередь.

## Контекст работы

Код живёт на сервере, локально только этот файл с правилами и (опционально)
клон репо. Claude Code запускается в этой папке, на сервер ходит по SSH.

- SSH: `ssh -i ~/.ssh/id_ed25519 pavel@178.72.178.9`
- Путь к проекту: `/srv/charger-watcher/`
- GitHub: https://github.com/devAsmodeus/Charger-Watcher (public)

## Стек

- Python 3.12, aiogram 3, SQLAlchemy 2 async, asyncpg, Postgres 16, Redis 7
- Alembic-миграции в `alembic/versions/`
- 4 docker-сервиса: postgres, redis, poller, bot (+ gluetun-VPN-контейнер)

## Сетевой режим — критично

- **bot** ходит наружу через `gluetun` (CyberGhost OpenVPN, US) — иначе из РФ
  Telegram заблокирован
- **poller** ходит напрямую с RU-IP — нужен для `apigateway.malankabn.by`
  (тестировано: гео-блока нет, README провайдера устарел)
- **Claude Code на хосте** ходит через тот же `gluetun` по HTTP-прокси
  `127.0.0.1:8888` (env: `HTTPS_PROXY=http://127.0.0.1:8888` в `~/.bashrc`).
  Если TG начнёт троттлить на US-IP — разделить: bot вернуть на DE-туннель,
  Claude оставить на US в отдельном `gluetun-us` контейнере.
- Не менять `network_mode: service:gluetun` у bot и не добавлять gluetun
  для poller — сломаются разные половины

## Деплой-цикл — НЕ нарушать порядок

```
1. Изменить код локально (или прямо на сервере)
2. cd /srv/charger-watcher && docker compose build <service>
3. Если есть новая миграция:
   docker compose run --rm poller alembic upgrade head
4. docker compose up -d
5. docker compose logs --tail=20 bot poller
```

Dockerfile делает `COPY src/`, `COPY alembic/` на этапе build — без rebuild
новые файлы внутрь контейнера не попадут. Проверено на собственных шишках.

## File-scope discipline для параллельных агентов

Точки конфликтов:

- `src/bot/notifier.py` — все fixы доставки/dedupe сюда
- `src/bot/main.py` — UI, команды, callback-хэндлеры
- `src/poller/main.py` — REST-diff, SSE state
- `alembic/versions/` — кто первый создал номер ревизии, того и tapочки

При диспатче 2+ агентов — строго один файл одному агенту. Документы
(`docs/legal/*`) — отдельная зона, конфликтов не бывает.

## Telegram-токен

**Один токен — один процесс**. Никогда не запускать второй инстанс с тем же
токеном (`getUpdates` отдаётся одному; оба процесса ломаются попеременно).

## БД и тарифы

- Пользователь/база: `malanka:malanka` (исторически — оставлено для совместимости volume)
- Тест-грант paid-тарифа: прямой `INSERT … ON CONFLICT … SET tier='paid'`
  в таблицу `users`
- Reaper expired-tier живёт в poller-е (`TIER_REAPER_INTERVAL_SEC=3600`),
  free возвращается автоматом
- Платёжный флоу: `pre_checkout` ⚠️ сейчас НЕ валидирует amount/currency
  (Critical #3 из ревью, не починено)

## Известный технический долг

(см. `~/docs/server-setup.md` и историю ревью)

- Stale-claim reaper для `notification_log.delivered_at IS NULL` — не реализован
- `/export_me` — BY 99-Z требует, не реализовано
- Очистка лишних подписок при истечении paid → free (старейшие первыми) —
  обещано в ToS, не реализовано
- Multi-stage Dockerfile + non-root user + healthcheck для bot/poller — не сделано
- `fakeredis` не в `pyproject.toml` — тесты в `tests/` без него скипаются

## Что НЕ делать

- Не открывать порты Postgres/Redis наружу — биндить только на 127.0.0.1
- Не коммитить `.env`, `vpn/`, `client.key`, `client.crt` в git
- Не запускать `docker compose down -v` без причины — снесёт БД (`pg_data` volume)
- Не редактировать существующие миграции — только новые ревизии

## Полезные команды

```bash
# логи в реальном времени
docker compose logs -f bot poller

# рестарт после изменений в .env
docker compose restart bot poller

# обновление кода из git
git pull && docker compose up -d --build

# схема таблицы
docker compose exec -T postgres psql -U malanka -d malanka -c '\d notification_log'

# tg_id юзера (после его /start)
docker compose exec -T postgres psql -U malanka -d malanka \
  -c "SELECT tg_id, tier, paid_until FROM users ORDER BY created_at DESC LIMIT 5;"
```

---

# Karpathy Skills

Источник: https://github.com/forrestchang/andrej-karpathy-skills (CLAUDE.md, raw, retrieved 2026-05-09).

Behavioral guidelines to reduce common LLM coding mistakes. Merge with project-specific instructions as needed.

**Tradeoff:** These guidelines bias toward caution over speed. For trivial tasks, use judgment.

## 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

## 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

## 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

## 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:
```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

---

**These guidelines are working if:** fewer unnecessary changes in diffs, fewer rewrites due to overcomplication, and clarifying questions come before implementation rather than after mistakes.
