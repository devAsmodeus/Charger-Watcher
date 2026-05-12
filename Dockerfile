# ---- builder stage: venv с зависимостями ----
# Здесь живут компиляторы wheel'ов, pip-кэш и build-tools. В runtime их
# не пускаем — меньше CVE surface, меньше размер image.
FROM python:3.14-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY pyproject.toml ./
COPY src ./src
COPY alembic ./alembic
COPY alembic.ini ./

RUN pip install --upgrade pip && pip install .


# ---- runtime stage ----
FROM python:3.14-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH=/app/src

# Non-root user. Если контейнер пробьют через зависимость — у атакующего
# не будет root внутри namespace'а. UID 1000 — стандартный для unprivileged.
RUN useradd --create-home --uid 1000 --shell /usr/sbin/nologin app

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY --chown=app:app src ./src
COPY --chown=app:app alembic ./alembic
COPY --chown=app:app alembic.ini ./

USER app

# Healthcheck: процесс способен импортировать конфиг (Python alive +
# pythonpath корректен). Чем дёргать api.telegram.org — тем для poller'а
# (он напрямую, без gluetun) разный путь, а лёгкий import check — общий.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import sys; from config import get_settings; get_settings(); sys.exit(0)" || exit 1

CMD ["python", "-m", "poller.main"]
