FROM python:3.11-slim

WORKDIR /app

# Install system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps
COPY pyproject.toml .
RUN pip install --no-cache-dir -e .

# Copy source
COPY sentinel/ sentinel/
COPY scripts/ scripts/

# Create data directory
RUN mkdir -p data

CMD ["python", "-m", "sentinel"]
