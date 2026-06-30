# syntax=docker/dockerfile:1
#
# BrizoCast — Outdoor Conditions Alert Bot
#
# Multi-arch image. The `python:3.12-slim` base publishes manifests for amd64,
# arm64, and arm/v7, so the same Dockerfile builds on an x86 dev machine and on
# a Raspberry Pi (ARM) without modification.
#
# The container runs the Telegram bot via long polling (outbound only) and
# persists its SQLite database + bundled JSON datasets under /app/data, which is
# declared as a volume so data survives container recreation.

FROM python:3.12-slim

WORKDIR /app

# Unbuffered stdout/stderr for prompt log delivery; disable pip's cache to keep
# the image small.
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# Install dependencies first (better layer caching): only pyproject.toml is
# needed to resolve the dependency set.
COPY pyproject.toml ./
COPY README.md ./
RUN pip install --upgrade pip && pip install .

# Copy the application source.
COPY . .

# Run as an unprivileged user. Create the data directory up front and hand
# ownership of the app tree to the non-root user.
RUN useradd -m app \
    && mkdir -p /app/data \
    && chown -R app:app /app
USER app

# Persist SQLite database and JSON dataset across container restarts.
VOLUME ["/app/data"]

# The composition root / bot entrypoint (implemented in task 11.1). Referenced
# here intentionally so the image is ready once that module lands.
CMD ["python", "-m", "brizocast.bot.app"]
