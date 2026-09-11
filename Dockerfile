FROM python:3.12-slim

WORKDIR /app

# Keep gcc so the database driver can still be built when no wheel matches.
RUN apt-get update && apt-get install -y --no-install-recommends gcc \
    && rm -rf /var/lib/apt/lists/*

# Install the package itself so runtime dependencies come from pyproject.toml.
# Source checkout entry points for development are not copied into the image.
COPY pyproject.toml README.md alembic.ini ./
COPY ./src ./src
RUN pip install --no-cache-dir .

ENV PYTHONPATH=/app/src \
    DATABASE_URL=sqlite+aiosqlite:////data/rotor.db \
    CONVERSATION_STORE_DIR=/data/conversations \
    ROTOR_LOG_DIR=/data/logs

# Persist the SQLite database, logs, and conversation store through one mount.
RUN mkdir -p /data/conversations /data/logs
VOLUME ["/data"]

# Expose port
EXPOSE 8000

# Run the application
CMD ["python", "-m", "rotor.cli", "serve", "--host", "0.0.0.0", "--port", "8000"]
