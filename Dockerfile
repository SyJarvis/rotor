FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    postgresql-client \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY ./src ./src
COPY ./alembic ./alembic
COPY ./alembic.ini ./alembic.ini

ENV PYTHONPATH=/app/src \
    DATABASE_URL=sqlite+aiosqlite:////data/rotor.db \
    CONVERSATION_STORE_DIR=/data/conversations

# Persist the SQLite database and conversation store through one mount.
RUN mkdir -p /data/conversations
VOLUME ["/data"]

# Expose port
EXPOSE 8000

# Run the application
CMD ["python", "-m", "rotor.cli", "serve", "--host", "0.0.0.0", "--port", "8000"]
