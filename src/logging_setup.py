"""Logging setup: pretty stdout + optional rotating JSON errors-file.

Поведение:
  - stdout: structlog ConsoleRenderer (читабельно глазами, забирается
    `docker compose logs`).
  - errors_file (опционально): RotatingFileHandler, JSON-строка на событие,
    уровень WARNING+. Используется для быстрого «что сломалось за сутки»
    через `tail` / `jq` / `grep` без docker.

Маршрутизация через stdlib logging — даёт точку добавления второго
handler'а (file) с другим форматом, не ломая существующий
``log.info(...)``-call API.
"""
import logging
import logging.handlers
import sys
from typing import Any

import structlog


def setup_logging(
    level: str = "INFO",
    errors_file: str | None = None,
    max_bytes: int = 5_000_000,
    backups: int = 5,
) -> None:
    lvl = getattr(logging, level.upper(), logging.INFO)

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
    ]

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    handlers: list[logging.Handler] = []

    stdout = logging.StreamHandler(sys.stdout)
    stdout.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared_processors,
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.dev.ConsoleRenderer(),
            ],
        )
    )
    stdout.setLevel(lvl)
    handlers.append(stdout)

    if errors_file:
        try:
            file_handler = logging.handlers.RotatingFileHandler(
                errors_file,
                maxBytes=max_bytes,
                backupCount=backups,
                encoding="utf-8",
            )
            file_handler.setFormatter(
                structlog.stdlib.ProcessorFormatter(
                    foreign_pre_chain=shared_processors,
                    processors=[
                        structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                        structlog.processors.JSONRenderer(),
                    ],
                )
            )
            file_handler.setLevel(logging.WARNING)
            handlers.append(file_handler)
        except Exception as e:  # noqa: BLE001
            # Не валим процесс: бот должен работать даже если volume не
            # смонтирован / нет прав на запись. Логируем дефект в stderr —
            # потом разберёмся, текущая сессия не пострадает.
            sys.stderr.write(
                f"WARN: errors_file disabled at startup: {errors_file!r} → {e!r}\n"
            )

    root = logging.getLogger()
    root.handlers.clear()
    for h in handlers:
        root.addHandler(h)
    root.setLevel(lvl)
