# Multi-stage build for minimal runtime footprint
FROM ghcr.io/astral-sh/uv:python3.13-alpine AS builder

ENV UV_COMPILE_BYTECODE=1

WORKDIR /app

# Copy dependency definition files
COPY pyproject.toml uv.lock ./

# Sync dependencies using frozen lockfile
RUN uv sync --frozen --no-dev --no-install-project

# Final runtime stage
FROM python:3.13-alpine

WORKDIR /app

# Create unprivileged user and pre-create runtime directories
RUN adduser -D -u 10001 hozbot && \
    mkdir -p /app/storage /app/downloads && \
    chown -R hozbot:hozbot /app

# Copy the compiled virtual environment
COPY --from=builder --chown=hozbot:hozbot /app/.venv /app/.venv

# Ensure PATH prioritizes the virtual environment packages
ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1

# Copy only necessary application files
COPY --chown=hozbot:hozbot *.py ./
COPY --chown=hozbot:hozbot prompts/ ./prompts/
COPY --chown=hozbot:hozbot tools/ ./tools/

# Switch to unprivileged user
USER hozbot

# Run the long-polling bot application
CMD ["python", "main.py"]
