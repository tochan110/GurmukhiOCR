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
