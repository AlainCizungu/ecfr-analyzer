FROM python:3.12-slim

WORKDIR /app

# Install dependencies first (cached layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Data directory — mount a Fly volume here to persist ecfr.db across deploys
RUN mkdir -p /data
ENV DB_PATH=/data/ecfr.db

EXPOSE 8000

# Render injects $PORT; fall back to 8000 for local use
CMD ["sh", "-c", "\
  if [ ! -f /data/ecfr.db ]; then \
    echo 'First boot: seeding database…' && python seed_data.py; \
  fi && \
  uvicorn api:app --host 0.0.0.0 --port ${PORT:-8000} --workers 2"]
