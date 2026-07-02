# GurmukhiOCR — production image (Fly.io, Railway, VPS, Render Docker, etc.)
FROM python:3.12-slim-bookworm

# qpdf linearizes uploaded PDFs so the viewer can render page 1 from ~200 KB
# instead of downloading the entire file (requires "fast web view").
RUN apt-get update \
    && apt-get install -y --no-install-recommends qpdf \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV FLASK_ENV=production \
    PYTHONUNBUFFERED=1 \
    PORT=8000

EXPOSE 8000

CMD ["gunicorn", "--workers", "2", "--bind", "0.0.0.0:8000", "--timeout", "120", "--graceful-timeout", "30", "--access-logfile", "-", "--error-logfile", "-", "app:app"]
