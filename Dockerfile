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

# Copy the compiled virtual environment
COPY --from=builder /app/.venv /app/.venv

# Ensure PATH prioritizes the virtual environment packages
ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1

# Copy module files
COPY agent_email.py main.py ./

# Run the long-polling bot application
CMD ["python", "main.py"]
