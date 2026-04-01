# ── Field Nation Content Auditor ──────────────────────────────────────────────
# Google Cloud Run deployment image
# Uses Python 3.11-slim + Playwright Chromium for JS-rendered page scraping
FROM python:3.11-slim

# Chromium system dependencies (required by Playwright)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    libnss3 libnspr4 \
    libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libdbus-1-3 \
    libxkbcommon0 libxcomposite1 libxdamage1 \
    libxfixes3 libxrandr2 libgbm1 libasound2 \
    libpango-1.0-0 libcairo2 libgdk-pixbuf-2.0-0 \
    fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first (layer-cached unless requirements.txt changes)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright's Chromium browser
RUN playwright install chromium

# Copy application code
COPY audit.py server.py Procfile ./
COPY templates/ templates/

# Cloud Run injects PORT env var; server.py already reads it
ENV PORT=8080
EXPOSE 8080

CMD ["python3", "server.py"]
