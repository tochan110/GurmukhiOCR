# GurmukhiOCR

Web app for Gurmukhi-focused document OCR using Google Cloud Vision, with Supabase auth and storage.

## Before you commit

- **Never commit** `.env`, service account JSON files, or anything under `uploads/`.
- Copy `.env.example` to `.env` locally and fill in real values only on your machine or hosting provider.
- If a key or `.env` was ever pushed to GitHub, **rotate** it in Supabase and Google Cloud immediately.

## Local development

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env — add Supabase keys and path to your Vision service account JSON
# Optional: install qpdf so uploaded PDFs are linearized for fast viewer loading
#   macOS: brew install qpdf   Debian/Ubuntu: sudo apt install qpdf
python app.py
```

Set `FLASK_DEBUG=1` in `.env` for local debugging only.

## Production deployment

1. Set environment variables from `.env.example` on your host (Render, Fly.io, Railway, VPS, etc.).
2. Set **`FLASK_ENV=production`** and a strong random **`SECRET_KEY`** (required; the app refuses the default in production).
3. Set **`FLASK_DEBUG=0`** (or unset).
4. Store the Google service account JSON **outside the repo** and point `GURMUKHI_OCR_KEY_PATH` at it.
5. Run with a production WSGI server, not `python app.py` alone:

```bash
pip install gunicorn
gunicorn -w 2 -b 0.0.0.0:${PORT:-8000} 'app:app'
```

6. Use HTTPS in front of the app (reverse proxy or platform TLS). Session cookies are marked `Secure` when `FLASK_ENV=production`.
7. In Supabase: enable RLS on tables, restrict storage bucket policies, and use the **publishable** key only in the browser.

### Render

- Render **does not** use `runtime.txt` for Python. Set the version with either:
  - a **`.python-version`** file in the repo root (this repo pins `3.12.8`), or  
  - the **`PYTHON_VERSION`** environment variable (e.g. `3.12.8`, fully qualified if using the dashboard option that requires it).
- **Python 3.14** is the default on new Render services (as of their docs) and currently breaks `google-cloud-vision` / `protobuf` at import time; stay on **3.12.x** or **3.13.x** until those stacks support 3.14.
- **System package `qpdf`** (PDF linearization for fast viewer loading): on Render’s **native Python** runtime, install it via the repo-root **`apt-packages`** file (`qpdf`, one package per line). If you deploy with **Docker**, use the included **`Dockerfile`** (it installs `qpdf` in the image).
- **Build command:** `pip install -r requirements.txt`
- **Start command:** `gunicorn --workers 2 --bind 0.0.0.0:$PORT --timeout 120 --graceful-timeout 30 --access-logfile - --error-logfile - app:app`  
  The **120s timeout** avoids spurious **502** responses when loading large OCR JSON from the database (Render/gunicorn default is often 30s). If payloads are very large, upgrade instance memory or split storage for OCR JSON.

### Docker (Fly.io, Railway, VPS, Render Docker)

```bash
docker build -t gurmukhi-ocr .
docker run --env-file .env -p 8000:8000 gurmukhi-ocr
```

The image includes **`qpdf`** on `PATH`. Without it, uploads still work but PDFs are not linearized and the viewer may download the full file before showing page 1.

## Security notes

- **Supabase**: `SUPABASE_SECRET_KEY` is server-only. Never expose it in templates or client JS.
- **Legacy routes** (`/pdf-json`, `/api/bundle/*`): require a configured Vision key but not Supabase user auth; prefer the authenticated `/process` and `/api/files/*` flow for production.
- **Session OCR keys**: users can upload a key at `/setup-credentials`; files live under `uploads/session_credentials/` (gitignored).

## Project layout

| Path | Purpose |
|------|---------|
| `app.py` | Flask API and page routes |
| `templates/` | HTML (landing, auth, dashboard) |
| `uploads/` | Local session credentials and legacy bundles (not in git) |
