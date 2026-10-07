FROM docker.io/library/python:3.12-slim-bookworm AS runtime
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 TZ=Europe/Kyiv \
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright
WORKDIR /srv/monitor
COPY pyproject.toml requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock \
    && python -m playwright install --with-deps chromium \
    && useradd --create-home --uid 10001 monitor \
    && chmod -R a+rX /opt/playwright
RUN apt-get update && apt-get install -y --no-install-recommends xauth \
    && rm -rf /var/lib/apt/lists/*
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./
COPY scripts/start.sh ./scripts/start.sh
USER monitor
CMD ["sh", "scripts/start.sh"]

FROM runtime AS verification
USER root
RUN pip install --no-cache-dir pytest==8.4.2 pytest-asyncio==1.3.0 aiosqlite==0.22.1
COPY tests ./tests
USER monitor
CMD ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider"]

FROM runtime AS production
