from flask import Flask, render_template, request, send_file, jsonify, redirect, url_for, Response, has_request_context
from dotenv import load_dotenv
from google.cloud import vision
from google.oauth2 import service_account
import fitz  # PyMuPDF
from PIL import Image, UnidentifiedImageError
import gc, io, mimetypes, tempfile, os, json, re, zipfile, sys, urllib.request, urllib.parse, math, traceback, gzip
import subprocess, shutil, threading
import requests
from requests.adapters import HTTPAdapter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
import uuid
import time
import jwt
from uuid import UUID
from supabase import create_client

from file_access import (
    FileAccess,
    access_payload,
    resolve_file_access,
)
from file_transfer import TransferError, transfer_file_ownership
from user_profiles import (
    ensure_profile_username,
    lookup_user_id_by_username,
    normalize_username_input,
    profile_user_display,
    update_profile_username,
    validate_username_format,
)
from page_metadata import (
    get_page_metadata,
    list_metadata_collaborators,
    normalize_metadata_patch,
    upsert_page_metadata,
)
from admin_panel import register_admin
from lemon_squeezy import (
    create_lemon_checkout_url,
    extract_lemon_order_payload,
    fulfill_lemon_order,
    verify_lemon_signature,
)

load_dotenv()

supabase_url = os.environ.get("SUPABASE_URL")
supabase_secret_key = os.environ.get("SUPABASE_SECRET_KEY")
supabase_publishable_key = os.environ.get("SUPABASE_PUBLISHABLE_KEY")

supabase_browser_config = {"supabase_url": supabase_url, "supabase_publishable_key": supabase_publishable_key}

supabase_client = (
    create_client(supabase_url, supabase_secret_key) if supabase_url and supabase_secret_key else None
)

# Dual credit model: free_pages_remaining (monthly) + paid_pages_remaining (never expire).
# API still exposes pages_remaining = free + paid for frontend compatibility.
# Deprecated DB column profiles.pages_remaining is not used internally.
PROFILE_PAGES_MONTHLY_ALLOWANCE = 20
PROFILE_PAGES_RESET_INTERVAL_DAYS = 30
_CREDIT_BALANCE_SELECT = (
    "free_pages_remaining, paid_pages_remaining, monthly_free_credit_allowance, last_reset"
)
_CREDIT_UPDATE_MAX_ATTEMPTS = 5

# Stripe + Lemon one-time Checkout: map each pack → credits granted.
# This is the ONLY place credit packs are defined (landing + /pricing render from it).
# amount_usd is display-only; Stripe uses price_id, Lemon uses lemon_variant_id.
PRICE_PACKAGES = [
    {
        "price_id": "price_1TuGJHRtZiy12tM4DFpU5YWF",
        "lemon_variant_id": "1990774",
        "credits": 250,
        "amount_usd": "5.00",
    },
    {
        "price_id": "price_1TuGJHRtZiy12tM4LzJjQhfA",
        "lemon_variant_id": "1990775",
        "credits": 1000,
        "amount_usd": "15.00",
    },
    {
        "price_id": "price_1TuGJHRtZiy12tM4r9Odrwh5",
        "lemon_variant_id": "1990769",
        "credits": 8000,
        "amount_usd": "50.00",
    },
    {
        "price_id": "price_1TuGJHRtZiy12tM4OgjkuP7P",
        "lemon_variant_id": "1990776",
        "credits": 32000,
        "amount_usd": "150.00",
    },
    {
        "price_id": "price_1TuGJHRtZiy12tM4xdc2Sjtc",
        "lemon_variant_id": "1990777",
        "credits": 128000,
        "amount_usd": "500.00",
    },
]
PRICE_ID_TO_CREDITS = {p["price_id"]: int(p["credits"]) for p in PRICE_PACKAGES}
LEMON_VARIANT_ID_TO_CREDITS = {
    str(p["lemon_variant_id"]): int(p["credits"])
    for p in PRICE_PACKAGES
    if p.get("lemon_variant_id")
}

# Super-admin email allowlist (lowercase match). Env SUPERADMINS=a@x.com,b@y.com overrides.
_SUPERADMINS_ENV = (os.environ.get("SUPERADMINS") or "").strip()
if _SUPERADMINS_ENV:
    SUPERADMINS = [e.strip() for e in _SUPERADMINS_ENV.split(",") if e.strip()]
else:
    SUPERADMINS = [
        "ggill@sailboattalent.com",
    ]

STRIPE_SECRET_KEY = (os.environ.get("STRIPE_SECRET_KEY") or "").strip()
STRIPE_WEBHOOK_SECRET = (os.environ.get("STRIPE_WEBHOOK_SECRET") or "").strip()

LEMON_SQUEEZY_API_KEY = (os.environ.get("LEMON_SQUEEZY_API_KEY") or "").strip()
LEMON_SQUEEZY_STORE_ID = (os.environ.get("LEMON_SQUEEZY_STORE_ID") or "").strip()
LEMON_SQUEEZY_WEBHOOK_SECRET = (os.environ.get("LEMON_SQUEEZY_WEBHOOK_SECRET") or "").strip()

SUPABASE_STORAGE_BUCKET = "gbucket"
SUPABASE_JSON_BUCKET = (os.environ.get("SUPABASE_JSON_BUCKET") or SUPABASE_STORAGE_BUCKET).strip()
VISION_ASYNC_GCS_BUCKET = (
    os.environ.get("VISION_ASYNC_GCS_BUCKET")
    or os.environ.get("GURMUKHI_OCR_GCS_BUCKET")
    or "gocr-processing-1"
).strip()
VISION_ASYNC_BATCH_SIZE = 100
_GCP_CLOUD_PLATFORM_SCOPES = ("https://www.googleapis.com/auth/cloud-platform",)
_GCS_BASE_URL = "https://storage.googleapis.com/storage/v1"
_GCS_UPLOAD_URL = "https://storage.googleapis.com/upload/storage/v1"
_GCS_RETRY_STATUSES = {408, 429, 500, 502, 503, 504}
_GCS_MAX_ATTEMPTS = 3
_STORAGE_STREAM_CHUNK = 8 * 1024 * 1024
_VISION_OPERATION_MAX_ATTEMPTS = 3
_OCR_LEASE_SECONDS = 120
_ocr_bg_lock = threading.Lock()
_ocr_bg_running: set[str] = set()
_ocr_resume_lock = threading.Lock()
_ocr_resume_started = False
_OCR_WORKER_ID = f"pid{os.getpid()}-{uuid.uuid4().hex[:8]}"

# Temporary upload-pipeline profiling (remove after bottleneck is identified).
_PROFILE_TAG = "[profile:upload]"
_upload_profile_tls = threading.local()

# Temporary memory profiling (remove after the memory spike is located).
try:
    import psutil as _psutil
    _mem_proc = _psutil.Process()
except Exception:
    _psutil = None
    _mem_proc = None


def _log_mem(tag: str) -> None:
    """Log current process RSS in MB, e.g. `[mem] before_ocr rss=182 MB`."""
    if _mem_proc is None:
        return
    try:
        rss_mb = _mem_proc.memory_info().rss / (1024 * 1024)
        print(f"[mem] {tag} rss={rss_mb:.0f} MB", file=sys.stderr, flush=True)
    except Exception:
        pass


# Obsolete PDF OCR raster-path instrumentation. PDF OCR now uses Vision asyncBatchAnnotateFiles;
# the separate PDF viewer renderer remains below in _render_stored_document_page_png().
_render_inflight = 0
_render_inflight_lock = threading.Lock()


class _UploadProfile:
    """Collects per-step timings for one upload/OCR job; logs to stderr."""

    def __init__(self, job_id: str):
        self.job_id = job_id
        self.t0 = time.perf_counter()
        self.metrics: dict[str, float] = {}
        self.counts: dict[str, int] = {}
        self._lock = threading.Lock()

    def log_step(self, step: str, duration_s: float, **extra) -> None:
        extra_s = " ".join(f"{k}={v}" for k, v in extra.items())
        msg = f"{_PROFILE_TAG} job_id={self.job_id} step={step} duration_s={duration_s:.3f}"
        if extra_s:
            msg += f" {extra_s}"
        print(msg, file=sys.stderr, flush=True)

    def add(self, metric: str, duration_s: float, count_key: str | None = None, count: int = 1) -> None:
        with self._lock:
            self.metrics[metric] = self.metrics.get(metric, 0.0) + duration_s
            if count_key:
                self.counts[count_key] = self.counts.get(count_key, 0) + count

    def time_step(self, step: str, **extra):
        return _UploadProfileStep(self, step, extra)

    def finish(self, **extra) -> None:
        total = time.perf_counter() - self.t0
        metric_parts = [f"{k}={v:.3f}s" for k, v in sorted(self.metrics.items())]
        count_parts = [f"{k}={v}" for k, v in sorted(self.counts.items())]
        extra_parts = [f"{k}={v}" for k, v in extra.items()]
        summary = " ".join(metric_parts + count_parts + extra_parts)
        print(
            f"{_PROFILE_TAG} job_id={self.job_id} step=total duration_s={total:.3f} {summary}".rstrip(),
            file=sys.stderr,
            flush=True,
        )


class _UploadProfileStep:
    def __init__(self, profile: _UploadProfile, step: str, extra: dict):
        self.profile = profile
        self.step = step
        self.extra = extra
        self.t0 = 0.0

    def __enter__(self):
        self.t0 = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.profile.log_step(self.step, time.perf_counter() - self.t0, **self.extra)


def _active_upload_profile() -> _UploadProfile | None:
    return getattr(_upload_profile_tls, "profile", None)


def _set_active_upload_profile(profile: _UploadProfile | None) -> None:
    _upload_profile_tls.profile = profile


_DEV_SECRET_KEY = "dev-only-change-SECRET_KEY-in-production"


def _env_truthy(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in ("1", "true", "yes", "on")


def _is_production() -> bool:
    return (os.environ.get("FLASK_ENV") or "").strip().lower() == "production" or _env_truthy("PRODUCTION")


app = Flask(__name__)
_secret = (os.environ.get("SECRET_KEY") or "").strip() or _DEV_SECRET_KEY
if _is_production() and _secret == _DEV_SECRET_KEY:
    raise RuntimeError("SECRET_KEY must be set to a strong random value when FLASK_ENV=production.")
app.config["SECRET_KEY"] = _secret
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = _is_production()
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)

# Public site origin for canonicals, sitemap, OG URLs, and auth redirects.
PUBLIC_APP_URL = (
    os.environ.get("PUBLIC_APP_URL") or os.environ.get("APP_BASE_URL") or ""
).strip().rstrip("/")

# Trust / legal identity shown on About, Contact, Privacy, Terms, Refund pages.
# Brand-only for now (no separate legal-entity display).
BUSINESS_PROFILE = {
    "brand_name": "GurmukhiOCR",
    "legal_name": "GurmukhiOCR",
    "support_email": "team@gurmukhiocr.com",
    "support_response": (os.environ.get("BUSINESS_SUPPORT_RESPONSE") or "1–2 business days").strip(),
    "refund_days": int((os.environ.get("BUSINESS_REFUND_DAYS") or "14").strip() or "14"),
    "refund_max_credits_used": int(
        (os.environ.get("BUSINESS_REFUND_MAX_CREDITS_USED") or "20").strip() or "20"
    ),
    "liability_months": int((os.environ.get("BUSINESS_LIABILITY_MONTHS") or "12").strip() or "12"),
    "product_summary": (
        "GurmukhiOCR provides online Punjabi / Gurmukhi OCR: customers upload PDFs or images, "
        "we run optical character recognition with Google Cloud Vision, customers review and edit "
        "Unicode text in the browser, and export .txt. Free monthly OCR credits are included; "
        "optional paid one-time packs add OCR credits (1 credit ≈ 1 page or image)."
    ),
}


def get_public_app_base_url() -> str:
    """Return the public site origin for the current environment."""
    if PUBLIC_APP_URL:
        return PUBLIC_APP_URL
    if has_request_context():
        proto = (
            (request.headers.get("X-Forwarded-Proto") or request.scheme or "http")
            .split(",")[0]
            .strip()
        )
        host = (
            (request.headers.get("X-Forwarded-Host") or request.host or "")
            .split(",")[0]
            .strip()
        )
        if host:
            return f"{proto}://{host}".rstrip("/")
        root = (request.url_root or "").rstrip("/")
        if root:
            return root
    return "http://127.0.0.1:5000"


def _seo_context(
    *,
    title: str,
    description: str,
    path: str,
    robots: str | None = None,
    json_ld: list | None = None,
    page_heading: str | None = None,
    page_lede: str | None = None,
    active_page: str | None = None,
) -> dict:
    base = get_public_app_base_url()
    path = path if path.startswith("/") else f"/{path}"
    canonical = f"{base}{path}" if path != "/" else f"{base}/"
    return {
        "page_title": title,
        "meta_description": description,
        "canonical_url": canonical,
        "app_base_url": base,
        "robots_content": robots,
        "json_ld": json_ld or [],
        "og_image": f"{base}/static/dashboard_dark.png",
        "page_heading": page_heading or title,
        "page_lede": page_lede,
        "active_page": active_page or "",
    }


def _homepage_json_ld(base: str) -> list[dict]:
    org = {
        "@context": "https://schema.org",
        "@type": "Organization",
        "name": "GurmukhiOCR",
        "url": f"{base}/",
        "logo": f"{base}/static/gocr_logo.png",
        "email": "team@gurmukhiocr.com",
        "description": (
            "Online Punjabi OCR and Gurmukhi OCR for converting scanned PDFs and "
            "images into editable Unicode text."
        ),
    }
    website = {
        "@context": "https://schema.org",
        "@type": "WebSite",
        "name": "GurmukhiOCR",
        "url": f"{base}/",
        "description": (
            "Punjabi OCR and Gurmukhi OCR tool to convert Punjabi PDFs and images "
            "into editable text."
        ),
        "publisher": {"@type": "Organization", "name": "GurmukhiOCR"},
    }
    software = {
        "@context": "https://schema.org",
        "@type": "SoftwareApplication",
        "name": "GurmukhiOCR",
        "applicationCategory": "BusinessApplication",
        "operatingSystem": "Web",
        "url": f"{base}/",
        "image": f"{base}/static/dashboard_dark.png",
        "description": (
            "Free online Punjabi OCR and Gurmukhi OCR. Convert scanned Punjabi "
            "PDFs and images into editable Unicode text with review tools and export."
        ),
        "offers": {
            "@type": "Offer",
            "price": "0",
            "priceCurrency": "USD",
            "description": "Free monthly OCR credits; paid packs available",
        },
        "featureList": [
            "Punjabi OCR",
            "Gurmukhi OCR",
            "Punjabi PDF OCR",
            "Punjabi image OCR",
            "Document conversion to editable text",
            "Side-by-side OCR editor",
            "Free OCR credits",
        ],
    }
    return [org, website, software]


@app.context_processor
def inject_public_app_urls():
    base = get_public_app_base_url()
    return {
        "app_base_url": base,
        "password_reset_redirect_url": f"{base}/reset-password",
        "business": BUSINESS_PROFILE,
        "supabase_url": supabase_url,
        "supabase_publishable_key": supabase_publishable_key,
    }


@app.after_request
def _cors_process(resp):
    if request.path == "/process":
        origin = (os.environ.get("CORS_PROCESS_ORIGIN") or "*").strip()
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
        resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
    return resp


@app.after_request
def _seo_robot_headers(resp):
    """Send X-Robots-Tag on private / auth / API surfaces."""
    path = request.path or ""
    if (
        path.startswith("/api/")
        or path.startswith("/dashboard")
        or path == "/process"
        or path.startswith("/process/")
        or path in (
            "/login",
            "/signup",
            "/forgot-password",
            "/reset-password",
            "/setup-credentials",
        )
    ):
        resp.headers.setdefault("X-Robots-Tag", "noindex, nofollow")
    return resp


class EmptyPagesError(Exception):
    """Exception raised when 10 consecutive empty pages are detected."""
    def __init__(self, start_page, end_page, message=None):
        self.start_page = start_page
        self.end_page = end_page
        if message is None:
            message = f"OCR processing paused: 10 consecutive empty pages detected (pages {start_page} to {end_page}). This may indicate an issue with the PDF or OCR service."
        super().__init__(message)


class VisionConfigurationError(Exception):
    """Vision API blocked by project setup, billing, IAM, or disabled API — show actionable guidance to the user."""

    def __init__(self, user_message, subtype, technical_detail=None):
        super().__init__(user_message)
        self.user_message = user_message
        self.subtype = subtype  # api_disabled | permission_denied | billing | quota | invalid_credentials
        self.technical_detail = technical_detail


def _first_https_url(text):
    if not text:
        return None
    m = re.search(r"https://[^\s\"'<>]+", text)
    return m.group(0) if m else None


def _vision_error_from_message(message: str):
    """Map common Google Vision / GCP error text to VisionConfigurationError."""
    if not message:
        return None
    lower = message.lower()
    if "service_disabled" in lower or "has not been used in project" in lower or "api has not been used" in lower:
        return VisionConfigurationError(
            "The Cloud Vision API is not enabled for the Google Cloud project attached to this key. "
            "Enable Vision in that project (Google Cloud Console → APIs & Services), wait a few minutes, then try again — or upload a JSON key from a project where Vision is already enabled.",
            "api_disabled",
            message,
        )
    if "billing" in lower and ("disabled" in lower or "not enabled" in lower or "account" in lower):
        return VisionConfigurationError(
            "Billing may be disabled or required for this Google Cloud project. Open Billing in Google Cloud Console for the project that owns this service account, then retry.",
            "billing",
            message,
        )
    if "permission denied" in lower or "403" in lower or "forbidden" in lower:
        return VisionConfigurationError(
            "This service account is not allowed to call the Vision API (wrong project, missing IAM role, or wrong key file). "
            "Confirm you uploaded the JSON key for the project where Vision is enabled, and that the account has a role that can use Vision (e.g. Cloud Vision AI User or Editor).",
            "permission_denied",
            message,
        )
    return None


def _raise_vision_configuration_if_applicable(exc):
    """Raise VisionConfigurationError when exc is a known setup/billing/IAM case."""
    from google.api_core import exceptions as google_exceptions

    if isinstance(exc, google_exceptions.PermissionDenied):
        mapped = _vision_error_from_message(str(exc))
        if mapped:
            raise mapped from exc
        raise VisionConfigurationError(
            "Google Cloud returned permission denied for Vision. Check the API key JSON, IAM permissions, and that the Vision API is enabled for that project.",
            "permission_denied",
            str(exc),
        ) from exc
    if isinstance(exc, google_exceptions.Unauthenticated):
        raise VisionConfigurationError(
            "The service account JSON is invalid, expired, or not accepted by Google. Upload a current key from Google Cloud Console → IAM → Service accounts → Keys.",
            "invalid_credentials",
            str(exc),
        ) from exc
    if isinstance(exc, google_exceptions.ResourceExhausted):
        raise VisionConfigurationError(
            "Vision API quota exceeded or rate limited. Check usage and billing in Google Cloud Console, then retry.",
            "quota",
            str(exc),
        ) from exc


def _vision_error_title(subtype):
    return {
        "api_disabled": "Enable Cloud Vision API",
        "billing": "Google Cloud billing",
        "permission_denied": "Vision API access denied",
        "invalid_credentials": "Invalid or expired service account key",
        "quota": "Vision API quota exceeded",
    }.get(subtype, "Google Cloud configuration")


def jsonify_vision_configuration_error(exc: VisionConfigurationError):
    """HTTP response for Vision project/key/billing issues (structured for the frontend)."""
    payload = {
        "error": exc.user_message,
        "error_type": "cloud_configuration",
        "error_subtype": exc.subtype,
        "error_title": _vision_error_title(exc.subtype),
    }
    if exc.technical_detail:
        payload["technical_detail"] = exc.technical_detail
    url = _first_https_url(exc.technical_detail or "")
    if url:
        payload["console_url"] = url
    status = 429 if exc.subtype == "quota" else 403
    return jsonify(payload), status


# Directory structure
UPLOADS_DIR = "uploads"
BUNDLES_DIR = os.path.join(UPLOADS_DIR, "bundles")

# Legacy bundle dirs; OCR results are not written under BUNDLES_DIR (client download only).


def _resolve_bundle_dir(bundle_id: str) -> str | None:
    """Resolve a legacy bundle id to an absolute directory under BUNDLES_DIR (blocks path traversal)."""
    if not bundle_id or ".." in bundle_id or "/" in bundle_id or "\\" in bundle_id:
        return None
    bundle_path = os.path.join(BUNDLES_DIR, bundle_id)
    try:
        abs_base = os.path.realpath(BUNDLES_DIR)
        abs_path = os.path.realpath(bundle_path)
    except OSError:
        return None
    if not abs_path.startswith(abs_base + os.sep):
        return None
    if not os.path.isdir(abs_path):
        return None
    return abs_path


def _default_shared_ocr_key_path() -> str | None:
    """Path to the server Vision service account JSON from environment.

    - ``GURMUKHI_OCR_KEY_PATH`` — preferred; absolute path to the JSON file.
    - ``GOOGLE_APPLICATION_CREDENTIALS`` — fallback when the above is unset/invalid.
    """
    for env_name in ("GURMUKHI_OCR_KEY_PATH", "GOOGLE_APPLICATION_CREDENTIALS"):
        p = (os.environ.get(env_name) or "").strip()
        if p and os.path.isfile(p):
            return p
    return None


def has_ocr_credentials() -> bool:
    """True when a server-side Vision service account JSON is configured."""
    return _default_shared_ocr_key_path() is not None


def get_credentials_file_path() -> str:
    """Path to the server Vision service account JSON (environment only)."""
    path = _default_shared_ocr_key_path()
    if not path:
        raise RuntimeError(
            "No Google Cloud credentials configured. Set GURMUKHI_OCR_KEY_PATH or "
            "GOOGLE_APPLICATION_CREDENTIALS to a service account JSON file."
        )
    return path


_ocr_vision_client_lock = threading.Lock()
_ocr_vision_client_cache: tuple[str, vision.ImageAnnotatorClient] | None = None


def get_vision_client() -> vision.ImageAnnotatorClient:
    """Build or return a cached Vision client using the server environment key."""
    global _ocr_vision_client_cache
    path = get_credentials_file_path()
    with _ocr_vision_client_lock:
        if _ocr_vision_client_cache is not None and _ocr_vision_client_cache[0] == path:
            return _ocr_vision_client_cache[1]
        creds = service_account.Credentials.from_service_account_file(path)
        client = vision.ImageAnnotatorClient(credentials=creds)
        _ocr_vision_client_cache = (path, client)
        return client


# Vision accepts large images but very large rasters or malformed buffers cause INVALID_ARGUMENT / "bad image data".
# See: https://cloud.google.com/vision/docs/supported-files
_VISION_MAX_PIXELS = 75_000_000
_VISION_MAX_IMAGE_BYTES = 20 * 1024 * 1024
_VISION_MAX_EDGE = 16_384

# Obsolete PDF OCR raster constants. Kept only to make accidental old-path use obvious.
_RENDER_TARGET_DPI = 300
_RENDER_MAX_EDGE = 5000


def render_pdf_page_to_image(doc, page_index):
    """Obsolete PDF OCR raster path, disabled after async Vision PDF OCR migration."""
    raise RuntimeError("PDF OCR rasterization is disabled; use Vision async PDF OCR.")
    # Old implementation intentionally commented out:
    # - rendered each PDF page to a high-DPI image with PyMuPDF
    # - converted that image through PIL
    # - submitted one Vision text_detection request per page
    # That path caused large memory spikes and is no longer used for OCR.


def _pil_to_rgb_white_background(image: Image.Image) -> Image.Image:
    """Normalize to RGB; flatten transparency onto white (common fix for odd PNG encodes)."""
    if image.mode == "RGB":
        return image
    if image.mode in ("RGBA", "LA"):
        base = Image.new("RGB", image.size, (255, 255, 255))
        if image.mode == "RGBA":
            base.paste(image, mask=image.split()[3])
        else:
            base.paste(image.convert("RGBA"), mask=image.split()[1])
        return base
    if image.mode == "P":
        rgba = image.convert("RGBA")
        base = Image.new("RGB", rgba.size, (255, 255, 255))
        base.paste(rgba, mask=rgba.split()[3])
        return base
    return image.convert("RGB")


def _maybe_downscale_for_vision(rgb: Image.Image) -> Image.Image:
    """Stay within documented pixel budget and sane edge length (avoids broken encodes / API errors)."""
    w, h = rgb.size
    if w < 1 or h < 1:
        raise ValueError("Invalid image dimensions")
    max_edge = max(w, h)
    scale = 1.0
    if w * h > _VISION_MAX_PIXELS:
        scale = min(scale, ((_VISION_MAX_PIXELS / (w * h)) ** 0.5) * 0.995)
    if max_edge > _VISION_MAX_EDGE:
        scale = min(scale, _VISION_MAX_EDGE / max_edge)
    if scale < 1.0:
        nw = max(1, int(w * scale))
        nh = max(1, int(h * scale))
        return rgb.resize((nw, nh), Image.Resampling.LANCZOS)
    return rgb


def image_to_vision_bytes_jpeg(rgb: Image.Image, quality: int = 92) -> bytes:
    # optimize=False: faster encodes; subsampling=0: sharper text (default for Q>=95, still ok at 92–94)
    buf = io.BytesIO()
    rgb.save(buf, format="JPEG", quality=quality, subsampling=0, optimize=False)
    return buf.getvalue()


def prepare_image_bytes_for_vision(pil_image: Image.Image) -> tuple[bytes, str]:
    """
    Encode for Vision API. JPEG (high Q) is much faster and smaller than PNG; fine for text OCR.
    If the payload is still over 20MB, downscale and re-encode (rare; huge pages at 300 DPI).
    """
    pil_image.load()
    rgb = _pil_to_rgb_white_background(pil_image)
    rgb = _maybe_downscale_for_vision(rgb)
    q = 94
    for _ in range(14):
        data = image_to_vision_bytes_jpeg(rgb, quality=q)
        if len(data) <= _VISION_MAX_IMAGE_BYTES:
            return data, "image/jpeg"
        w, h = rgb.size
        rgb = rgb.resize(
            (max(1, int(w * 0.90)), max(1, int(h * 0.90))),
            Image.Resampling.LANCZOS,
        )
        q = max(75, q - 2)
    return image_to_vision_bytes_jpeg(rgb, quality=75), "image/jpeg"


def _finalize_text_detection_response(response):
    """Raise on RPC error embedded in Vision response."""
    import sys

    if hasattr(response, "error") and response.error:
        if response.error.code != 0 or (
            response.error.message and response.error.message.strip()
        ):
            rpc_msg = response.error.message or "Unknown error"
            mapped = _vision_error_from_message(rpc_msg)
            if mapped:
                print(f"[OCR Error] {mapped.technical_detail or rpc_msg}", file=sys.stderr, flush=True)
                raise mapped
            error_msg = f"Google Vision API error: {rpc_msg} (Code: {response.error.code})"
            print(f"[OCR Error] {error_msg}", file=sys.stderr, flush=True)
            raise Exception(error_msg)
    return response


def _is_bad_image_invalid_argument(exc: BaseException) -> bool:
    s = str(exc).lower()
    return any(
        x in s
        for x in (
            "bad image",
            "invalid image",
            "malformed",
            "could not decode",
            "image data",
            "corrupt",
        )
    )


def ocr_pil_with_client(vision_client, image: Image.Image):
    """Call Vision `text_detection` for one raster; thread-safe for shared `vision_client`."""
    from google.api_core import exceptions as google_exceptions
    import sys

    content, _mime = prepare_image_bytes_for_vision(image)
    vision_image = vision.Image(content=content)
    try:
        response = vision_client.text_detection(image=vision_image)
        return _finalize_text_detection_response(response)
    except VisionConfigurationError:
        raise
    except google_exceptions.InvalidArgument as e:
        if _is_bad_image_invalid_argument(e):
            print(
                "[OCR] Vision rejected image payload; retrying with JPEG and re-normalized RGB",
                file=sys.stderr,
                flush=True,
            )
            rgb = _pil_to_rgb_white_background(image)
            rgb = _maybe_downscale_for_vision(rgb)
            jpeg_bytes = image_to_vision_bytes_jpeg(rgb, quality=93)
            response = vision_client.text_detection(
                image=vision.Image(content=jpeg_bytes)
            )
            return _finalize_text_detection_response(response)
        error_msg = f"Google Vision API invalid argument: {str(e)}"
        print(f"[OCR Error] {error_msg}", file=sys.stderr, flush=True)
        raise Exception(error_msg) from e
    except google_exceptions.ResourceExhausted as e:
        print(f"[OCR Error] {e}", file=sys.stderr, flush=True)
        _raise_vision_configuration_if_applicable(e)
    except google_exceptions.PermissionDenied as e:
        print(f"[OCR Error] {e}", file=sys.stderr, flush=True)
        _raise_vision_configuration_if_applicable(e)
    except google_exceptions.Unauthenticated as e:
        print(f"[OCR Error] {e}", file=sys.stderr, flush=True)
        _raise_vision_configuration_if_applicable(e)
    except Exception as e:
        if isinstance(e, VisionConfigurationError):
            raise
        if "Google Vision API" in str(e) or isinstance(
            e, google_exceptions.GoogleAPIError
        ):
            mapped = _vision_error_from_message(str(e))
            if mapped:
                raise mapped from e
            raise
        error_msg = f"Google Vision API error: {str(e)}"
        print(f"[OCR Error] {error_msg}", file=sys.stderr, flush=True)
        raise Exception(error_msg) from e


def ocr_page(image):
    """Run OCR (uses Flask `g` client; same thread as the request)."""
    return ocr_pil_with_client(get_vision_client(), image)


def is_page_empty(page_data):
    """Check if a page JSON is empty (all fields are empty)."""
    return (
        not page_data.get("text", "").strip() and
        not page_data.get("full_text", "").strip() and
        len(page_data.get("blocks", [])) == 0 and
        len(page_data.get("paragraphs", [])) == 0 and
        len(page_data.get("words", [])) == 0
    )


def ocr_result_compiled_full_text(ocr_result: dict) -> str:
    """
    Concatenate page text in order (page_1, page_2, …), same rules as offline_bundles_viewer /
    Dashboard / offline viewer: each page uses full_text or text, separated by blank lines.
    """
    if not isinstance(ocr_result, dict):
        return ""
    keys = [k for k in ocr_result if re.match(r"^page_\d+$", k, re.I)]

    def _page_num(k):
        return int(re.sub(r"\D", "", k) or 0)

    keys.sort(key=_page_num)
    parts = []
    for k in keys:
        page = ocr_result.get(k) or {}
        if not isinstance(page, dict):
            continue
        t = (page.get("full_text") or page.get("text") or "").strip()
        if t:
            parts.append(t)
    return "\n\n".join(parts).strip()


def _bounding_poly_vertices(poly) -> list[dict]:
    """Return Vision vertices in the existing {x, y} shape, including async normalized vertices."""
    if not poly:
        return []
    vertices = getattr(poly, "vertices", None)
    if vertices:
        return [{"x": v.x, "y": v.y} for v in vertices]
    normalized = getattr(poly, "normalized_vertices", None)
    if normalized:
        return [
            {
                "x": _safe_ocr_float(getattr(v, "x", None)),
                "y": _safe_ocr_float(getattr(v, "y", None)),
            }
            for v in normalized
        ]
    return []


def _vision_response_to_page_data(response) -> dict:
    """Build `page_n` JSON from a Vision `text_detection` response."""
    page_data = {
        "text": "",
        "full_text": "",
        "blocks": [],
        "paragraphs": [],
        "words": [],
    }

    if response.text_annotations:
        page_data["full_text"] = response.text_annotations[0].description
    elif response.full_text_annotation and getattr(response.full_text_annotation, "text", None):
        page_data["full_text"] = response.full_text_annotation.text

    if response.full_text_annotation:
        for page_annotation in response.full_text_annotation.pages:
            for block in page_annotation.blocks:
                block_data = {
                    "bounding_box": {"vertices": _bounding_poly_vertices(block.bounding_box)},
                    "paragraphs": [],
                }

                for paragraph in block.paragraphs:
                    para_text = ""
                    para_words = []

                    for word in paragraph.words:
                        word_text = ""
                        for symbol in word.symbols:
                            word_text += symbol.text

                        if word_text:
                            para_text += word_text + " "
                            para_words.append(
                                {
                                    "text": word_text,
                                    "bounding_box": {"vertices": _bounding_poly_vertices(word.bounding_box)},
                                    "confidence": _safe_ocr_float(getattr(word, "confidence", None)),
                                }
                            )

                    para_data = {
                        "text": para_text.strip(),
                        "bounding_box": {"vertices": _bounding_poly_vertices(paragraph.bounding_box)},
                        "words": para_words,
                    }

                    block_data["paragraphs"].append(para_data)
                    page_data["paragraphs"].append(para_data)
                    page_data["words"].extend(para_words)

                page_data["blocks"].append(block_data)

            all_para_text = " ".join([p["text"] for p in page_data["paragraphs"]])
            page_data["text"] = all_para_text

    return page_data


def _effective_ocr_worker_count(total_pages: int) -> int:
    """Obsolete PDF OCR raster worker-count helper, disabled after async Vision PDF OCR migration."""
    raise RuntimeError("PDF OCR worker-count raster path is disabled; use Vision async PDF OCR.")
    # Old implementation intentionally commented out:
    # - read OCR_MAX_WORKERS
    # - selected up to 8 concurrent per-page Vision text_detection requests
    # Async PDF OCR now submits one long-running file request instead.


def _import_max_workers(page_count: int) -> int:
    """
    Parallel storage uploads during bundle import. Set IMPORT_MAX_WORKERS=1 for sequential.
    Default: up to 10 workers, capped by page count (2 uploads per page).
    """
    if page_count < 2:
        return 1
    raw = os.environ.get("IMPORT_MAX_WORKERS", "").strip()
    if raw == "1":
        return 1
    if raw.isdigit() and int(raw) >= 1:
        w = int(raw)
    else:
        w = min(10, max(2, os.cpu_count() or 4))
    return max(1, min(w, 20, page_count * 2))


def _parallel_ocr_one_page(pdf_bytes, page_index: int, vision_client) -> tuple[int, dict]:
    """Obsolete parallel raster PDF OCR helper, disabled after async Vision PDF OCR migration."""
    raise RuntimeError("Parallel raster PDF OCR is disabled; use Vision async PDF OCR.")
    # Old implementation intentionally commented out:
    # - opened the PDF in each worker
    # - rendered one page to an image
    # - called ocr_pil_with_client()
    # - converted the per-page response with _vision_response_to_page_data()


def _assemble_pages_with_empty_detection(
    page_datas: list, total_pages: int, result: dict, progress_callback, save_callback
) -> None:
    """In-order pass: 10 empty-page rule, `result` dict, save callback."""
    import sys

    consecutive_empty_count = 0
    first_empty_page = None

    for i in range(total_pages):
        page_num = i + 1
        page_data = page_datas[i]

        if is_page_empty(page_data):
            if consecutive_empty_count == 0:
                first_empty_page = page_num
            consecutive_empty_count += 1
            print(
                f"[Empty Page Detection] Page {page_num} is empty (consecutive count: {consecutive_empty_count})",
                file=sys.stderr,
                flush=True,
            )
            if consecutive_empty_count >= 10:
                error_msg = (
                    f"OCR processing paused: 10 consecutive empty pages detected (pages {first_empty_page} to {page_num}). "
                    "This may indicate an issue with the PDF or OCR service."
                )
                print(f"[Empty Page Detection] ERROR: {error_msg}", file=sys.stderr, flush=True)
                raise EmptyPagesError(first_empty_page, page_num, error_msg)
        else:
            consecutive_empty_count = 0
            first_empty_page = None
        
        result[f"page_{page_num}"] = page_data
        
        if save_callback:
            try:
                prof = _active_upload_profile()
                t_save = time.perf_counter()
                save_callback(page_num, page_data)
                if prof:
                    prof.add("json_page_save_callback", time.perf_counter() - t_save, "json_page_save_pages")
            except Exception as e:
                print(f"[Save Callback Error] Page {page_num}: {str(e)}", file=sys.stderr, flush=True)
        
        if progress_callback:
            progress_callback(
                page_num, total_pages, f"Completed page {page_num} of {total_pages}…"
            )


def _invoke_save_callback(save_callback, page_num, page_data) -> None:
    """Run the per-page save callback (with profiling) for streaming OCR.

    Unlike the batch assembly path this does NOT swallow errors: when a caller streams
    pages to storage, a failed page upload must surface so the file can be marked failed
    instead of silently completing with a missing page.
    """
    if not save_callback:
        return
    prof = _active_upload_profile()
    t_save = time.perf_counter()
    save_callback(page_num, page_data)
    if prof:
        prof.add("json_page_save_callback", time.perf_counter() - t_save, "json_page_save_pages")


def _raise_if_consecutive_empty(empties: list) -> None:
    """Apply the 10-consecutive-empty-page rule over per-page emptiness flags.

    Lets the streaming path keep only one bool per page (instead of every page's OCR
    data) while preserving the exact detection behaviour of the batch assembler.
    """
    import sys

    consecutive_empty_count = 0
    first_empty_page = None
    for i, is_empty in enumerate(empties):
        page_num = i + 1
        if is_empty:
            if consecutive_empty_count == 0:
                first_empty_page = page_num
            consecutive_empty_count += 1
            print(
                f"[Empty Page Detection] Page {page_num} is empty (consecutive count: {consecutive_empty_count})",
                file=sys.stderr,
                flush=True,
            )
            if consecutive_empty_count >= 10:
                error_msg = (
                    f"OCR processing paused: 10 consecutive empty pages detected (pages {first_empty_page} to {page_num}). "
                    "This may indicate an issue with the PDF or OCR service."
                )
                print(f"[Empty Page Detection] ERROR: {error_msg}", file=sys.stderr, flush=True)
                raise EmptyPagesError(first_empty_page, page_num, error_msg)
        else:
            consecutive_empty_count = 0
            first_empty_page = None


def _vision_async_timeout_seconds() -> int:
    raw = (os.environ.get("VISION_ASYNC_TIMEOUT_SECONDS") or "").strip()
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    return 3600


def _gcs_object_url(bucket: str, object_name: str) -> str:
    return f"{_GCS_BASE_URL}/b/{urllib.parse.quote(bucket, safe='')}/o/{urllib.parse.quote(object_name, safe='')}"


class GCSOperationError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def _raise_gcs_error(response, action: str) -> None:
    if response.ok:
        return
    detail = (getattr(response, "text", "") or "").strip()
    if len(detail) > 1000:
        detail = detail[:1000] + "..."
    raise GCSOperationError(
        f"GCS {action} failed ({response.status_code}): {detail or response.reason}",
        getattr(response, "status_code", None),
    )


def _is_retryable_gcs_exception(exc: BaseException) -> bool:
    if isinstance(exc, GCSOperationError):
        return exc.status_code in _GCS_RETRY_STATUSES
    if isinstance(exc, requests.RequestException):
        return True
    try:
        from google.api_core import exceptions as gcp_exc

        return isinstance(
            exc,
            (
                gcp_exc.ServiceUnavailable,
                gcp_exc.TooManyRequests,
                gcp_exc.InternalServerError,
                gcp_exc.GatewayTimeout,
                gcp_exc.DeadlineExceeded,
            ),
        )
    except Exception:
        return False


def _gcs_client_error(exc: BaseException, action: str) -> GCSOperationError:
    if isinstance(exc, GCSOperationError):
        return exc
    status_code = 500
    code = getattr(exc, "code", None)
    if isinstance(code, int):
        status_code = code
    return GCSOperationError(f"GCS {action} failed: {exc}", status_code)


def _gcs_storage_client():
    path = get_credentials_file_path()
    from google.cloud import storage

    creds = service_account.Credentials.from_service_account_file(
        path,
        scopes=_GCP_CLOUD_PLATFORM_SCOPES,
    )
    return storage.Client(credentials=creds)


def _unlink_temp_files(paths: list[str] | tuple[str, ...] | None) -> None:
    if not paths:
        return
    for path in paths:
        if not path:
            continue
        for candidate in (path, f"{path}.part", f"{path}.lin.pdf"):
            if os.path.exists(candidate):
                try:
                    os.remove(candidate)
                except OSError:
                    pass


def _gcs_upload_pdf_file(bucket: str, object_name: str, local_path: str) -> int:
    """Stream a local PDF file to GCS via blob.open('wb'). Returns bytes uploaded."""

    def _attempt():
        client = _gcs_storage_client()
        blob = client.bucket(bucket).blob(object_name)
        blob.content_type = "application/pdf"
        bytes_uploaded = 0
        try:
            with blob.open("wb", chunk_size=_STORAGE_STREAM_CHUNK) as writer:
                with open(local_path, "rb") as reader:
                    while True:
                        chunk = reader.read(_STORAGE_STREAM_CHUNK)
                        if not chunk:
                            break
                        writer.write(chunk)
                        bytes_uploaded += len(chunk)
        except Exception as exc:
            raise _gcs_client_error(exc, f"upload to gs://{bucket}/{object_name}") from exc
        return bytes_uploaded

    return _with_gcs_retries(f"upload gs://{bucket}/{object_name}", _attempt)


def _with_gcs_retries(action: str, fn):
    last_exc = None
    for attempt in range(1, _GCS_MAX_ATTEMPTS + 1):
        try:
            return fn()
        except Exception as exc:
            last_exc = exc
            if attempt >= _GCS_MAX_ATTEMPTS or not _is_retryable_gcs_exception(exc):
                raise
            delay = min(8, 2 ** (attempt - 1))
            print(
                f"[Vision Async OCR] transient {action} failure; retrying "
                f"({attempt + 1}/{_GCS_MAX_ATTEMPTS}) after {delay}s: {exc}",
                file=sys.stderr,
                flush=True,
            )
            time.sleep(delay)
    raise last_exc


def _is_retryable_vision_operation_exception(exc: BaseException) -> bool:
    from google.api_core import exceptions as google_exceptions

    retryable_types = tuple(
        t
        for t in (
            getattr(google_exceptions, "Aborted", None),
            getattr(google_exceptions, "DeadlineExceeded", None),
            getattr(google_exceptions, "InternalServerError", None),
            getattr(google_exceptions, "ResourceExhausted", None),
            getattr(google_exceptions, "ServiceUnavailable", None),
            getattr(google_exceptions, "TooManyRequests", None),
        )
        if t is not None
    )
    return isinstance(exc, retryable_types)


def _wait_for_vision_operation(operation, timeout_seconds: int):
    deadline = time.monotonic() + timeout_seconds
    last_exc = None
    for attempt in range(1, _VISION_OPERATION_MAX_ATTEMPTS + 1):
        remaining = max(1, int(deadline - time.monotonic()))
        try:
            return operation.result(timeout=remaining)
        except Exception as exc:
            last_exc = exc
            if (
                attempt >= _VISION_OPERATION_MAX_ATTEMPTS
                or time.monotonic() >= deadline
                or not _is_retryable_vision_operation_exception(exc)
            ):
                raise
            delay = min(15, 2 ** (attempt - 1))
            print(
                f"[Vision Async OCR] transient Vision operation polling failure; retrying "
                f"({attempt + 1}/{_VISION_OPERATION_MAX_ATTEMPTS}) after {delay}s: {exc}",
                file=sys.stderr,
                flush=True,
            )
            time.sleep(delay)
    raise last_exc


def _with_vision_retries(action: str, fn):
    last_exc = None
    for attempt in range(1, _VISION_OPERATION_MAX_ATTEMPTS + 1):
        try:
            return fn()
        except Exception as exc:
            last_exc = exc
            if attempt >= _VISION_OPERATION_MAX_ATTEMPTS or not _is_retryable_vision_operation_exception(exc):
                raise
            delay = min(15, 2 ** (attempt - 1))
            print(
                f"[Vision Async OCR] transient {action} failure; retrying "
                f"({attempt + 1}/{_VISION_OPERATION_MAX_ATTEMPTS}) after {delay}s: {exc}",
                file=sys.stderr,
                flush=True,
            )
            time.sleep(delay)
    raise last_exc


def _vision_async_progress(progress_callback, completed: int, total: int, message: str) -> None:
    print(f"[Vision Async OCR] {message}", file=sys.stderr, flush=True)
    if progress_callback:
        progress_callback(completed, total, message)


def _vision_async_clients():
    path = get_credentials_file_path()
    from google.auth.transport.requests import AuthorizedSession

    creds = service_account.Credentials.from_service_account_file(
        path,
        scopes=_GCP_CLOUD_PLATFORM_SCOPES,
    )
    return vision.ImageAnnotatorClient(credentials=creds), AuthorizedSession(creds)


def _gcs_upload_pdf_bytes(session_obj, bucket: str, object_name: str, pdf_bytes: bytes) -> None:
    def _attempt():
        url = f"{_GCS_UPLOAD_URL}/b/{urllib.parse.quote(bucket, safe='')}/o"
        response = session_obj.post(
            url,
            params={"uploadType": "media", "name": object_name},
            headers={"Content-Type": "application/pdf"},
            data=pdf_bytes,
        )
        _raise_gcs_error(response, f"upload to gs://{bucket}/{object_name}")

    return _with_gcs_retries(f"upload gs://{bucket}/{object_name}", _attempt)


def _gcs_list_objects(session_obj, bucket: str, prefix: str) -> list[str]:
    def _attempt():
        url = f"{_GCS_BASE_URL}/b/{urllib.parse.quote(bucket, safe='')}/o"
        names: list[str] = []
        page_token: str | None = None
        while True:
            params = {"prefix": prefix}
            if page_token:
                params["pageToken"] = page_token
            response = session_obj.get(url, params=params)
            _raise_gcs_error(response, f"list gs://{bucket}/{prefix}")
            payload = response.json()
            names.extend(item["name"] for item in payload.get("items", []) if item.get("name"))
            page_token = payload.get("nextPageToken")
            if not page_token:
                return names

    return _with_gcs_retries(f"list gs://{bucket}/{prefix}", _attempt)


def _gcs_download_object_to_file(session_obj, bucket: str, object_name: str, local_path: str) -> None:
    part_path = f"{local_path}.part"

    def _attempt():
        try:
            if os.path.exists(part_path):
                os.remove(part_path)
        except OSError:
            pass
        response = session_obj.get(_gcs_object_url(bucket, object_name), params={"alt": "media"}, stream=True)
        try:
            _raise_gcs_error(response, f"download gs://{bucket}/{object_name}")
            with open(part_path, "wb") as out:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        out.write(chunk)
            os.replace(part_path, local_path)
        finally:
            response.close()

    try:
        return _with_gcs_retries(f"download gs://{bucket}/{object_name}", _attempt)
    finally:
        try:
            if os.path.exists(part_path):
                os.remove(part_path)
        except OSError:
            pass


def _gcs_delete_object(session_obj, bucket: str, object_name: str) -> None:
    def _attempt():
        response = session_obj.delete(_gcs_object_url(bucket, object_name))
        if response.status_code == 404:
            return
        _raise_gcs_error(response, f"delete gs://{bucket}/{object_name}")

    return _with_gcs_retries(f"delete gs://{bucket}/{object_name}", _attempt)


def _best_effort_delete_gcs_object(session_obj, bucket: str, object_name: str) -> None:
    try:
        _gcs_delete_object(session_obj, bucket, object_name)
    except Exception as e:
        print(f"[Vision Async OCR] cleanup failed for gs://{bucket}/{object_name}: {e}", file=sys.stderr, flush=True)


def _best_effort_delete_gcs_prefix(session_obj, bucket: str, prefix: str) -> None:
    try:
        for object_name in _gcs_list_objects(session_obj, bucket, prefix):
            _best_effort_delete_gcs_object(session_obj, bucket, object_name)
    except Exception as e:
        print(f"[Vision Async OCR] cleanup list failed for gs://{bucket}/{prefix}: {e}", file=sys.stderr, flush=True)


def _vision_output_page_range(object_name: str) -> tuple[int, int]:
    base = object_name.rsplit("/", 1)[-1]
    m = re.search(r"output-(\d+)-to-(\d+)\.json$", base, re.I)
    if not m:
        raise RuntimeError(
            f"Unexpected Google Vision output filename: {base!r}; expected output-START-to-END.json."
        )
    start_page = int(m.group(1))
    end_page = int(m.group(2))
    if start_page < 1 or end_page < start_page:
        raise RuntimeError(f"Invalid Google Vision output page range in filename: {base!r}.")
    return start_page, end_page


def _vision_output_sort_key(object_name: str):
    start_page, end_page = _vision_output_page_range(object_name)
    return (start_page, end_page, object_name)


def _validate_vision_output_sequence(
    object_names: list[str], total_pages: int, expected_start: int = 1
) -> None:
    """Validate Vision output batches cover expected_start..total_pages contiguously."""
    cursor = int(expected_start)
    for object_name in object_names:
        start_page, end_page = _vision_output_page_range(object_name)
        if start_page != cursor:
            raise RuntimeError(
                "Google Vision output batches are not contiguous: "
                f"expected page {cursor}, got {start_page}-{end_page} in {object_name}."
            )
        cursor = end_page + 1
    if cursor != total_pages + 1:
        raise RuntimeError(
            f"Google Vision output batches covered pages {expected_start}-{cursor - 1}, "
            f"expected {expected_start}-{total_pages}."
        )


def _submit_async_pdf_ocr(vision_client, source_uri: str, output_uri: str):
    def _attempt():
        feature = vision.Feature(type_=vision.Feature.Type.DOCUMENT_TEXT_DETECTION)
        input_config = vision.InputConfig(
            gcs_source=vision.GcsSource(uri=source_uri),
            mime_type="application/pdf",
        )
        output_config = vision.OutputConfig(
            gcs_destination=vision.GcsDestination(uri=output_uri),
            batch_size=VISION_ASYNC_BATCH_SIZE,
        )
        request = vision.AsyncAnnotateFileRequest(
            features=[feature],
            input_config=input_config,
            output_config=output_config,
        )
        return vision_client.async_batch_annotate_files(requests=[request])

    return _with_vision_retries("Vision async OCR submit", _attempt)


def _vision_json_response_to_proto(response_json: dict):
    from google.protobuf.json_format import ParseDict

    pb_response = vision.AnnotateImageResponse.pb(vision.AnnotateImageResponse())
    ParseDict(response_json, pb_response, ignore_unknown_fields=True)
    response = vision.AnnotateImageResponse.wrap(pb_response)
    return _finalize_text_detection_response(response)


def _update_streaming_empty_page_state(page_num: int, page_data: dict, state: dict) -> None:
    if is_page_empty(page_data):
        if state["consecutive_empty_count"] == 0:
            state["first_empty_page"] = page_num
        state["consecutive_empty_count"] += 1
        print(
            f"[Empty Page Detection] Page {page_num} is empty "
            f"(consecutive count: {state['consecutive_empty_count']})",
            file=sys.stderr,
            flush=True,
        )
        if state["consecutive_empty_count"] >= 10:
            first_empty_page = state["first_empty_page"]
            error_msg = (
                f"OCR processing paused: 10 consecutive empty pages detected (pages {first_empty_page} to {page_num}). "
                "This may indicate an issue with the PDF or OCR service."
            )
            print(f"[Empty Page Detection] ERROR: {error_msg}", file=sys.stderr, flush=True)
            raise EmptyPagesError(first_empty_page, page_num, error_msg)
    else:
        state["consecutive_empty_count"] = 0
        state["first_empty_page"] = None


def _process_vision_async_output_file(
    local_json_path: str,
    batch_start_page: int,
    total_pages: int,
    result: dict,
    progress_callback,
    save_callback,
    empty_state: dict,
    resume_from_page: int = 1,
) -> int:
    """Convert one Vision output JSON into page saves.

    ``batch_start_page`` is the first page covered by this file (from the filename).
    Pages with number < ``resume_from_page`` are skipped (already uploaded) so mid-batch
    resume does not re-upload or require a contiguous GCS object start.
    Returns the next page number after the last response in this file.
    """
    with open(local_json_path, "r", encoding="utf-8") as f:
        payload = json.load(f)
    responses = payload.get("responses")
    if not isinstance(responses, list):
        raise RuntimeError(f"Vision output file {local_json_path} did not contain a responses list.")

    stream = save_callback is not None
    next_page_num = int(batch_start_page)
    for response_index, response_json in enumerate(responses):
        if not isinstance(response_json, dict):
            raise RuntimeError(f"Vision output file {local_json_path} contained an invalid response.")
        page_num = int(batch_start_page) + response_index
        next_page_num = page_num + 1
        if page_num < int(resume_from_page):
            responses[response_index] = None
            del response_json
            continue
        if page_num > total_pages:
            raise RuntimeError(
                f"Vision output file {local_json_path} produced page {page_num}, expected at most {total_pages}."
            )
        response = _vision_json_response_to_proto(response_json)
        page_data = _vision_response_to_page_data(response)
        try:
            _update_streaming_empty_page_state(page_num, page_data, empty_state)
            if stream:
                _invoke_save_callback(save_callback, page_num, page_data)
            else:
                result[f"page_{page_num}"] = page_data
            if progress_callback:
                progress_callback(page_num, total_pages, f"Completed page {page_num} of {total_pages}…")
        finally:
            if stream:
                del page_data
            del response
            responses[response_index] = None
            del response_json

    del payload
    del responses
    return next_page_num


def _vision_operation_name(operation) -> str:
    wrapped = getattr(operation, "operation", None)
    name = getattr(wrapped, "name", None) or getattr(operation, "name", None)
    if not name:
        raise RuntimeError("Google Vision did not return an operation name.")
    return str(name)


def _wait_for_named_vision_operation(vision_client, operation_name: str, timeout_seconds: int, on_poll=None):
    """Poll a long-running Vision operation by resource name (supports resume after restart)."""
    ops_client = vision_client.transport.operations_client
    deadline = time.monotonic() + timeout_seconds
    last_exc = None
    while time.monotonic() < deadline:
        if callable(on_poll):
            try:
                on_poll()
            except Exception:
                raise
        try:
            try:
                op = ops_client.get_operation(operation_name)
            except TypeError:
                op = ops_client.get_operation(request={"name": operation_name})
            if getattr(op, "done", False):
                err = getattr(op, "error", None)
                if err and getattr(err, "code", 0):
                    raise RuntimeError(getattr(err, "message", None) or "Vision operation failed")
                return op
            last_exc = None
        except RuntimeError:
            raise
        except Exception as exc:
            last_exc = exc
            if not _is_retryable_vision_operation_exception(exc):
                print(
                    f"[Vision Async OCR] operation poll error for {operation_name}: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
            else:
                print(
                    f"[Vision Async OCR] operation poll retry for {operation_name}: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
        time.sleep(5)
    if last_exc:
        raise TimeoutError(
            f"Timed out waiting for Vision operation {operation_name}: {last_exc}"
        ) from last_exc
    raise TimeoutError(f"Timed out waiting for Vision operation {operation_name}")


def _start_async_pdf_ocr_job(
    total_pages: int,
    progress_callback=None,
    *,
    pdf_bytes: bytes | None = None,
    pdf_path: str | None = None,
) -> dict:
    """Upload PDF to GCS and submit Vision async OCR. Returns durable job metadata."""
    if (pdf_bytes is None) == (pdf_path is None):
        raise ValueError("Exactly one of pdf_bytes or pdf_path must be provided.")
    if not VISION_ASYNC_GCS_BUCKET:
        raise RuntimeError("VISION_ASYNC_GCS_BUCKET must be configured for async PDF OCR.")

    prof = _active_upload_profile()
    run_id = uuid.uuid4().hex
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_prefix = f"vision_async_pdf/{timestamp}-{run_id}/"
    input_object = f"{run_prefix}input/document.pdf"
    output_prefix = f"{run_prefix}output/"
    source_uri = f"gs://{VISION_ASYNC_GCS_BUCKET}/{input_object}"
    output_uri = f"gs://{VISION_ASYNC_GCS_BUCKET}/{output_prefix}"

    _vision_async_progress(
        progress_callback,
        0,
        total_pages,
        f"Starting async PDF OCR: {total_pages} page(s), Vision batch size {VISION_ASYNC_BATCH_SIZE}…",
    )

    vision_client, storage_session = _vision_async_clients()
    try:
        t_upload = time.perf_counter()
        _vision_async_progress(progress_callback, 0, total_pages, "Uploading PDF to Google Cloud Storage…")
        if pdf_path is not None:
            _log_mem("before_stream_upload")
            bytes_uploaded = _gcs_upload_pdf_file(VISION_ASYNC_GCS_BUCKET, input_object, pdf_path)
            elapsed = time.perf_counter() - t_upload
            _log_mem("after_stream_upload")
            if prof:
                avg_mbps = (bytes_uploaded / (1024 * 1024)) / elapsed if elapsed > 0 else 0.0
                prof.log_step(
                    "stream_supabase_to_gcs",
                    elapsed,
                    bytes=bytes_uploaded,
                    avg_mbps=f"{avg_mbps:.2f}",
                )
                prof.log_step("vision_async_gcs_upload", elapsed, bytes=bytes_uploaded)
        else:
            _gcs_upload_pdf_bytes(storage_session, VISION_ASYNC_GCS_BUCKET, input_object, pdf_bytes)
            if prof:
                prof.log_step("vision_async_gcs_upload", time.perf_counter() - t_upload, bytes=len(pdf_bytes))

        t_submit = time.perf_counter()
        _log_mem("vision_async_before_request")
        _vision_async_progress(progress_callback, 0, total_pages, "Starting Google Vision async OCR job…")
        operation = _submit_async_pdf_ocr(vision_client, source_uri, output_uri)
        operation_name = _vision_operation_name(operation)
        if prof:
            prof.log_step("vision_async_submit", time.perf_counter() - t_submit)

        return {
            "status": "processing",
            "operation_name": operation_name,
            "gcs_bucket": VISION_ASYNC_GCS_BUCKET,
            "gcs_input_object": input_object,
            "gcs_output_prefix": output_prefix,
            "run_prefix": run_prefix,
            "source_uri": source_uri,
            "output_uri": output_uri,
            "stage": "waiting_vision",
            "pages_done": 0,
            "total_pages": int(total_pages),
            "started_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "error": None,
            "lease_owner": None,
            "lease_expires": None,
            "retry_count": 0,
        }
    except Exception:
        _best_effort_delete_gcs_prefix(storage_session, VISION_ASYNC_GCS_BUCKET, run_prefix)
        raise


def _validate_vision_output_covers(
    object_names: list[str], total_pages: int, resume_from_page: int
) -> None:
    """Validate remaining Vision batches cover resume_from_page..total_pages.

    The first batch may start before resume_from_page (mid-batch resume); later batches
    must be contiguous through total_pages.
    """
    if not object_names:
        raise RuntimeError("Google Vision async OCR produced no remaining JSON output batches.")
    first_start, first_end = _vision_output_page_range(object_names[0])
    if first_end < resume_from_page:
        raise RuntimeError(
            f"Google Vision output batches do not cover resume page {resume_from_page} "
            f"(first remaining batch is {first_start}-{first_end})."
        )
    if first_start > resume_from_page:
        raise RuntimeError(
            f"Google Vision output has a gap before page {resume_from_page} "
            f"(first remaining batch starts at {first_start})."
        )
    cursor = first_start
    for object_name in object_names:
        start_page, end_page = _vision_output_page_range(object_name)
        if start_page != cursor:
            raise RuntimeError(
                "Google Vision output batches are not contiguous: "
                f"expected page {cursor}, got {start_page}-{end_page} in {object_name}."
            )
        cursor = end_page + 1
    if cursor != total_pages + 1:
        raise RuntimeError(
            f"Google Vision output batches covered through page {cursor - 1}, expected {total_pages}."
        )


def _finish_async_pdf_ocr_job(job: dict, progress_callback=None, save_callback=None) -> dict:
    """Wait for a started Vision job, then stream page JSON through save_callback.

    Supports resume: when job['pages_done'] > 0, already-finished pages are skipped
    (including mid-batch), and processing continues from the next page. Never submits a
    new Vision operation — always polls the existing operation_name.
    """
    bucket = (job.get("gcs_bucket") or VISION_ASYNC_GCS_BUCKET or "").strip()
    output_prefix = (
        (job.get("gcs_output_prefix") or job.get("output_prefix") or "").strip()
    )
    input_object = (job.get("gcs_input_object") or job.get("input_object") or "").strip()
    run_prefix = (job.get("run_prefix") or "").strip()
    if not run_prefix and input_object and "/input/" in input_object:
        run_prefix = input_object.split("/input/", 1)[0].rstrip("/") + "/"
    operation_name = (job.get("operation_name") or "").strip()
    total_pages = int(job.get("total_pages") or 0)
    pages_done = max(0, int(job.get("pages_done") or 0))
    if not bucket or not output_prefix or not operation_name or total_pages < 1:
        raise RuntimeError("Invalid OCR job metadata; cannot finish async PDF OCR.")

    prof = _active_upload_profile()
    result: dict = {}
    local_paths: set[str] = set()
    vision_client, storage_session = _vision_async_clients()
    output_uri = job.get("output_uri") or f"gs://{bucket}/{output_prefix}"
    finished_ok = False

    def _set_stage(stage: str) -> None:
        job["stage"] = stage
        on_progress = job.get("_on_progress")
        if callable(on_progress):
            try:
                on_progress(job)
            except Exception as cb_exc:
                print(
                    f"[Vision Async OCR] progress callback failed: {cb_exc}",
                    file=sys.stderr,
                    flush=True,
                )

    def _renew_lease_or_raise() -> None:
        on_renew = job.get("_on_lease_renew")
        if callable(on_renew):
            on_renew()

    try:
        if pages_done >= total_pages:
            job["pages_done"] = total_pages
            _set_stage("complete")
            _vision_async_progress(progress_callback, total_pages, total_pages, "Finished async PDF OCR.")
            finished_ok = True
            return result

        t_ocr = time.perf_counter()
        _set_stage("waiting_vision")
        _vision_async_progress(
            progress_callback, pages_done, total_pages, "Waiting for Google OCR to complete…"
        )
        _wait_for_named_vision_operation(
            vision_client,
            operation_name,
            _vision_async_timeout_seconds(),
            on_poll=_renew_lease_or_raise,
        )
        _log_mem("vision_async_after_vision_complete")
        if prof:
            prof.log_step("vision_async_operation_wait", time.perf_counter() - t_ocr, pages=total_pages)

        _set_stage("downloading")
        _vision_async_progress(progress_callback, pages_done, total_pages, "Downloading OCR results…")
        all_output_objects = sorted(
            [
                name
                for name in _gcs_list_objects(storage_session, bucket, output_prefix)
                if name.lower().endswith(".json")
            ],
            key=_vision_output_sort_key,
        )
        resume_from_page = pages_done + 1
        # Keep any batch that still contains pages we need (supports mid-batch resume).
        output_objects = [
            name
            for name in all_output_objects
            if _vision_output_page_range(name)[1] >= resume_from_page
        ]
        if not output_objects:
            if pages_done >= total_pages:
                _set_stage("complete")
                finished_ok = True
                return result
            raise RuntimeError(f"Google Vision async OCR produced no JSON output under {output_uri}.")
        _validate_vision_output_covers(output_objects, total_pages, resume_from_page)

        empty_state = {"consecutive_empty_count": 0, "first_empty_page": None}
        next_page_num = resume_from_page
        with tempfile.TemporaryDirectory(prefix="vision_async_pdf_") as tmp_dir:
            total_batches = len(output_objects)
            for batch_index, object_name in enumerate(output_objects, start=1):
                batch_start_page, _batch_end_page = _vision_output_page_range(object_name)
                local_json_path = os.path.join(
                    tmp_dir, object_name.rsplit("/", 1)[-1] or f"output_{batch_start_page}.json"
                )
                local_paths.add(local_json_path)
                try:
                    _log_mem(f"vision_async_before_download_batch_{batch_index}")
                    _set_stage("downloading")
                    _renew_lease_or_raise()
                    _vision_async_progress(
                        progress_callback,
                        max(0, next_page_num - 1),
                        total_pages,
                        f"Downloading OCR results batch {batch_index} of {total_batches}…",
                    )
                    t_download = time.perf_counter()
                    _gcs_download_object_to_file(storage_session, bucket, object_name, local_json_path)
                    if prof:
                        prof.add(
                            "vision_async_json_download",
                            time.perf_counter() - t_download,
                            "vision_async_json_files",
                        )
                    _set_stage("processing_pages")
                    _vision_async_progress(
                        progress_callback,
                        max(0, next_page_num - 1),
                        total_pages,
                        f"Processing OCR pages starting at page {next_page_num}…",
                    )
                    next_page_num = _process_vision_async_output_file(
                        local_json_path,
                        batch_start_page,
                        total_pages,
                        result,
                        progress_callback,
                        save_callback,
                        empty_state,
                        resume_from_page=resume_from_page,
                    )
                    job["pages_done"] = next_page_num - 1
                    resume_from_page = next_page_num
                    if callable(job.get("_on_pages_done")):
                        try:
                            job["_on_pages_done"](next_page_num - 1, total_pages)
                        except Exception as cb_exc:
                            print(
                                f"[Vision Async OCR] pages_done callback failed: {cb_exc}",
                                file=sys.stderr,
                                flush=True,
                            )
                    _log_mem(
                        f"vision_async_after_processing_batch_{batch_index} "
                        f"pages_through={next_page_num - 1}"
                    )
                finally:
                    try:
                        if os.path.exists(local_json_path):
                            os.remove(local_json_path)
                    finally:
                        local_paths.discard(local_json_path)
                    _best_effort_delete_gcs_object(storage_session, bucket, object_name)
                    gc.collect()
                    _log_mem(f"vision_async_after_delete_batch_{batch_index}")

        processed_pages = next_page_num - 1
        if processed_pages != total_pages:
            raise RuntimeError(
                f"Google Vision async OCR returned {processed_pages} page response(s), expected {total_pages}."
            )

        job["pages_done"] = total_pages
        _set_stage("complete")
        _vision_async_progress(progress_callback, total_pages, total_pages, "Finished async PDF OCR.")
        _log_mem("vision_async_complete")
        finished_ok = True
        return result
    finally:
        for local_path in list(local_paths):
            try:
                if os.path.exists(local_path):
                    os.remove(local_path)
            except OSError:
                pass
        # Success: delete temp GCS prefix. Failure: leave it for resume; terminal
        # failure cleanup is handled in _mark_ocr_job_failed / _cleanup_ocr_job_gcs.
        if finished_ok and run_prefix:
            _best_effort_delete_gcs_prefix(storage_session, bucket, run_prefix)
        gc.collect()
        _log_mem("vision_async_after_final_cleanup")


def _extract_text_with_async_pdf_ocr(pdf_bytes, total_pages: int, progress_callback=None, save_callback=None):
    """Synchronous wrapper used by legacy routes: start Vision job, then finish streaming."""
    job = _start_async_pdf_ocr_job(
        total_pages,
        progress_callback=progress_callback,
        pdf_bytes=pdf_bytes,
    )
    return _finish_async_pdf_ocr_job(job, progress_callback=progress_callback, save_callback=save_callback)


def extract_text_with_locations(pdf_bytes, progress_callback=None, save_callback=None):
    """
    Extract text with location data per PDF page using Vision async PDF OCR.

    Streaming mode: when `save_callback` is provided, each page is handed to the callback and
    released immediately (never accumulated), so peak memory stays flat regardless of page
    count. The returned dict is empty in this mode. When `save_callback` is None the full
    per-page result dict is returned (used by the legacy /pdf and /pdf-json routes).
    """
    import sys

    prof = _active_upload_profile()
    t_meta = time.perf_counter()
    _log_mem("before_open_pdf_page_count")
    meta = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        total_pages = len(meta)
    finally:
        meta.close()
    _log_mem("after_page_count")
    if prof:
        prof.log_step("pdf_parse_page_count", time.perf_counter() - t_meta, pages=total_pages)

    return _extract_text_with_async_pdf_ocr(
        pdf_bytes,
        total_pages,
        progress_callback=progress_callback,
        save_callback=save_callback,
    )


def _is_superadmin_email(email: str | None) -> bool:
    if not email:
        return False
    needle = email.strip().lower()
    return needle in {e.strip().lower() for e in SUPERADMINS if e}


def _admin_view_as_user_id_from_request() -> str | None:
    """Return target user id when a verified superadmin sends X-Admin-View-As (GET only)."""
    try:
        from flask import has_request_context

        if not has_request_context():
            return None
    except Exception:
        return None
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        return None
    view_as = (request.headers.get("X-Admin-View-As") or "").strip()
    if not view_as:
        return None
    token = _bearer_token_from_request()
    if not token:
        return None
    actor = _auth_user_from_access_token(token)
    if not actor or not _is_superadmin_email(actor.get("email")):
        return None
    return view_as


def supabase_access_token_to_user_id(access_token: str) -> str | None:
    """Resolve Supabase Auth user id from a browser JWT (Bearer).

    When a superadmin sends X-Admin-View-As on a safe/read request, returns that
    target user id so read APIs can render the target account (read-only).
    """
    user = _auth_user_from_access_token(access_token)
    if not user:
        return None
    view_as = _admin_view_as_user_id_from_request()
    if view_as:
        return view_as
    return user.get("id")


def _auth_user_from_access_token(access_token: str) -> dict | None:
    """Return {id, email} from a Supabase access token, or None."""
    if not access_token or not supabase_url:
        return None
    jwt_secret = os.environ.get("SUPABASE_JWT_SECRET")
    if jwt_secret:
        try:
            payload = jwt.decode(
                access_token,
                jwt_secret,
                algorithms=["HS256"],
                audience="authenticated",
                options={"verify_exp": True},
            )
            uid = payload.get("sub")
            if uid:
                email = payload.get("email")
                return {"id": str(uid), "email": (email or "").strip() or None}
        except jwt.PyJWTError:
            pass
    api_key = supabase_secret_key or supabase_publishable_key
    if not api_key:
        return None
    try:
        url = f"{supabase_url.rstrip('/')}/auth/v1/user"
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {access_token}",
                "apikey": api_key,
            },
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = json.loads(resp.read().decode())
        uid = body.get("id")
        if not uid:
            return None
        email = (body.get("email") or "").strip() or None
        return {"id": str(uid), "email": email}
    except Exception as e:
        print(f"[auth] resolve user from token failed: {e}", file=sys.stderr, flush=True)
        return None


def _auth_email_from_access_token(access_token: str) -> str | None:
    user = _auth_user_from_access_token(access_token)
    return user.get("email") if user else None


def download_from_gbucket(storage_path: str) -> bytes:
    if not supabase_client:
        raise RuntimeError("Supabase is not configured (missing URL or secret key).")
    return supabase_client.storage.from_(SUPABASE_STORAGE_BUCKET).download(storage_path)


def download_json_from_storage(storage_path: str) -> bytes:
    if not supabase_client:
        raise RuntimeError("Supabase is not configured (missing URL or secret key).")
    return supabase_client.storage.from_(SUPABASE_JSON_BUCKET).download(storage_path)


def _storage_object_url(bucket: str, storage_path: str) -> str:
    encoded_path = urllib.parse.quote(storage_path.strip("/"), safe="/")
    return f"{supabase_url.rstrip('/')}/storage/v1/object/{bucket}/{encoded_path}"


def _storage_bucket_url(bucket: str) -> str:
    return f"{supabase_url.rstrip('/')}/storage/v1/object/{bucket}"


def _http_error_detail(exc: urllib.error.HTTPError) -> str:
    try:
        body = exc.read().decode("utf-8", "replace")
    except Exception:
        body = ""
    return f"HTTP {exc.code} {exc.reason}: {body}".strip()


def _storage_timeout_seconds(content_len: int, minimum: int = 120, maximum: int = 600) -> int:
    """Scale storage HTTP timeouts with payload size (large OCR JSON can be slow)."""
    per_mb = max(0, content_len // (1024 * 1024))
    return max(minimum, min(maximum, minimum + per_mb))


_storage_thread_local = threading.local()


def _storage_http_session() -> requests.Session:
    """Thread-local requests session with connection pooling for storage I/O."""
    sess = getattr(_storage_thread_local, "session", None)
    if sess is None:
        sess = requests.Session()
        adapter = HTTPAdapter(pool_connections=12, pool_maxsize=12)
        sess.mount("https://", adapter)
        sess.mount("http://", adapter)
        _storage_thread_local.session = sess
    return sess


def _storage_auth_headers(extra: dict | None = None) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {supabase_secret_key}",
        "apikey": supabase_secret_key,
    }
    if extra:
        headers.update(extra)
    return headers


def _storage_download(bucket: str, storage_path: str, timeout: int | None = None) -> bytes:
    if not supabase_url or not supabase_secret_key:
        raise RuntimeError("Supabase storage download requires SUPABASE_URL and SUPABASE_SECRET_KEY.")
    if not storage_path or storage_path.startswith("/") or ".." in storage_path:
        raise ValueError("Invalid storage path.")
    try:
        resp = _storage_http_session().get(
            _storage_object_url(bucket, storage_path),
            headers=_storage_auth_headers(),
            timeout=timeout or 300,
        )
        resp.raise_for_status()
        return resp.content
    except requests.RequestException as e:
        raise RuntimeError(
            f"Supabase Storage download failed for {storage_path} in bucket {bucket}: {e!s}"
        ) from e


def _storage_download_to_file(
    bucket: str, storage_path: str, local_path: str, timeout: int | None = None
) -> int:
    """Stream a Supabase Storage object to a local file. Returns bytes written."""
    if not supabase_url or not supabase_secret_key:
        raise RuntimeError("Supabase storage download requires SUPABASE_URL and SUPABASE_SECRET_KEY.")
    if not storage_path or storage_path.startswith("/") or ".." in storage_path:
        raise ValueError("Invalid storage path.")
    part_path = f"{local_path}.part"

    def _attempt() -> int:
        try:
            if os.path.exists(part_path):
                os.remove(part_path)
        except OSError:
            pass
        nbytes = 0
        resp = _storage_http_session().get(
            _storage_object_url(bucket, storage_path),
            headers=_storage_auth_headers(),
            timeout=timeout or 600,
            stream=True,
        )
        try:
            resp.raise_for_status()
            with open(part_path, "wb") as out:
                for chunk in resp.iter_content(chunk_size=_STORAGE_STREAM_CHUNK):
                    if chunk:
                        out.write(chunk)
                        nbytes += len(chunk)
            os.replace(part_path, local_path)
            return nbytes
        finally:
            resp.close()

    last_exc: Exception | None = None
    for attempt in range(1, _GCS_MAX_ATTEMPTS + 1):
        try:
            return _attempt()
        except requests.RequestException as exc:
            last_exc = exc
            if attempt >= _GCS_MAX_ATTEMPTS:
                break
            delay = min(2.0 ** (attempt - 1), 8.0)
            print(
                f"[storage] download retry for {storage_path} "
                f"({attempt + 1}/{_GCS_MAX_ATTEMPTS}) after {delay}s: {exc}",
                file=sys.stderr,
                flush=True,
            )
            time.sleep(delay)
        finally:
            try:
                if os.path.exists(part_path):
                    os.remove(part_path)
            except OSError:
                pass
    raise RuntimeError(
        f"Supabase Storage download failed for {storage_path} in bucket {bucket}: {last_exc!s}"
    ) from last_exc


def _storage_upload(
    bucket: str, storage_path: str, content: bytes, content_type: str = "application/octet-stream"
) -> None:
    """Upload/overwrite a private Supabase Storage object using the server secret key."""
    if not supabase_url or not supabase_secret_key:
        raise RuntimeError("Supabase storage upload requires SUPABASE_URL and SUPABASE_SECRET_KEY.")
    if not storage_path or storage_path.startswith("/") or ".." in storage_path:
        raise ValueError("Invalid storage path.")
    timeout = _storage_timeout_seconds(len(content))
    prof = _active_upload_profile()
    t0 = time.perf_counter()
    try:
        resp = _storage_http_session().post(
            _storage_object_url(bucket, storage_path),
            data=content,
            headers=_storage_auth_headers(
                {
                    "Content-Type": content_type,
                    "x-upsert": "true",
                }
            ),
            timeout=timeout,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        raise RuntimeError(
            f"Supabase Storage upload failed for {storage_path} in bucket {bucket} "
            f"(timeout {timeout}s, {len(content)} bytes): {e!s}"
        ) from e
    finally:
        if prof:
            metric = "json_storage_upload" if bucket == SUPABASE_JSON_BUCKET else "storage_upload"
            prof.add(metric, time.perf_counter() - t0, f"{metric}_ops")


def _storage_upload_file(
    bucket: str,
    storage_path: str,
    local_path: str,
    content_type: str = "application/octet-stream",
) -> int:
    """Stream a local file to Supabase Storage. Returns bytes uploaded."""
    if not supabase_url or not supabase_secret_key:
        raise RuntimeError("Supabase storage upload requires SUPABASE_URL and SUPABASE_SECRET_KEY.")
    if not storage_path or storage_path.startswith("/") or ".." in storage_path:
        raise ValueError("Invalid storage path.")
    nbytes = os.path.getsize(local_path)
    timeout = _storage_timeout_seconds(nbytes)
    prof = _active_upload_profile()
    t0 = time.perf_counter()
    try:
        with open(local_path, "rb") as src:
            resp = _storage_http_session().post(
                _storage_object_url(bucket, storage_path),
                data=src,
                headers=_storage_auth_headers(
                    {
                        "Content-Type": content_type,
                        "x-upsert": "true",
                    }
                ),
                timeout=timeout,
            )
        resp.raise_for_status()
        return nbytes
    except requests.RequestException as e:
        raise RuntimeError(
            f"Supabase Storage upload failed for {storage_path} in bucket {bucket} "
            f"(timeout {timeout}s, {nbytes} bytes): {e!s}"
        ) from e
    finally:
        if prof:
            metric = "json_storage_upload" if bucket == SUPABASE_JSON_BUCKET else "storage_upload"
            prof.add(metric, time.perf_counter() - t0, f"{metric}_ops")


def _storage_delete(bucket: str, storage_path: str) -> None:
    if not supabase_url or not supabase_secret_key or not storage_path:
        return
    try:
        resp = _storage_http_session().request(
            "DELETE",
            _storage_bucket_url(bucket),
            data=json.dumps({"prefixes": [storage_path]}),
            headers=_storage_auth_headers({"Content-Type": "application/json"}),
            timeout=30,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        print(
            f"[storage] delete failed for {storage_path} in {bucket}: {e!s}",
            file=sys.stderr,
            flush=True,
        )
    except Exception as e:
        print(f"[storage] delete failed for {storage_path} in {bucket}: {e}", file=sys.stderr, flush=True)


def _is_gzip_bytes(raw: bytes) -> bool:
    return len(raw) >= 2 and raw[0] == 0x1F and raw[1] == 0x8B


def _gzip_bytes(raw: bytes) -> bytes:
    return gzip.compress(raw, compresslevel=6)


def _decompress_json_bytes_maybe(raw: bytes) -> bytes:
    if _is_gzip_bytes(raw):
        return gzip.decompress(raw)
    return raw


def _parse_stored_json_bytes(raw: bytes, source: str = "storage") -> dict:
    try:
        payload = _decompress_json_bytes_maybe(raw)
        parsed = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, OSError) as e:
        raise ValueError(f"Stored JSON at {source} is not valid JSON.") from e
    if not isinstance(parsed, dict):
        raise ValueError(f"Stored JSON at {source} must be an object.")
    return parsed


def upload_to_gbucket(storage_path: str, content: bytes, content_type: str = "application/octet-stream") -> None:
    """Upload OCR JSON to storage; plain JSON payloads are gzip-compressed before upload."""
    prof = _active_upload_profile()
    if content_type == "application/json":
        t_gzip = time.perf_counter()
        content = _gzip_bytes(content)
        if prof:
            prof.add("json_gzip", time.perf_counter() - t_gzip, "json_gzip_ops")
        content_type = "application/gzip"
    _storage_upload(SUPABASE_JSON_BUCKET, storage_path, content, content_type)


def delete_from_gbucket(storage_path: str) -> None:
    _storage_delete(SUPABASE_JSON_BUCKET, storage_path)


def _json_kind_prefix(user_id: str, file_id: str, kind: str) -> str:
    """Storage folder prefix for per-page OCR JSON (original or editable)."""
    return f"{user_id}/{kind}/{file_id}"


def _json_page_storage_path(user_id: str, file_id: str, kind: str, page_num: int) -> str:
    return f"{user_id}/{kind}/{file_id}/page_{page_num}.json.gz"


def _page_json_path_from_prefix(prefix: str, page_num: int) -> str:
    clean = (prefix or "").strip().strip("/")
    if not clean or ".." in clean:
        raise ValueError("Invalid JSON storage prefix.")
    return f"{clean}/page_{page_num}.json.gz"


def _download_page_json_at_prefix(prefix: str, page_num: int) -> dict:
    path = _page_json_path_from_prefix(prefix, page_num)
    raw = download_json_from_storage(path)
    parsed = _parse_stored_json_bytes(raw, path)
    if not isinstance(parsed, dict):
        raise ValueError(f"Page JSON at {path} must be an object.")
    return parsed


def _upload_page_json_at_prefix(prefix: str, page_num: int, page_data: dict) -> None:
    prof = _active_upload_profile()
    t_ser = time.perf_counter()
    raw = _json_bytes_for_storage(page_data)
    if prof:
        prof.add("json_serialize", time.perf_counter() - t_ser, "json_serialize_ops")
    path = _page_json_path_from_prefix(prefix, page_num)
    upload_to_gbucket(path, raw, "application/json")


def _json_bytes_for_storage(data) -> bytes:
    return json.dumps(
        data,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")


def _upload_page_json(user_id: str, file_id: str, kind: str, page_num: int, page_data: dict) -> None:
    prof = _active_upload_profile()
    t_ser = time.perf_counter()
    raw = _json_bytes_for_storage(page_data)
    if prof:
        prof.add("json_serialize", time.perf_counter() - t_ser, "json_serialize_ops")
    path = _json_page_storage_path(user_id, file_id, kind, page_num)
    upload_to_gbucket(path, raw, "application/json")


def _download_page_json(user_id: str, file_id: str, kind: str, page_num: int) -> dict:
    path = _json_page_storage_path(user_id, file_id, kind, page_num)
    raw = download_json_from_storage(path)
    parsed = _parse_stored_json_bytes(raw, path)
    if not isinstance(parsed, dict):
        raise ValueError(f"Page JSON at {path} must be an object.")
    return parsed


def _storage_list(bucket: str, prefix: str, limit: int = 1000) -> list[dict]:
    if not supabase_url or not supabase_secret_key:
        raise RuntimeError("Supabase storage list requires SUPABASE_URL and SUPABASE_SECRET_KEY.")
    clean_prefix = (prefix or "").strip().strip("/")
    if ".." in clean_prefix:
        raise ValueError("Invalid storage prefix.")
    req = urllib.request.Request(
        f"{supabase_url.rstrip('/')}/storage/v1/object/list/{bucket}",
        data=json.dumps({"prefix": clean_prefix, "limit": limit, "sortBy": {"column": "name", "order": "asc"}}).encode(
            "utf-8"
        ),
        headers={
            "Authorization": f"Bearer {supabase_secret_key}",
            "apikey": supabase_secret_key,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Supabase Storage list failed for {clean_prefix}: {_http_error_detail(e)}") from e
    if isinstance(data, list):
        return data
    return []


_PAGE_FILE_RE = re.compile(r"^page_(\d+)\.json\.gz$", re.I)


def _page_numbers_from_prefix(prefix: str) -> list[int]:
    items = _storage_list(SUPABASE_JSON_BUCKET, prefix)
    nums: list[int] = []
    for item in items:
        name = (item.get("name") or "").strip()
        if not name:
            continue
        base = name.split("/")[-1]
        m = _PAGE_FILE_RE.match(base)
        if m:
            nums.append(int(m.group(1)))
    return sorted(set(nums))


def _delete_page_json_prefix(prefix: str) -> None:
    """Best-effort removal of every per-page JSON under a storage prefix.

    Lists the pages actually present and deletes each one; "not found" is a no-op.
    Never raises: individual failures are logged so callers can use this on error paths
    without masking the original error.
    """
    prefix = (prefix or "").strip()
    if not prefix:
        return
    try:
        nums = _page_numbers_from_prefix(prefix)
    except Exception as e:
        print(f"[cleanup] could not list pages under {prefix}: {e}", file=sys.stderr, flush=True)
        return
    for n in nums:
        try:
            delete_from_gbucket(_page_json_path_from_prefix(prefix, n))
        except Exception as e:
            print(f"[cleanup] failed to delete page {n} under {prefix}: {e}", file=sys.stderr, flush=True)


def _upload_bundle_page_task(task: tuple[str, str, str, int, dict]) -> None:
    user_id, file_id, kind, page_num, page_data = task
    _upload_page_json(user_id, file_id, kind, page_num, page_data)


def _upload_bundle_pages(user_id: str, file_id: str, original_data: dict, editable_data: dict) -> int:
    """Split monolithic import bundles into per-page gzip JSON files."""
    orig_keys = _ocr_json_page_keys(original_data)
    edit_keys = _ocr_json_page_keys(editable_data)
    if not orig_keys or not edit_keys:
        raise ValueError("Bundle JSON must contain page_N keys.")
    page_nums = sorted(
        {int(re.sub(r"\D", "", k)) for k in orig_keys} | {int(re.sub(r"\D", "", k)) for k in edit_keys}
    )
    tasks: list[tuple[str, str, str, int, dict]] = []
    for page_num in page_nums:
        ok = f"page_{page_num}"
        if ok not in original_data:
            raise ValueError(f"original JSON missing {ok}.")
        if ok not in editable_data:
            raise ValueError(f"editable JSON missing {ok}.")
        tasks.append((user_id, file_id, "original", page_num, original_data[ok]))
        tasks.append((user_id, file_id, "editable", page_num, editable_data[ok]))

    workers = _import_max_workers(len(page_nums))
    if workers <= 1 or len(tasks) <= 1:
        for task in tasks:
            _upload_bundle_page_task(task)
        return len(page_nums)

    errors: list[Exception] = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(_upload_bundle_page_task, task) for task in tasks]
        for fut in as_completed(futures):
            try:
                fut.result()
            except Exception as e:
                errors.append(e)
    if errors:
        raise errors[0]
    return len(page_nums)


def _upload_original_pages_parallel(user_id: str, file_id: str, ocr_result: dict) -> int:
    """Upload per-page original OCR JSON (.json.gz) concurrently after OCR completes.

    Copy-on-write: only the 'original' pages are written here. The editable copy is created
    lazily, one page at a time, the first time a user edits that page (see
    _save_editable_page_data), so a freshly processed file needs half as many uploads.
    """
    page_keys = _ocr_json_page_keys(ocr_result)
    if not page_keys:
        raise ValueError("OCR result has no page_N keys to store.")
    page_nums = sorted(int(re.sub(r"\D", "", k)) for k in page_keys)
    tasks = [(user_id, file_id, "original", n, ocr_result[f"page_{n}"]) for n in page_nums]

    workers = _import_max_workers(len(page_nums))
    if workers <= 1 or len(tasks) <= 1:
        for task in tasks:
            _upload_bundle_page_task(task)
        return len(page_nums)

    errors: list[Exception] = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(_upload_bundle_page_task, task) for task in tasks]
        for fut in as_completed(futures):
            try:
                fut.result()
            except Exception as e:
                errors.append(e)
    if errors:
        raise errors[0]
    return len(page_nums)


def _download_json_dicts_from_storage_parallel(
    original_json_path: str, editable_json_path: str
) -> tuple[dict, dict]:
    with ThreadPoolExecutor(max_workers=2) as ex:
        f_orig = ex.submit(_download_json_dict_from_storage_path, original_json_path)
        f_edit = ex.submit(_download_json_dict_from_storage_path, editable_json_path)
        return f_orig.result(), f_edit.result()


def _defer_import_temp_json_cleanup(*paths: str) -> None:
    """Delete temporary import JSON objects after the HTTP response returns."""
    to_delete = [p for p in paths if p]

    def _run() -> None:
        for path in to_delete:
            try:
                delete_from_gbucket(path)
            except Exception as e:
                print(f"[import-bundle] deferred cleanup failed for {path}: {e}", file=sys.stderr, flush=True)

    if to_delete:
        threading.Thread(target=_run, daemon=True).start()


def _row_uses_inline_json(row: dict) -> bool:
    """True when OCR JSON for a single-page file is stored inline in the files row."""
    if (row.get("editable_json_path") or "").strip() or (row.get("original_json_path") or "").strip():
        return False
    ej = _json_field_to_python(row.get("edited_json"))
    oj = _json_field_to_python(row.get("original_json"))
    return bool(isinstance(ej, dict) and ej) or bool(isinstance(oj, dict) and oj)


def _inline_page_data(row: dict, *, prefer_edited: bool = True) -> dict:
    if prefer_edited:
        ej = _json_field_to_python(row.get("edited_json"))
        if isinstance(ej, dict) and ej:
            return ej
    oj = _json_field_to_python(row.get("original_json"))
    if isinstance(oj, dict) and oj:
        return oj
    raise ValueError("No inline OCR JSON found for this file.")


def _json_row_fields_for_page_count(
    user_id: str,
    file_id: str,
    page_count: int,
    original_data: dict | None = None,
    editable_data: dict | None = None,
) -> dict:
    """Build files-table JSON columns for inline (1 page) or per-page storage prefixes."""
    if page_count == 1:
        if original_data is None or editable_data is None:
            raise ValueError("Single-page storage requires original and editable page data.")
        return {
            "original_json": original_data,
            "edited_json": editable_data,
            "original_json_path": None,
            "editable_json_path": None,
        }
    return {
        "original_json": None,
        "edited_json": None,
        "original_json_path": _json_kind_prefix(user_id, file_id, "original"),
        "editable_json_path": _json_kind_prefix(user_id, file_id, "editable"),
    }


def _store_bundle_json(user_id: str, file_id: str, original_data: dict, editable_data: dict) -> tuple[dict, int]:
    """Persist imported bundle JSON inline (1 page) or as per-page storage files."""
    page_keys = _ocr_json_page_keys(editable_data)
    if not page_keys:
        raise ValueError("editable JSON does not look like OCR output (expected page_N keys).")
    page_count = len(page_keys)
    if page_count == 1:
        page_key = page_keys[0]
        fields = _json_row_fields_for_page_count(
            user_id,
            file_id,
            1,
            original_data.get(page_key),
            editable_data.get(page_key),
        )
        return fields, page_count
    page_count = _upload_bundle_pages(user_id, file_id, original_data, editable_data)
    fields = _json_row_fields_for_page_count(user_id, file_id, page_count)
    return fields, page_count


def _page_count_for_row(row: dict) -> int:
    if _row_uses_inline_json(row):
        return 1
    # The original prefix always holds every page; the editable prefix is sparse under
    # copy-on-write (only edited pages), so count from original first.
    for key in ("original_json_path", "editable_json_path"):
        prefix = (row.get(key) or "").strip()
        if prefix:
            nums = _page_numbers_from_prefix(prefix)
            if nums:
                return len(nums)
    path = row.get("original_file_path")
    if path:
        try:
            raw = download_from_gbucket(path)
            _, page_count = _document_kind_and_page_count(raw)
            return page_count
        except Exception:
            pass
    return 0


def _download_json_dict_from_gbucket(storage_path: str) -> dict | None:
    raw = download_json_from_storage(storage_path)
    return _parse_stored_json_bytes(raw, storage_path)


def _download_json_dict_from_storage_path(storage_path: str) -> dict:
    raw = _storage_download(SUPABASE_JSON_BUCKET, storage_path)
    return _parse_stored_json_bytes(raw, storage_path)


_OCR_STAGE_LABELS = {
    "queued": "Queued",
    "uploading": "Uploading PDF",
    "waiting_vision": "Google OCR running",
    "downloading": "Downloading OCR results",
    "processing_pages": "Processing pages",
    "complete": "Complete",
    "failed": "Failed",
}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _public_job_metadata(metadata: dict | None) -> dict | None:
    """Drop in-process-only keys (callbacks) before writing job_metadata."""
    if metadata is None:
        return None
    if not isinstance(metadata, dict):
        return None
    return {k: v for k, v in metadata.items() if not str(k).startswith("_")}


def get_job_metadata(file_id: str) -> dict | None:
    """Read the files.job_metadata JSONB column for a file."""
    if not supabase_client or not file_id:
        return None
    try:
        res = (
            supabase_client.table("files")
            .select("job_metadata")
            .eq("id", str(file_id))
            .limit(1)
            .execute()
        )
    except Exception as e:
        print(f"[ocr-job] get_job_metadata failed for {file_id}: {e}", file=sys.stderr, flush=True)
        return None
    rows = getattr(res, "data", None) or []
    if not rows:
        return None
    meta = _json_field_to_python(rows[0].get("job_metadata"))
    return dict(meta) if isinstance(meta, dict) else None


def update_job_metadata(file_id: str, metadata: dict | None) -> None:
    """Replace files.job_metadata entirely (pass None to clear)."""
    if not supabase_client or not file_id:
        return
    payload = _public_job_metadata(metadata) if metadata is not None else None
    try:
        supabase_client.table("files").update({"job_metadata": payload}).eq("id", str(file_id)).execute()
    except Exception as e:
        print(f"[ocr-job] update_job_metadata failed for {file_id}: {e}", file=sys.stderr, flush=True)
        raise


def clear_job_metadata(file_id: str) -> None:
    """Set files.job_metadata to NULL."""
    update_job_metadata(file_id, None)


def merge_job_metadata(file_id: str, updates: dict) -> dict:
    """Merge updates into existing job_metadata and persist. Always refreshes updated_at."""
    current = get_job_metadata(file_id) or {}
    merged = {**current, **(_public_job_metadata(updates) or {})}
    merged["updated_at"] = _utc_now_iso()
    update_job_metadata(file_id, merged)
    return merged


def _job_metadata_from_row(row: dict | None) -> dict | None:
    if not isinstance(row, dict):
        return None
    meta = _json_field_to_python(row.get("job_metadata"))
    return dict(meta) if isinstance(meta, dict) else None


def _parse_iso_timestamp(value) -> datetime | None:
    if not value:
        return None
    try:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _contiguous_pages_done(page_nums: list[int]) -> int:
    have = set(page_nums or [])
    done = 0
    while (done + 1) in have:
        done += 1
    return done


def _ocr_progress_percent(job: dict, status: str | None = None) -> int:
    stage = (job.get("stage") or "").strip().lower()
    status_l = (status or job.get("status") or "").strip().lower()
    if status_l in ("completed", "complete") or stage == "complete":
        return 100
    if status_l == "failed" or stage == "failed":
        return 0
    pages_done = max(0, int(job.get("pages_done") or 0))
    total_pages = max(0, int(job.get("total_pages") or 0))
    if stage in ("queued",):
        return 2
    if stage == "uploading":
        return 8
    if stage == "waiting_vision":
        started = _parse_iso_timestamp(job.get("started_at"))
        if started and total_pages > 0:
            elapsed = max(0.0, (datetime.now(timezone.utc) - started).total_seconds())
            expected = max(30.0, total_pages * 0.25)
            return min(45, 10 + int(35 * min(1.0, elapsed / expected)))
        return 20
    if stage == "downloading":
        return 50
    if stage == "processing_pages" and total_pages > 0:
        return min(99, 50 + int(50 * (pages_done / total_pages)))
    if total_pages > 0 and pages_done > 0:
        return min(99, int(100 * (pages_done / total_pages)))
    return 5


def _cleanup_ocr_job_gcs(job: dict | None) -> None:
    """Best-effort delete of the temporary Vision GCS prefix for a job."""
    if not isinstance(job, dict):
        return
    bucket = (job.get("gcs_bucket") or VISION_ASYNC_GCS_BUCKET or "").strip()
    run_prefix = (job.get("run_prefix") or "").strip()
    input_object = (job.get("gcs_input_object") or "").strip()
    if not run_prefix and input_object and "/input/" in input_object:
        run_prefix = input_object.split("/input/", 1)[0].rstrip("/") + "/"
    if not bucket or not run_prefix:
        return
    try:
        _vision_client, storage_session = _vision_async_clients()
        _best_effort_delete_gcs_prefix(storage_session, bucket, run_prefix)
    except Exception as e:
        print(f"[ocr-job] GCS cleanup failed for prefix {run_prefix}: {e}", file=sys.stderr, flush=True)


def _refund_profile_pages(
    user_id: str,
    delta: int,
    *,
    free_used: int | None = None,
    paid_used: int | None = None,
) -> tuple[bool, str | None, int | None]:
    """Refund OCR credits. Prefer exact free/paid split when known; else restore free-first."""
    ok, err, balance = refund_ocr_credits(
        user_id, delta, free_used=free_used, paid_used=paid_used
    )
    total = balance.get("pages_remaining") if isinstance(balance, dict) else None
    return ok, err, total


def _mark_ocr_job_failed(
    file_id: str,
    user_id: str,
    job: dict | None,
    message: str,
    *,
    refund_pages: int | None = None,
) -> int | None:
    """Mark files.status=failed, keep failure details in job_metadata, refund credits if charged.

    Returns the user's new pages_remaining (free+paid) when a refund was applied, else None.
    Pass refund_pages=0 to skip refunding (e.g. credits were never consumed).
    """
    job = dict(job or {})
    now = _utc_now_iso()
    updates = {
        "status": "failed",
        "error": (message or "OCR failed")[:2000],
        "pages_done": int(job.get("pages_done") or 0),
        "total_pages": int(job.get("total_pages") or 0),
        "lease_owner": None,
        "lease_expires": None,
        "updated_at": now,
    }
    if job.get("stage") and str(job.get("stage")).strip().lower() not in ("complete", "failed"):
        updates["stage"] = job.get("stage")
    else:
        updates["stage"] = "failed"

    pages_remaining: int | None = None
    if not supabase_client:
        return None

    charge = 0
    if refund_pages is not None:
        charge = max(0, int(refund_pages))
    else:
        try:
            sel = (
                supabase_client.table("files")
                .select("credits_used")
                .eq("id", str(file_id))
                .eq("user_id", str(user_id))
                .in_("status", ["processing", "pending"])
                .limit(1)
                .execute()
            )
            rows = getattr(sel, "data", None) or []
            if rows:
                charge = max(0, int(rows[0].get("credits_used") or 0))
        except Exception as e:
            print(f"[ocr-job] could not read credits for refund {file_id}: {e}", file=sys.stderr, flush=True)

    free_used = job.get("credits_free_used")
    paid_used = job.get("credits_paid_used")
    try:
        free_used = int(free_used) if free_used is not None else None
    except (TypeError, ValueError):
        free_used = None
    try:
        paid_used = int(paid_used) if paid_used is not None else None
    except (TypeError, ValueError):
        paid_used = None

    try:
        fail_res = (
            supabase_client.table("files")
            .update({"status": "failed", "credits_used": 0})
            .eq("id", str(file_id))
            .eq("user_id", str(user_id))
            .in_("status", ["processing", "pending"])
            .execute()
        )
        failed_rows = getattr(fail_res, "data", None) or []
        if failed_rows and charge > 0:
            ok, err, pages_remaining = _refund_profile_pages(
                user_id, charge, free_used=free_used, paid_used=paid_used
            )
            if not ok:
                print(
                    f"[ocr-job] credit refund failed for {file_id} ({charge} pages): {err}",
                    file=sys.stderr,
                    flush=True,
                )
            else:
                print(
                    f"[ocr-job] refunded {charge} credit(s) to user {user_id} for failed file {file_id}",
                    file=sys.stderr,
                    flush=True,
                )
        merge_job_metadata(file_id, updates)
    except Exception as e:
        print(f"[ocr-job] failed to mark failed for {file_id}: {e}", file=sys.stderr, flush=True)
    _cleanup_ocr_job_gcs(job)
    return pages_remaining


def _mark_ocr_job_completed(
    file_id: str,
    user_id: str,
    *,
    original_json_prefix: str,
    total_pages: int,
    processing_duration_seconds: int,
    credits_used: int | None = None,
) -> None:
    if not supabase_client:
        return
    update_row = {
        "status": "completed",
        "job_metadata": None,
        "original_json": None,
        "original_json_path": original_json_prefix,
        "editable_json_path": original_json_prefix,
        "processing_duration_seconds": max(0, int(processing_duration_seconds)),
    }
    if credits_used is not None:
        update_row["credits_used"] = int(credits_used)
    try:
        supabase_client.table("files").update(update_row).eq("id", str(file_id)).eq(
            "user_id", str(user_id)
        ).execute()
    except Exception as e:
        print(f"[ocr-job] failed to mark completed for {file_id}: {e}", file=sys.stderr, flush=True)
        raise


def _try_acquire_ocr_lease(file_id: str, job: dict | None = None, *, renew_only: bool = False) -> bool:
    """Claim or renew the OCR job lease for this worker.

    Returns False if another worker holds an unexpired lease. After writing, re-reads
    job_metadata and confirms this worker still owns the lease (reduces dual-claim races).
    """
    now = datetime.now(timezone.utc)
    meta = dict(job or get_job_metadata(file_id) or {})
    owner = (meta.get("lease_owner") or "").strip()
    expires = _parse_iso_timestamp(meta.get("lease_expires"))
    if owner and owner != _OCR_WORKER_ID and expires and expires > now:
        return False
    if renew_only and owner and owner != _OCR_WORKER_ID:
        return False
    if renew_only and owner == _OCR_WORKER_ID and expires and expires > now:
        # Still ours; extend.
        pass
    lease_expires = (now + timedelta(seconds=_OCR_LEASE_SECONDS)).isoformat().replace("+00:00", "Z")
    if job is not None:
        job["lease_owner"] = _OCR_WORKER_ID
        job["lease_expires"] = lease_expires
    try:
        merge_job_metadata(
            file_id,
            {"lease_owner": _OCR_WORKER_ID, "lease_expires": lease_expires},
        )
        latest = get_job_metadata(file_id) or {}
        if (latest.get("lease_owner") or "").strip() != _OCR_WORKER_ID:
            return False
    except Exception as e:
        print(f"[ocr-job] lease acquire failed for {file_id}: {e}", file=sys.stderr, flush=True)
        return False
    return True


class OcrLeaseLostError(RuntimeError):
    """Raised when this worker no longer holds the OCR job lease."""


def _run_background_ocr_finish(file_id: str, user_id: str, job: dict | None = None) -> None:
    """Poll Vision, stream pages to storage, and finalize the files row."""
    job = dict(job or get_job_metadata(file_id) or {})
    total_pages = int(job.get("total_pages") or 0)
    original_json_prefix = (
        (job.get("original_json_prefix") or "").strip()
        or _json_kind_prefix(user_id, file_id, "original")
    )
    job["original_json_prefix"] = original_json_prefix
    started = _parse_iso_timestamp(job.get("started_at")) or datetime.now(timezone.utc)
    t0 = time.perf_counter()

    # Align pages_done with pages already uploaded (restart recovery).
    try:
        existing = _contiguous_pages_done(_page_numbers_from_prefix(original_json_prefix))
        job["pages_done"] = max(int(job.get("pages_done") or 0), existing)
    except Exception as e:
        print(f"[ocr-job] could not list existing pages for {file_id}: {e}", file=sys.stderr, flush=True)

    if total_pages > 0 and int(job.get("pages_done") or 0) >= total_pages:
        duration = max(0, round((datetime.now(timezone.utc) - started).total_seconds()))
        try:
            _mark_ocr_job_completed(
                file_id,
                user_id,
                original_json_prefix=original_json_prefix,
                total_pages=total_pages,
                processing_duration_seconds=duration,
                credits_used=total_pages,
            )
        except Exception:
            pass
        return

    if not _try_acquire_ocr_lease(file_id, job):
        print(f"[ocr-job] skip {file_id}: leased by another worker", file=sys.stderr, flush=True)
        return

    last_persist_pages = [int(job.get("pages_done") or 0)]
    last_stage = [job.get("stage")]

    def _on_lease_renew() -> None:
        # Renew while waiting on Vision / processing. If another worker holds the lease,
        # abort so we do not double-process — do not mark the job failed.
        if not _try_acquire_ocr_lease(file_id, job, renew_only=True):
            raise OcrLeaseLostError("Lost OCR job lease to another worker")

    def _on_progress(j: dict) -> None:
        stage = j.get("stage")
        pages_done = int(j.get("pages_done") or 0)
        if stage != last_stage[0] or pages_done - last_persist_pages[0] >= 10:
            last_stage[0] = stage
            last_persist_pages[0] = pages_done
            try:
                merge_job_metadata(
                    file_id,
                    {
                        "status": "processing",
                        "stage": stage,
                        "pages_done": pages_done,
                    },
                )
            except Exception as e:
                print(f"[ocr-job] progress merge failed for {file_id}: {e}", file=sys.stderr, flush=True)

    def _on_pages_done(done: int, _total: int) -> None:
        job["pages_done"] = int(done)
        job["stage"] = "processing_pages"
        last_persist_pages[0] = int(done)
        last_stage[0] = "processing_pages"
        try:
            merge_job_metadata(
                file_id,
                {
                    "status": "processing",
                    "stage": "processing_pages",
                    "pages_done": int(done),
                },
            )
            _on_lease_renew()
        except OcrLeaseLostError:
            raise
        except Exception as e:
            print(f"[ocr-job] pages_done merge failed for {file_id}: {e}", file=sys.stderr, flush=True)
            raise

    job["_on_progress"] = _on_progress
    job["_on_pages_done"] = _on_pages_done
    job["_on_lease_renew"] = _on_lease_renew

    def _save_original_page(page_num, page_data):
        _upload_page_json(user_id, file_id, "original", page_num, page_data)

    try:
        _finish_async_pdf_ocr_job(job, progress_callback=None, save_callback=_save_original_page)
        duration = max(
            0,
            round((datetime.now(timezone.utc) - started).total_seconds()),
            round(time.perf_counter() - t0),
        )
        _mark_ocr_job_completed(
            file_id,
            user_id,
            original_json_prefix=original_json_prefix,
            total_pages=total_pages,
            processing_duration_seconds=duration,
            credits_used=total_pages,
        )
        print(
            f"[ocr-job] completed file_id={file_id} pages={total_pages} duration_s={duration}",
            file=sys.stderr,
            flush=True,
        )
    except OcrLeaseLostError as e:
        print(f"[ocr-job] yielding file_id={file_id}: {e}", file=sys.stderr, flush=True)
        return
    except EmptyPagesError as e:
        print(f"[ocr-job] empty pages for {file_id}: {e}", file=sys.stderr, flush=True)
        try:
            _delete_page_json_prefix(original_json_prefix)
            _delete_page_json_prefix(_json_kind_prefix(user_id, file_id, "editable"))
        except Exception as cleanup_e:
            print(f"[ocr-job] orphan cleanup failed: {cleanup_e}", file=sys.stderr, flush=True)
        _mark_ocr_job_failed(file_id, user_id, job, str(e))
    except Exception as e:
        print(f"[ocr-job] failed file_id={file_id}: {e}", file=sys.stderr, flush=True)
        traceback.print_exc(file=sys.stderr)
        _mark_ocr_job_failed(file_id, user_id, job, str(e))


def _spawn_background_ocr_finish(file_id: str, user_id: str, job: dict | None = None) -> bool:
    fid = str(file_id)
    with _ocr_bg_lock:
        if fid in _ocr_bg_running:
            return False
        _ocr_bg_running.add(fid)

    def _run():
        try:
            _run_background_ocr_finish(fid, str(user_id), job)
        finally:
            with _ocr_bg_lock:
                _ocr_bg_running.discard(fid)

    threading.Thread(target=_run, name=f"ocr-finish-{fid[:8]}", daemon=True).start()
    return True


def _resume_orphaned_ocr_jobs() -> None:
    """After deploy/restart, resume processing files that still have Vision job_metadata."""
    if not supabase_client:
        return
    try:
        res = (
            supabase_client.table("files")
            .select("id,user_id,job_metadata,status,original_json_path,credits_used")
            .eq("status", "processing")
            .execute()
        )
    except Exception as e:
        print(f"[ocr-job] resume query failed: {e}", file=sys.stderr, flush=True)
        return
    rows = getattr(res, "data", None) or []
    resumed = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        job = _job_metadata_from_row(row)
        if not job or not (job.get("operation_name") or "").strip():
            continue
        job_status = (job.get("status") or "").strip().lower()
        if job_status == "failed":
            continue
        file_id = str(row.get("id") or "")
        user_id = str(row.get("user_id") or "")
        if not file_id or not user_id:
            continue
        if not (job.get("original_json_prefix") or "").strip():
            prefix = (row.get("original_json_path") or "").strip()
            if prefix:
                job["original_json_prefix"] = prefix
        if not _try_acquire_ocr_lease(file_id, job):
            continue
        if _spawn_background_ocr_finish(file_id, user_id, job):
            resumed += 1
            print(f"[ocr-job] resumed file_id={file_id}", file=sys.stderr, flush=True)
    if resumed:
        print(f"[ocr-job] resumed {resumed} orphaned OCR job(s)", file=sys.stderr, flush=True)


def _ocr_status_payload(row: dict) -> dict:
    """Build OCR status API payload entirely from files.job_metadata (+ row status)."""
    file_status = (row.get("status") or "").strip().lower() or "unknown"
    job = _job_metadata_from_row(row) or {}
    job_status = (job.get("status") or "").strip().lower()
    stage = (job.get("stage") or "").strip().lower()

    if file_status == "completed":
        status_out = "complete"
        stage = "complete"
    elif file_status == "failed" or job_status == "failed":
        status_out = "failed"
        stage = stage or "failed"
    else:
        status_out = job_status or file_status
        if status_out == "completed":
            status_out = "complete"
        if not stage:
            stage = "queued" if status_out in ("processing", "pending") else status_out

    pages_done = int(job.get("pages_done") or 0)
    total_pages = int(job.get("total_pages") or row.get("credits_used") or 0)
    label = _OCR_STAGE_LABELS.get(stage) or stage.replace("_", " ").title()
    percent = _ocr_progress_percent(job, status=status_out)
    payload = {
        "file_id": row.get("id"),
        "job_id": row.get("id"),
        "status": status_out,
        "stage": stage,
        "stage_label": label,
        "progress_percent": percent,
        "pages_done": pages_done,
        "total_pages": total_pages,
        "error": job.get("error"),
        "started_at": job.get("started_at"),
        "updated_at": job.get("updated_at"),
        "operation_name": job.get("operation_name"),
    }
    if status_out == "failed":
        owner_id = str(row.get("user_id") or "").strip()
        if owner_id:
            balance, _pr_err = get_credit_balance(owner_id)
            if balance is not None:
                payload.update(_credits_api_fields(balance))
    return payload


def run_ocr_on_file_bytes(content: bytes, filename: str, save_callback=None) -> dict:
    """PDF → multi-page OCR; images → single page_1 (same Vision pipeline as /pdf-json)."""
    name_l = (filename or "").lower()
    head = content[:5] if content else b""
    is_pdf = name_l.endswith(".pdf") or head.startswith(b"%PDF")
    if is_pdf:
        return extract_text_with_locations(content, progress_callback=None, save_callback=save_callback)
    try:
        im = Image.open(io.BytesIO(content))
        im.load()
    except (UnidentifiedImageError, OSError) as e:
        raise ValueError("File is not a PDF or a supported image (PNG, JPEG, WebP, GIF, etc.).") from e
    try:
        page_data = _vision_response_to_page_data(ocr_pil_with_client(get_vision_client(), im))
        if save_callback:
            save_callback(1, page_data)
        return {"page_1": page_data}
    finally:
        try:
            im.close()
        except Exception:
            pass


@app.before_request
def require_google_ocr_key():
    global _ocr_resume_started
    # Once per worker process: resume any OCR jobs left processing after a restart.
    if not _ocr_resume_started:
        with _ocr_resume_lock:
            if not _ocr_resume_started:
                _ocr_resume_started = True
                threading.Thread(
                    target=_resume_orphaned_ocr_jobs,
                    name="ocr-resume",
                    daemon=True,
                ).start()
    if request.endpoint is None:
        return None
    if request.endpoint == "static":
        return None
    # Super-admin read-only view-as: block all mutating requests that carry the header.
    view_as = (request.headers.get("X-Admin-View-As") or "").strip()
    if view_as and request.method not in ("GET", "HEAD", "OPTIONS"):
        token = _bearer_token_from_request()
        actor = _auth_user_from_access_token(token) if token else None
        if actor and _is_superadmin_email(actor.get("email")):
            return (
                jsonify(
                    {
                        "error": "Read-only admin view. Mutations are disabled.",
                        "error_type": "admin_readonly",
                    }
                ),
                403,
            )
        return jsonify({"error": "Forbidden.", "error_type": "not_superadmin"}), 403
    exempt = {
        "api_auth_status",
        "landing",
        "pricing_page",
        "privacy_page",
        "terms_page",
        "about_page",
        "faq_page",
        "contact_page",
        "refund_page",
        "robots_txt",
        "sitemap_xml",
        "login_page",
        "signup_page",
        "forgot_password_page",
        "reset_password_page",
        "dashboard2_page",
        "dashboard_legacy_redirect",
        "process_supabase_preflight",
        "process_supabase_preflight_upload",
        "api_ocr_status",
        "api_stripe_create_checkout_session",
        "api_stripe_webhook",
        "api_lemon_create_checkout",
        "api_lemon_webhook",
        "admin.admin_dashboard_page",
        "admin.admin_user_page",
        "admin.admin_finance_page",
        "admin.api_admin_me",
        "admin.api_admin_stats",
        "admin.api_admin_finance",
        "admin.api_admin_users",
        "admin.api_admin_user_detail",
        "admin.api_admin_user_files",
        "admin.api_admin_impersonate_start",
        "admin.api_admin_impersonate_exit",
    }
    if request.endpoint in exempt:
        return None
    if has_ocr_credentials():
        return None
    if (
        request.path.startswith("/api/")
        or request.path in ("/pdf", "/pdf-json", "/process")
        or request.is_json
    ):
        return jsonify(
            {
                "error": "Google OCR is not configured on the server.",
                "error_type": "cloud_configuration",
                "needs_credentials": False,
            }
        ), 503
    if request.method in ("GET", "HEAD"):
        return redirect(url_for("landing"))
    return jsonify(
        {
            "error": "Google OCR is not configured on the server.",
            "error_type": "cloud_configuration",
            "needs_credentials": False,
        }
    ), 503


@app.route("/api/auth/status", methods=["GET"])
def api_auth_status():
    return jsonify({"has_credentials": has_ocr_credentials(), "source": "environment"})


@app.route("/setup-credentials", methods=["GET"])
def setup_credentials_redirect():
    """Legacy route: per-session OCR keys are no longer supported."""
    return redirect(url_for("dashboard2_page"))


def _public_pricing_context() -> dict:
    return {
        "pricing_packages": [
            {
                "price_id": p["price_id"],
                "lemon_variant_id": str(p.get("lemon_variant_id") or ""),
                "credits": int(p["credits"]),
                "amount_usd": p["amount_usd"],
                "price_label": f"${p['amount_usd']}",
            }
            for p in PRICE_PACKAGES
        ],
        "monthly_free_credit_allowance": PROFILE_PAGES_MONTHLY_ALLOWANCE,
        "lemon_checkout_enabled": bool(
            LEMON_SQUEEZY_API_KEY and LEMON_SQUEEZY_STORE_ID
        ),
        "stripe_checkout_enabled": bool(STRIPE_SECRET_KEY),
    }


@app.route("/robots.txt", methods=["GET"])
def robots_txt():
    base = get_public_app_base_url()
    body = "\n".join(
        [
            "User-agent: *",
            "Allow: /",
            "Disallow: /api/",
            "Disallow: /dashboard",
            "Disallow: /dashboard2",
            "Disallow: /admin",
            "Disallow: /login",
            "Disallow: /signup",
            "Disallow: /forgot-password",
            "Disallow: /reset-password",
            "Disallow: /setup-credentials",
            "Disallow: /process",
            f"Sitemap: {base}/sitemap.xml",
            "",
        ]
    )
    return Response(body, mimetype="text/plain; charset=utf-8")


@app.route("/sitemap.xml", methods=["GET"])
def sitemap_xml():
    base = get_public_app_base_url()
    # Public, indexable pages only (no auth / dashboard / API).
    pages = [
        ("/", "1.0", "weekly"),
        ("/pricing", "0.8", "weekly"),
        ("/about", "0.7", "monthly"),
        ("/faq", "0.7", "monthly"),
        ("/contact", "0.6", "monthly"),
        ("/privacy", "0.4", "yearly"),
        ("/terms", "0.4", "yearly"),
        ("/refund", "0.4", "yearly"),
    ]
    urls = []
    for path, priority, changefreq in pages:
        loc = f"{base}{path}" if path != "/" else f"{base}/"
        urls.append(
            "  <url>\n"
            f"    <loc>{loc}</loc>\n"
            f"    <changefreq>{changefreq}</changefreq>\n"
            f"    <priority>{priority}</priority>\n"
            "  </url>"
        )
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "\n".join(urls)
        + "\n</urlset>\n"
    )
    return Response(xml, mimetype="application/xml; charset=utf-8")


@app.route("/", methods=["GET"])
def landing():
    base = get_public_app_base_url()
    seo = _seo_context(
        title=(
            "Punjabi OCR & Gurmukhi OCR — Convert Punjabi PDFs to Editable Text | GurmukhiOCR"
        ),
        description=(
            "Free online Punjabi OCR and Gurmukhi OCR. Convert scanned Punjabi PDFs and "
            "images into editable Unicode text. Review pages side by side and export .txt."
        ),
        path="/",
        json_ld=_homepage_json_ld(base),
    )
    return render_template(
        "landing.html",
        pricing_page=False,
        **_public_pricing_context(),
        **supabase_browser_config,
        **seo,
    )


@app.route("/pricing", methods=["GET"])
def pricing_page():
    seo = _seo_context(
        title="OCR Credits & Pricing — Punjabi / Gurmukhi OCR | GurmukhiOCR",
        description=(
            "GurmukhiOCR pricing: free monthly Punjabi OCR credits, plus optional one-time "
            "credit packs for Gurmukhi PDF and image OCR."
        ),
        path="/pricing",
        active_page="pricing",
    )
    return render_template(
        "landing.html",
        pricing_page=True,
        **_public_pricing_context(),
        **supabase_browser_config,
        **seo,
    )


@app.route("/privacy", methods=["GET"])
def privacy_page():
    seo = _seo_context(
        title="Privacy Policy — GurmukhiOCR",
        description=(
            "Privacy policy for GurmukhiOCR: how uploads are processed with Google Cloud Vision, "
            "how long files are kept, and how to request deletion."
        ),
        path="/privacy",
        page_heading="Privacy policy",
        page_lede="How GurmukhiOCR handles uploads, OCR processing, retention, and your account data.",
        active_page="privacy",
    )
    return render_template("privacy.html", **seo)


@app.route("/terms", methods=["GET"])
def terms_page():
    seo = _seo_context(
        title="Terms of Service — GurmukhiOCR",
        description=(
            "Terms of service for using GurmukhiOCR Punjabi OCR, credit purchases, and document conversion."
        ),
        path="/terms",
        page_heading="Terms of service",
        page_lede="Rules and expectations for using the GurmukhiOCR service.",
        active_page="terms",
    )
    return render_template("terms.html", **seo)


@app.route("/refund", methods=["GET"])
def refund_page():
    seo = _seo_context(
        title="Refund Policy — GurmukhiOCR",
        description=(
            "Refund policy for GurmukhiOCR OCR credit pack purchases, including the refund window "
            "and how to request a refund."
        ),
        path="/refund",
        page_heading="Refund policy",
        page_lede="How refunds work for paid GurmukhiOCR OCR credit packs.",
        active_page="refund",
    )
    return render_template("refund.html", **seo)


@app.route("/contact", methods=["GET"])
def contact_page():
    seo = _seo_context(
        title="Contact — GurmukhiOCR Support",
        description=(
            "Contact GurmukhiOCR support for billing, OCR credits, privacy requests, and product questions."
        ),
        path="/contact",
        page_heading="Contact",
        page_lede="Reach the GurmukhiOCR team for support, billing, and privacy requests.",
        active_page="contact",
    )
    return render_template("contact.html", **seo)


@app.route("/about", methods=["GET"])
def about_page():
    seo = _seo_context(
        title="About GurmukhiOCR — Online Punjabi & Gurmukhi OCR",
        description=(
            "About GurmukhiOCR and the business behind our online Punjabi OCR and Gurmukhi OCR tool."
        ),
        path="/about",
        page_heading="About GurmukhiOCR",
        page_lede="Online Punjabi OCR and Gurmukhi OCR for scanned documents—plus who operates the service.",
        active_page="about",
        json_ld=[
            {
                "@context": "https://schema.org",
                "@type": "AboutPage",
                "name": "About GurmukhiOCR",
                "url": f"{get_public_app_base_url()}/about",
                "description": (
                    "About the GurmukhiOCR Punjabi OCR and Gurmukhi OCR web application."
                ),
            }
        ],
    )
    return render_template("about.html", **seo)


@app.route("/faq", methods=["GET"])
def faq_page():
    base = get_public_app_base_url()
    faq_entities = [
        {
            "@type": "Question",
            "name": "What is GurmukhiOCR?",
            "acceptedAnswer": {
                "@type": "Answer",
                "text": (
                    "GurmukhiOCR is an online Punjabi OCR and Gurmukhi OCR service that converts "
                    "scanned Punjabi PDFs and images into editable Unicode text."
                ),
            },
        },
        {
            "@type": "Question",
            "name": "Can I convert a Punjabi PDF to editable text?",
            "acceptedAnswer": {
                "@type": "Answer",
                "text": (
                    "Yes. Upload a multi-page PDF for Punjabi PDF OCR, review results page by page, "
                    "then export the full text as a .txt file."
                ),
            },
        },
        {
            "@type": "Question",
            "name": "Does it support Punjabi image OCR?",
            "acceptedAnswer": {
                "@type": "Answer",
                "text": (
                    "Yes. You can run Punjabi image OCR on common image formats as well as PDF pages."
                ),
            },
        },
    ]
    seo = _seo_context(
        title="FAQ — Punjabi OCR & Gurmukhi OCR | GurmukhiOCR",
        description=(
            "Frequently asked questions about Punjabi OCR, Gurmukhi OCR, PDF conversion, "
            "free credits, and using GurmukhiOCR."
        ),
        path="/faq",
        page_heading="Frequently asked questions",
        page_lede="Quick answers about Punjabi OCR, Gurmukhi OCR, and using GurmukhiOCR.",
        active_page="faq",
        json_ld=[
            {
                "@context": "https://schema.org",
                "@type": "FAQPage",
                "mainEntity": faq_entities,
                "url": f"{base}/faq",
            }
        ],
    )
    return render_template("faq.html", **seo)


@app.route("/login", methods=["GET"])
def login_page():
    seo = _seo_context(
        title="Log in — GurmukhiOCR",
        description="Log in to GurmukhiOCR to run Punjabi OCR and manage your documents.",
        path="/login",
        robots="noindex, nofollow",
        active_page="login",
    )
    return render_template(
        "login.html",
        **supabase_browser_config,
        **seo,
    )


@app.route("/signup", methods=["GET"])
def signup_page():
    seo = _seo_context(
        title="Sign up — Free Punjabi OCR Credits | GurmukhiOCR",
        description=(
            "Create a free GurmukhiOCR account to convert Punjabi PDFs and images into editable text."
        ),
        path="/signup",
        robots="noindex, nofollow",
        active_page="signup",
    )
    return render_template(
        "signup.html",
        **supabase_browser_config,
        **seo,
    )


@app.route("/forgot-password", methods=["GET"])
def forgot_password_page():
    seo = _seo_context(
        title="Forgot password — GurmukhiOCR",
        description="Reset your GurmukhiOCR account password.",
        path="/forgot-password",
        robots="noindex, nofollow",
    )
    return render_template("forgot_password.html", **supabase_browser_config, **seo)


@app.route("/reset-password", methods=["GET"])
def reset_password_page():
    seo = _seo_context(
        title="Reset password — GurmukhiOCR",
        description="Choose a new password for your GurmukhiOCR account.",
        path="/reset-password",
        robots="noindex, nofollow",
    )
    return render_template(
        "reset_password.html",
        password_recovery_page=True,
        **supabase_browser_config,
        **seo,
    )


@app.route("/dashboard", methods=["GET"])
def dashboard_legacy_redirect():
    return redirect(url_for("dashboard2_page"))


@app.route("/dashboard2", methods=["GET"])
def dashboard2_page():
    seo = _seo_context(
        title="Dashboard — GurmukhiOCR",
        description="GurmukhiOCR workspace for Punjabi OCR uploads and editing.",
        path="/dashboard2",
        robots="noindex, nofollow",
    )
    return render_template("dashboard2.html", **supabase_browser_config, **seo)


@app.route("/api/me/pages", methods=["GET"])
def api_me_pages():
    """Return credit balances. pages_remaining is free+paid for frontend compatibility."""
    token = _bearer_token_from_request()
    if not token:
        return jsonify({"error": "Missing Authorization bearer token."}), 401
    user_id = supabase_access_token_to_user_id(token)
    if not user_id:
        return jsonify({"error": "Invalid or expired token."}), 401
    balance, err, anchor = _load_credit_balance(user_id)
    if balance is None or anchor is None:
        return jsonify({"error": err or "No profile.", "pages_remaining": None}), 404
    next_reset = anchor + timedelta(days=PROFILE_PAGES_RESET_INTERVAL_DAYS)
    next_iso = next_reset.isoformat().replace("+00:00", "Z")
    payload = _credits_api_fields(balance)
    payload["next_reset_at"] = next_iso
    payload["reset_interval_days"] = PROFILE_PAGES_RESET_INTERVAL_DAYS
    return jsonify(payload)


@app.route("/api/stripe/create-checkout-session", methods=["POST"])
def api_stripe_create_checkout_session():
    """Create a one-time Stripe Checkout Session for buying OCR credits."""
    token = _bearer_token_from_request()
    if not token:
        return jsonify({"error": "Missing Authorization bearer token."}), 401
    user_id = supabase_access_token_to_user_id(token)
    if not user_id:
        return jsonify({"error": "Invalid or expired token."}), 401
    if not request.is_json:
        return jsonify({"error": "Expected application/json."}), 400
    if not STRIPE_SECRET_KEY:
        return jsonify({"error": "Stripe is not configured on the server."}), 503

    body = request.get_json(silent=True) or {}
    price_id = str(body.get("price_id") or "").strip()
    if not price_id or price_id not in PRICE_ID_TO_CREDITS:
        return jsonify({"error": "Invalid price_id.", "error_type": "invalid_price"}), 400

    credits = int(PRICE_ID_TO_CREDITS[price_id])
    try:
        stripe = _stripe_client()
        success_url = url_for("dashboard2_page", _external=True) + "?checkout=success"
        cancel_url = url_for("pricing_page", _external=True) + "?checkout=cancelled"
        session = stripe.checkout.Session.create(
            mode="payment",
            line_items=[{"price": price_id, "quantity": 1}],
            success_url=success_url,
            cancel_url=cancel_url,
            client_reference_id=str(user_id),
            metadata={
                "user_id": str(user_id),
                "price_id": price_id,
                "credits_granted": str(credits),
            },
        )
        checkout_url = getattr(session, "url", None)
        if not checkout_url:
            return jsonify({"error": "Stripe did not return a checkout URL."}), 502
        print(
            f"[stripe] checkout created session_id={getattr(session, 'id', None)} "
            f"user_id={user_id} price_id={price_id} credits={credits}",
            file=sys.stderr,
            flush=True,
        )
        return jsonify({"checkout_url": checkout_url})
    except Exception as e:
        print(f"[stripe] create-checkout-session failed: {e}", file=sys.stderr, flush=True)
        return jsonify({"error": "Could not create Stripe Checkout session."}), 500


@app.route("/api/stripe/webhook", methods=["POST"])
def api_stripe_webhook():
    """Stripe webhook: grant paid credits on checkout.session.completed (idempotent)."""
    if not STRIPE_SECRET_KEY or not STRIPE_WEBHOOK_SECRET:
        return jsonify({"error": "Stripe webhook is not configured."}), 503

    payload = request.get_data(cache=False, as_text=False)
    sig_header = request.headers.get("Stripe-Signature") or ""
    try:
        stripe = _stripe_client()
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except ValueError as e:
        print(f"[stripe] webhook invalid payload: {e}", file=sys.stderr, flush=True)
        return jsonify({"error": "Invalid payload."}), 400
    except Exception as e:
        # Includes stripe.error.SignatureVerificationError
        print(f"[stripe] webhook signature verification failed: {e}", file=sys.stderr, flush=True)
        return jsonify({"error": "Invalid signature."}), 400

    event_type = event.get("type") if isinstance(event, dict) else getattr(event, "type", None)
    if event_type != "checkout.session.completed":
        return jsonify({"received": True, "ignored": True}), 200

    try:
        data_object = event["data"]["object"] if isinstance(event, dict) else event.data.object
        session_id = (
            data_object.get("id")
            if isinstance(data_object, dict)
            else getattr(data_object, "id", None)
        )
        if not session_id:
            return jsonify({"error": "Missing checkout session id."}), 400

        # Re-fetch with line_items so we can resolve price_id reliably.
        session_obj = stripe.checkout.Session.retrieve(
            session_id,
            expand=["line_items.data.price"],
        )
        ok, message, status = _fulfill_checkout_session(session_obj)
        if status == 200:
            return jsonify({"received": True, "status": message}), 200
        return jsonify({"error": message}), status
    except Exception as e:
        print(f"[stripe] webhook handler error: {e}", file=sys.stderr, flush=True)
        traceback.print_exc(file=sys.stderr)
        return jsonify({"error": "Webhook handler failed."}), 500


@app.route("/api/lemon/create-checkout", methods=["POST"])
def api_lemon_create_checkout():
    """Create a Lemon Squeezy Checkout URL for a configured variant (additive to Stripe)."""
    token = _bearer_token_from_request()
    if not token:
        return jsonify({"error": "Missing Authorization bearer token."}), 401
    user_id = supabase_access_token_to_user_id(token)
    if not user_id:
        return jsonify({"error": "Invalid or expired token."}), 401
    if not request.is_json:
        return jsonify({"error": "Expected application/json."}), 400
    if not LEMON_SQUEEZY_API_KEY or not LEMON_SQUEEZY_STORE_ID:
        return jsonify({"error": "Lemon Squeezy is not configured on the server."}), 503

    body = request.get_json(silent=True) or {}
    variant_id = str(body.get("variant_id") or body.get("lemon_variant_id") or "").strip()
    if not variant_id or variant_id not in LEMON_VARIANT_ID_TO_CREDITS:
        return jsonify({"error": "Invalid variant_id.", "error_type": "invalid_variant"}), 400

    credits = int(LEMON_VARIANT_ID_TO_CREDITS[variant_id])
    email = _auth_email_from_access_token(token)
    redirect_url = url_for("dashboard2_page", _external=True) + "?checkout=success"
    try:
        checkout_url = create_lemon_checkout_url(
            api_key=LEMON_SQUEEZY_API_KEY,
            store_id=LEMON_SQUEEZY_STORE_ID,
            variant_id=variant_id,
            user_id=str(user_id),
            redirect_url=redirect_url,
            email=email,
        )
        print(
            f"[lemon] checkout created user_id={user_id} "
            f"variant_id={variant_id} credits={credits}",
            file=sys.stderr,
            flush=True,
        )
        return jsonify({"checkout_url": checkout_url})
    except Exception as e:
        print(f"[lemon] create-checkout failed: {e}", file=sys.stderr, flush=True)
        return jsonify({"error": "Could not create Lemon Squeezy checkout."}), 500


@app.route("/api/lemon/webhook", methods=["POST"])
def api_lemon_webhook():
    """Lemon Squeezy webhook: grant paid credits on paid order events (idempotent)."""
    if not LEMON_SQUEEZY_WEBHOOK_SECRET:
        return jsonify({"error": "Lemon webhook is not configured."}), 503

    payload = request.get_data(cache=False, as_text=False)
    signature = request.headers.get("X-Signature") or ""
    if not verify_lemon_signature(payload, signature, LEMON_SQUEEZY_WEBHOOK_SECRET):
        print("[lemon] webhook signature verification failed", file=sys.stderr, flush=True)
        return jsonify({"error": "Invalid signature."}), 400

    try:
        event = json.loads(payload.decode("utf-8"))
    except Exception as e:
        print(f"[lemon] webhook invalid JSON: {e}", file=sys.stderr, flush=True)
        return jsonify({"error": "Invalid payload."}), 400

    order = extract_lemon_order_payload(event)
    if order is None:
        return jsonify({"received": True, "ignored": True}), 200

    try:
        ok, message, status = fulfill_lemon_order(
            supabase_client=supabase_client,
            order=order,
            variant_id_to_credits=LEMON_VARIANT_ID_TO_CREDITS,
            add_paid_credits=add_paid_credits,
            is_unique_violation=_is_unique_violation,
        )
        if status == 200:
            return jsonify({"received": True, "status": message}), 200
        return jsonify({"error": message}), status
    except Exception as e:
        print(f"[lemon] webhook handler error: {e}", file=sys.stderr, flush=True)
        traceback.print_exc(file=sys.stderr)
        return jsonify({"error": "Webhook handler failed."}), 500


@app.route("/api/profile", methods=["GET"])
def api_profile_get():
    token = _bearer_token_from_request()
    if not token:
        return jsonify({"error": "Missing Authorization bearer token."}), 401
    user_id = supabase_access_token_to_user_id(token)
    if not user_id:
        return jsonify({"error": "Invalid or expired token."}), 401
    if not supabase_client:
        return jsonify({"error": "Supabase is not configured."}), 503
    email = _auth_email_from_access_token(token)
    username = ensure_profile_username(supabase_client, str(user_id), email)
    if not username:
        return jsonify({"error": "Could not load or create your username."}), 404
    return jsonify({"username": username})


@app.route("/api/profile", methods=["PATCH"])
def api_profile_patch():
    token = _bearer_token_from_request()
    if not token:
        return jsonify({"error": "Missing Authorization bearer token."}), 401
    user_id = supabase_access_token_to_user_id(token)
    if not user_id:
        return jsonify({"error": "Invalid or expired token."}), 401
    if not request.is_json:
        return jsonify({"error": "Expected application/json."}), 400
    if not supabase_client:
        return jsonify({"error": "Supabase is not configured."}), 503
    body = request.get_json(silent=True) or {}
    raw_username = body.get("username")
    if raw_username is None:
        return jsonify({"error": "Username is required."}), 400
    username, err = update_profile_username(supabase_client, str(user_id), str(raw_username))
    if err:
        status = 409 if "taken" in err.lower() else 400
        return jsonify({"error": err}), status
    return jsonify({"success": True, "username": username})


@app.route("/process", methods=["POST", "OPTIONS"])
def process_supabase_ocr():
    """
    Download a file from Supabase Storage (gbucket), run the same OCR pipeline as /pdf-json,
    and insert a row into the public.files table.
    Expects Authorization: Bearer <supabase access token> and JSON { file_path, file_name }.
    """
    if request.method == "OPTIONS":
        return Response("", status=204)
    if not supabase_client:
        return jsonify({"error": "Supabase is not configured on the server."}), 503
    if not request.is_json:
        return jsonify({"error": "Content-Type must be application/json."}), 400

    auth_header = request.headers.get("Authorization") or ""
    token = None
    if auth_header.lower().startswith("bearer "):
        token = auth_header[7:].strip()
    if not token:
        return jsonify({"error": "Missing or invalid Authorization bearer token."}), 401

    user_id = supabase_access_token_to_user_id(token)
    if not user_id:
        return jsonify({"error": "Invalid or expired session token."}), 401

    body = request.get_json(silent=True) or {}
    file_path = (body.get("file_path") or "").strip()
    file_name = (body.get("file_name") or "").strip() or None
    if not file_path:
        return jsonify({"error": "file_path is required."}), 400
    if ".." in file_path or file_path.startswith("/"):
        return jsonify({"error": "Invalid file_path."}), 400
    if not file_path.startswith(f"{user_id}/"):
        return jsonify({"error": "file_path must be under your user folder (user_id/…)."}), 403

    profile_job_id = str(uuid.uuid4())[:8]
    profile = _UploadProfile(profile_job_id)
    _set_active_upload_profile(profile)
    profile_finished = False
    temp_paths: list[str] = []
    pdf_for_gcs: str | None = None
    try:
        fd, temp_path = tempfile.mkstemp(suffix=".upload")
        os.close(fd)
        temp_paths.append(temp_path)

        with profile.time_step("storage_download", file_path=file_path):
            try:
                nbytes = _storage_download_to_file(SUPABASE_STORAGE_BUCKET, file_path, temp_path)
            except Exception as e:
                print(f"[process] Storage download failed: {e}", file=sys.stderr, flush=True)
                return jsonify({"error": f"Could not download file from storage: {e!s}"}), 502

        if not nbytes:
            return jsonify({"error": "Downloaded file was empty."}), 400

        _log_mem("after_storage_download_to_file")

        with profile.time_step("pdf_parse_page_count", bytes=nbytes):
            try:
                doc_kind, doc_pages = _document_kind_and_page_count_from_path(temp_path)
            except ValueError as e:
                return jsonify({"error": str(e)}), 400

        # Linearize PDFs ("fast web view") so the viewer can render page 1 from the first
        # ~few hundred KB instead of downloading the whole file. Client-uploaded originals
        # aren't linearized; re-upload the optimized copy in place. Best-effort.
        pdf_for_gcs = temp_path
        if doc_kind == "pdf":
            lin_path = _linearize_pdf_file(temp_path)
            if lin_path != temp_path:
                if lin_path not in temp_paths:
                    temp_paths.append(lin_path)
                pdf_for_gcs = lin_path
                try:
                    with profile.time_step(
                        "pdf_linearize_reupload",
                        bytes=os.path.getsize(lin_path),
                    ):
                        _storage_upload_file(
                            SUPABASE_STORAGE_BUCKET, file_path, lin_path, "application/pdf"
                        )
                except Exception as e:
                    print(
                        f"[process] linearized re-upload failed (using original): {e}",
                        file=sys.stderr,
                        flush=True,
                    )

        page_charge = doc_pages if doc_kind == "pdf" else 1

        with profile.time_step("db_read_profile_quota"):
            balance, pr_err = get_credit_balance(user_id)
        if balance is None:
            return jsonify(
                {
                    "error": pr_err or "Could not load your page quota from profiles.",
                    "error_type": "profile",
                    **_credits_api_fields(None),
                }
            ), 400
        cur_pages = int(balance["pages_remaining"])
        if cur_pages < page_charge:
            return jsonify(
                {
                    "error": f"Not enough credits remaining ({cur_pages} left; this file needs {page_charge}).",
                    "error_type": "insufficient_pages",
                    "pages_required": page_charge,
                    **_credits_api_fields(balance),
                }
            ), 402

        file_row_id = str(uuid.uuid4())
        original_json_prefix = _json_kind_prefix(user_id, file_row_id, "original")
        use_inline_json = doc_pages == 1
        insert_row = {
            "id": file_row_id,
            "user_id": user_id,
            "original_file_path": file_path,
            "file_name": file_name or os.path.basename(file_path),
            "original_json": None,
            "edited_json": None,
            "original_json_path": None if use_inline_json else original_json_prefix,
            # Copy-on-write: editable starts pointing at the original pages; a distinct
            # editable prefix is created lazily on the first edit.
            "editable_json_path": None if use_inline_json else original_json_prefix,
            "credits_used": page_charge,
            "status": "processing",
        }

        with profile.time_step("db_insert", file_id=file_row_id):
            try:
                ins = supabase_client.table("files").insert(insert_row).execute()
            except Exception as e:
                print(f"[process] DB insert failed: {e}", file=sys.stderr, flush=True)
                return jsonify({"error": f"Database insert failed: {e!s}", "error_type": "db_error"}), 500

        row = None
        if getattr(ins, "data", None) is not None:
            row = ins.data[0] if isinstance(ins.data, list) and len(ins.data) else ins.data
        if row is None:
            return jsonify(
                {
                    "error": "Insert returned no row. Confirm the files table columns and that the service role can insert.",
                    "error_type": "db_error",
                }
            ), 500

        inserted_id = row.get("id") if isinstance(row, dict) else None
        if not inserted_id:
            return jsonify({"error": "Inserted row has no id.", "error_type": "db_error"}), 500

        credit_free_used = None
        credit_paid_used = None

        def mark_file_failed(message: str, *, refund_pages: int | None = None) -> int | None:
            meta = get_job_metadata(str(inserted_id)) or {}
            if credit_free_used is not None:
                meta["credits_free_used"] = credit_free_used
            if credit_paid_used is not None:
                meta["credits_paid_used"] = credit_paid_used
            return _mark_ocr_job_failed(
                str(inserted_id),
                user_id,
                meta,
                message,
                refund_pages=refund_pages,
            )

        with profile.time_step("db_consume_credits", pages=page_charge):
            ok_q, err_q, deduct_balance = deduct_ocr_credits(user_id, page_charge)
        if not ok_q:
            mark_file_failed(err_q or "Could not update credit balance.", refund_pages=0)
            return jsonify(
                {
                    "error": err_q or "Could not update credit balance.",
                    "error_type": "quota_update_failed",
                    "file_id": str(inserted_id),
                    **_credits_api_fields(deduct_balance or balance),
                }
            ), 409
        credit_free_used = int(deduct_balance.get("free_used") or 0)
        credit_paid_used = int(deduct_balance.get("paid_used") or 0)

        inserted_id_str = str(inserted_id)
        ocr_result = None
        try:
            if use_inline_json:
                # Single page: keep the page data in memory so it can be stored inline on
                # the row (1 page is tiny). No streaming needed.
                with open(pdf_for_gcs or temp_path, "rb") as inline_src:
                    inline_bytes = inline_src.read()
                with profile.time_step("ocr_pipeline", pages=doc_pages, inline_json=True):
                    ocr_result = run_ocr_on_file_bytes(
                        inline_bytes,
                        file_name or os.path.basename(file_path),
                        save_callback=None,
                    )
            else:
                # Multi-page PDFs: start Vision async in-request, then finish in a background
                # thread so gunicorn can return before long OCR / page streaming completes.
                started_at = _utc_now_iso()
                update_job_metadata(
                    inserted_id_str,
                    {
                        "status": "processing",
                        "stage": "uploading",
                        "pages_done": 0,
                        "total_pages": int(doc_pages),
                        "started_at": started_at,
                        "updated_at": started_at,
                        "error": None,
                        "operation_name": None,
                        "gcs_input_object": None,
                        "gcs_output_prefix": None,
                        "lease_owner": None,
                        "lease_expires": None,
                        "retry_count": 0,
                        "original_json_prefix": original_json_prefix,
                        "credits_free_used": credit_free_used,
                        "credits_paid_used": credit_paid_used,
                    },
                )

                with profile.time_step("ocr_vision_start", pages=doc_pages):
                    job = _start_async_pdf_ocr_job(
                        doc_pages,
                        progress_callback=None,
                        pdf_path=pdf_for_gcs or temp_path,
                    )

                gc.collect()

                job["original_json_prefix"] = original_json_prefix
                job["status"] = "processing"
                job["stage"] = "waiting_vision"
                if not job.get("started_at"):
                    job["started_at"] = started_at
                merge_job_metadata(
                    inserted_id_str,
                    {
                        "status": "processing",
                        "stage": "waiting_vision",
                        "operation_name": job.get("operation_name"),
                        "gcs_bucket": job.get("gcs_bucket"),
                        "gcs_input_object": job.get("gcs_input_object"),
                        "gcs_output_prefix": job.get("gcs_output_prefix"),
                        "run_prefix": job.get("run_prefix"),
                        "source_uri": job.get("source_uri"),
                        "output_uri": job.get("output_uri"),
                        "pages_done": 0,
                        "total_pages": int(doc_pages),
                        "started_at": job.get("started_at") or started_at,
                        "error": None,
                        "original_json_prefix": original_json_prefix,
                        "retry_count": int(job.get("retry_count") or 0),
                        "credits_free_used": credit_free_used,
                        "credits_paid_used": credit_paid_used,
                    },
                )
                _spawn_background_ocr_finish(inserted_id_str, user_id, job)

                profile.finish(
                    file_path=file_path,
                    page_count=doc_pages,
                    json_storage="per-page",
                    doc_kind=doc_kind,
                    async_ocr=True,
                )
                profile_finished = True
                return jsonify(
                    {
                        "success": True,
                        "async": True,
                        "file_id": inserted_id_str,
                        "job_id": inserted_id_str,
                        "file_path": file_path,
                        "page_count": doc_pages,
                        "status": "processing",
                        "stage": "waiting_vision",
                        "stage_label": _OCR_STAGE_LABELS["waiting_vision"],
                        "pages_charged": page_charge,
                        "credits_used": page_charge,
                        "json_storage": "per-page",
                        "profile_job_id": profile_job_id,
                        "row": {
                            "id": inserted_id_str,
                            "status": "processing",
                            "file_name": file_name or os.path.basename(file_path),
                            "credits_used": page_charge,
                        },
                        **_credits_api_fields(deduct_balance),
                    }
                )
        except VisionConfigurationError as e:
            refunded = mark_file_failed(e.user_message)
            resp, code = jsonify_vision_configuration_error(e)
            if refunded is not None:
                data = resp.get_json()
                bal, _ = get_credit_balance(user_id)
                data.update(_credits_api_fields(bal) if bal else {"pages_remaining": refunded})
                return jsonify(data), code
            return resp, code
        except EmptyPagesError as e:
            refunded = mark_file_failed(str(e))
            # Streaming uploads may have already written some per-page JSONs before the
            # consecutive-empty check tripped. Remove the orphans (both prefixes, no-op if
            # absent) without ever masking the original EmptyPagesError.
            try:
                _delete_page_json_prefix(_json_kind_prefix(user_id, inserted_id_str, "original"))
                _delete_page_json_prefix(_json_kind_prefix(user_id, inserted_id_str, "editable"))
            except Exception as cleanup_e:
                print(
                    f"[process] orphan JSON cleanup after empty-pages failure errored: {cleanup_e}",
                    file=sys.stderr,
                    flush=True,
                )
            err_body = {
                "error": str(e),
                "error_type": "empty_pages",
                "start_page": e.start_page,
                "end_page": e.end_page,
                "file_id": str(inserted_id),
            }
            if refunded is not None:
                bal, _ = get_credit_balance(user_id)
                err_body.update(_credits_api_fields(bal) if bal else {"pages_remaining": refunded})
            return jsonify(err_body), 500
        except Exception as e:
            print(f"[process] OCR failed: {e}", file=sys.stderr, flush=True)
            refunded = mark_file_failed(str(e))
            err_body = {"error": str(e), "error_type": "ocr_error", "file_id": str(inserted_id)}
            if refunded is not None:
                bal, _ = get_credit_balance(user_id)
                err_body.update(_credits_api_fields(bal) if bal else {"pages_remaining": refunded})
            return jsonify(err_body), 500

        # Temp files are removed in finally; OCR no longer needs the on-disk PDF here.

        processing_duration_seconds = max(0, round(time.perf_counter() - profile.t0))
        if use_inline_json:
            page_keys = [k for k in ocr_result if isinstance(k, str) and re.match(r"^page_\d+$", k, re.I)]
            page_count = len(page_keys)
            page_key = page_keys[0]
            page_data = ocr_result[page_key]
            t_ser = time.perf_counter()
            update_row = {
                "status": "completed",
                "credits_used": page_charge,
                "processing_duration_seconds": processing_duration_seconds,
                **_json_row_fields_for_page_count(user_id, inserted_id_str, 1, page_data, page_data),
            }
            profile.add("json_serialize", time.perf_counter() - t_ser, "json_serialize_ops")
            json_storage = "inline"
        else:
            page_count = doc_pages
            update_row = {
                "status": "completed",
                "credits_used": page_charge,
                "processing_duration_seconds": processing_duration_seconds,
                "original_json": None,
                "edited_json": None,
                "original_json_path": original_json_prefix,
                # Copy-on-write: editable mirrors original until the first per-page edit.
                "editable_json_path": original_json_prefix,
            }
            json_storage = "per-page"

        with profile.time_step("db_final_update", file_id=str(inserted_id), json_storage=json_storage):
            try:
                upd = supabase_client.table("files").update(update_row).eq("id", str(inserted_id)).eq("user_id", user_id).execute()
                upd_rows = getattr(upd, "data", None) or []
                if isinstance(upd_rows, list) and upd_rows:
                    row = upd_rows[0]
                elif isinstance(upd_rows, dict):
                    row = upd_rows
            except Exception as e:
                print(f"[process] final files row update failed: {e}", file=sys.stderr, flush=True)
                # OCR succeeded; pages may already be in storage — do not refund credits.
                mark_file_failed(str(e), refund_pages=0)
                return jsonify({"error": f"Final database update failed: {e!s}", "error_type": "db_error", "file_id": str(inserted_id)}), 500

        profile.finish(
            file_path=file_path,
            page_count=page_count,
            json_storage=json_storage,
            doc_kind=doc_kind,
        )
        profile_finished = True
        return jsonify(
            {
                "success": True,
                "file_path": file_path,
                "page_count": page_count,
                "row": row,
                "file_id": str(inserted_id),
                "status": "completed",
                "pages_charged": page_charge,
                "credits_used": page_charge,
                "json_storage": json_storage,
                "profile_job_id": profile_job_id,
                **_credits_api_fields(deduct_balance),
            }
        )
    finally:
        _unlink_temp_files(temp_paths)
        if not profile_finished:
            profile.finish(status="incomplete")
        _set_active_upload_profile(None)


@app.route("/api/ocr-status/<uuid:file_id>", methods=["GET"])
def api_ocr_status(file_id):
    """Poll background OCR progress for a file the caller can access."""
    token = _bearer_token_from_request()
    if not token:
        return jsonify({"error": "Missing Authorization bearer token."}), 401
    user_id = supabase_access_token_to_user_id(token)
    if not user_id:
        return jsonify({"error": "Invalid or expired session token."}), 401
    if not supabase_client:
        return jsonify({"error": "Supabase is not configured on the server."}), 503

    access = _file_access_for_user(str(file_id), user_id)
    if not access or not access.row:
        return jsonify({"error": "File not found."}), 404
    return jsonify(_ocr_status_payload(access.row))


@app.route("/api/process/preflight", methods=["POST"])
def process_supabase_preflight():
    """Check file type/page count and available credits before running OCR."""
    if not supabase_client:
        return jsonify({"error": "Supabase is not configured on the server."}), 503
    if not request.is_json:
        return jsonify({"error": "Content-Type must be application/json."}), 400

    token = _bearer_token_from_request()
    if not token:
        return jsonify({"error": "Missing or invalid Authorization bearer token."}), 401

    user_id = supabase_access_token_to_user_id(token)
    if not user_id:
        return jsonify({"error": "Invalid or expired session token."}), 401

    body = request.get_json(silent=True) or {}
    file_path = (body.get("file_path") or "").strip()
    file_name = (body.get("file_name") or "").strip() or None
    if not file_path:
        return jsonify({"error": "file_path is required."}), 400
    if ".." in file_path or file_path.startswith("/"):
        return jsonify({"error": "Invalid file_path."}), 400
    if not file_path.startswith(f"{user_id}/"):
        return jsonify({"error": "file_path must be under your user folder (user_id/…)."}), 403

    try:
        raw = download_from_gbucket(file_path)
    except Exception as e:
        print(f"[preflight] Storage download failed: {e}", file=sys.stderr, flush=True)
        return jsonify({"error": f"Could not download file from storage: {e!s}"}), 502
    if not raw:
        return jsonify({"error": "Downloaded file was empty."}), 400

    try:
        doc_kind, doc_pages = _document_kind_and_page_count(raw)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    credits_required = doc_pages if doc_kind == "pdf" else 1
    balance, pr_err = get_credit_balance(user_id)
    if balance is None:
        return jsonify(
            {
                "error": pr_err or "Could not load your page quota from profiles.",
                "error_type": "profile",
                **_credits_api_fields(None),
            }
        ), 400

    cur_pages = int(balance["pages_remaining"])
    can_process = cur_pages >= credits_required
    return jsonify(
        {
            "can_process": can_process,
            "file_path": file_path,
            "file_name": file_name or os.path.basename(file_path),
            "kind": doc_kind,
            "page_count": doc_pages,
            "credits_required": credits_required,
            "pages_required": credits_required,
            "error_type": None if can_process else "insufficient_pages",
            "error": None
            if can_process
            else f"Not enough credits remaining ({cur_pages} left; this file needs {credits_required}).",
            **_credits_api_fields(balance),
        }
    )


@app.route("/api/process/preflight-upload", methods=["POST"])
def process_supabase_preflight_upload():
    """Check selected file bytes before any Supabase Storage upload or OCR processing."""
    token = _bearer_token_from_request()
    if not token:
        return jsonify({"error": "Missing or invalid Authorization bearer token."}), 401

    user_id = supabase_access_token_to_user_id(token)
    if not user_id:
        return jsonify({"error": "Invalid or expired session token."}), 401

    if "file" not in request.files:
        return jsonify({"error": "No file provided."}), 400
    upload = request.files["file"]
    file_name = upload.filename or "upload"
    profile_job_id = f"preflight-{str(uuid.uuid4())[:8]}"
    profile = _UploadProfile(profile_job_id)
    with profile.time_step("file_upload_received", file_name=file_name):
        raw = upload.read()
    if not raw:
        return jsonify({"error": "Uploaded file was empty."}), 400

    with profile.time_step("pdf_parse_page_count", bytes=len(raw)):
        try:
            doc_kind, doc_pages = _document_kind_and_page_count(raw)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

    credits_required = doc_pages if doc_kind == "pdf" else 1
    balance, pr_err = get_credit_balance(user_id)
    if balance is None:
        return jsonify(
            {
                "error": pr_err or "Could not load your page quota from profiles.",
                "error_type": "profile",
                **_credits_api_fields(None),
            }
        ), 400

    cur_pages = int(balance["pages_remaining"])
    can_process = cur_pages >= credits_required
    profile.finish(kind=doc_kind, page_count=doc_pages, can_process=can_process)
    return jsonify(
        {
            "can_process": can_process,
            "file_name": file_name,
            "kind": doc_kind,
            "page_count": doc_pages,
            "credits_required": credits_required,
            "pages_required": credits_required,
            "error_type": None if can_process else "insufficient_pages",
            "error": None
            if can_process
            else f"Not enough credits remaining ({cur_pages} left; this file needs {credits_required}).",
            **_credits_api_fields(balance),
        }
    )


def _bearer_token_from_request():
    auth_header = request.headers.get("Authorization") or ""
    if auth_header.lower().startswith("bearer "):
        return auth_header[7:].strip()
    return None


def _files_row_for_user(file_id: str, user_id: str) -> dict | None:
    access = _file_access_for_user(file_id, user_id)
    return access.row if access else None


def _file_access_for_user(file_id: str, user_id: str) -> FileAccess | None:
    return resolve_file_access(supabase_client, file_id, user_id)


def _json_field_to_python(val):
    if val is None:
        return None
    if isinstance(val, (dict, list)):
        return val
    if isinstance(val, str):
        try:
            return json.loads(val)
        except json.JSONDecodeError:
            return None
    return val


def _safe_ocr_float(val):
    """Vision/confidence values must be JSON-safe (no NaN/inf)."""
    if val is None:
        return None
    try:
        f = float(val)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def _json_http_response(data, status=200):
    """Build a UTF-8 JSON Response; allow_nan=False so clients can parse with fetch().json()."""
    try:
        body = json.dumps(
            data,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            default=str,
        )
    except (TypeError, ValueError) as e:
        print(f"[json-response] Serialization failed: {e}", file=sys.stderr, flush=True)
        traceback.print_exc(file=sys.stderr)
        return (
            jsonify(
                {
                    "error": "Could not serialize document JSON (invalid floats or structure).",
                    "error_type": "serialization",
                }
            ),
            500,
        )
    return Response(body, mimetype="application/json; charset=utf-8", status=status)


def _file_row_json_status(row: dict) -> tuple[str | None, tuple]:
    status = (row.get("status") or "").strip().lower()
    if status in ("processing", "pending"):
        return status, (jsonify({"error": "This file is still processing.", "status": status}), 409)
    if status == "failed":
        return status, (jsonify({"error": "This file failed to process.", "status": status}), 409)
    return status, None


def _load_editable_page_data(row: dict, page_num: int) -> dict:
    file_id = row.get("id")
    if not file_id:
        raise ValueError("Invalid file row.")
    if _row_uses_inline_json(row):
        if page_num != 1:
            raise ValueError("Page number out of range.")
        return _inline_page_data(row, prefer_edited=True)
    original_prefix = (row.get("original_json_path") or "").strip()
    editable_prefix = (row.get("editable_json_path") or "").strip()
    # Copy-on-write: when editable mirrors original (no edits yet) or is unset, read the
    # original page directly — one request, no failed lookup. Only a distinct editable
    # prefix (created on first edit) warrants trying editable then falling back to original
    # for pages that haven't been edited.
    if editable_prefix and editable_prefix != original_prefix:
        try:
            return _download_page_json_at_prefix(editable_prefix, page_num)
        except Exception:
            if original_prefix:
                return _download_page_json_at_prefix(original_prefix, page_num)
            raise
    if original_prefix:
        return _download_page_json_at_prefix(original_prefix, page_num)
    user_id = row.get("user_id")
    if not user_id:
        raise ValueError("Invalid file row.")
    try:
        return _download_page_json(str(user_id), str(file_id), "editable", page_num)
    except Exception:
        return _download_page_json(str(user_id), str(file_id), "original", page_num)


def _load_original_page_data(row: dict, page_num: int) -> dict:
    file_id = row.get("id")
    if not file_id:
        raise ValueError("Invalid file row.")
    if _row_uses_inline_json(row):
        if page_num != 1:
            raise ValueError("Page number out of range.")
        return _inline_page_data(row, prefer_edited=False)
    original_prefix = (row.get("original_json_path") or "").strip()
    if original_prefix:
        return _download_page_json_at_prefix(original_prefix, page_num)
    user_id = row.get("user_id")
    if not user_id:
        raise ValueError("Invalid file row.")
    return _download_page_json(str(user_id), str(file_id), "original", page_num)


def _save_editable_page_data(row: dict, page_num: int, page_data: dict) -> None:
    owner_id = str(row.get("user_id") or "")
    file_id = str(row.get("id") or "")
    if not owner_id or not file_id:
        raise ValueError("Invalid file row.")
    if _row_uses_inline_json(row):
        if page_num != 1:
            raise ValueError("Page number out of range.")
        supabase_client.table("files").update({"edited_json": page_data}).eq("id", file_id).eq(
            "user_id", owner_id
        ).execute()
        return
    original_prefix = (row.get("original_json_path") or "").strip()
    editable_prefix = (row.get("editable_json_path") or "").strip()
    # Copy-on-write: the first edit splits editable off from the shared original prefix so
    # writing an edited page never clobbers the pristine original. The editable prefix stays
    # sparse — only pages that were actually edited live there.
    if not editable_prefix or editable_prefix == original_prefix:
        editable_prefix = _json_kind_prefix(owner_id, file_id, "editable")
        try:
            supabase_client.table("files").update({"editable_json_path": editable_prefix}).eq(
                "id", file_id
            ).eq("user_id", owner_id).execute()
        except Exception as e:
            raise ValueError(f"Could not initialize editable storage: {e}") from e
        row["editable_json_path"] = editable_prefix
    _upload_page_json_at_prefix(editable_prefix, page_num, page_data)


def _user_owned_storage_path(user_id: str, storage_path: str | None) -> bool:
    path = (storage_path or "").strip()
    if not path or ".." in path or path.startswith("/"):
        return False
    return path.startswith(f"{user_id}/")


def _sanitize_storage_filename(name: str | None) -> str:
    base = (name or "upload.bin").strip()
    base = re.sub(r"[/\\]", "_", base)
    if len(base) > 220:
        base = base[:220]
    return base or "upload.bin"


def _ocr_json_page_keys(data: dict) -> list[str]:
    return [k for k in data if isinstance(k, str) and re.match(r"^page_\d+$", k, re.I)]


def _parse_uploaded_json_file(upload) -> tuple[dict, bytes]:
    if upload is None or not getattr(upload, "filename", None):
        raise ValueError("JSON file is required.")
    raw = upload.read()
    if not raw:
        raise ValueError("JSON file was empty.")
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ValueError("JSON file is not valid UTF-8 JSON.") from e
    if not isinstance(parsed, dict):
        raise ValueError("JSON root must be an object.")
    return parsed, raw


def _document_kind_and_page_count(file_bytes: bytes) -> tuple[str, int]:
    if len(file_bytes) >= 4 and file_bytes[:4] == b"%PDF":
        doc = fitz.open(stream=file_bytes, filetype="pdf")
        try:
            return "pdf", len(doc)
        finally:
            doc.close()
    try:
        Image.open(io.BytesIO(file_bytes)).load()
        return "image", 1
    except Exception as e:
        raise ValueError("File is not a PDF or a supported raster image.") from e


def _document_kind_and_page_count_from_path(file_path: str) -> tuple[str, int]:
    with open(file_path, "rb") as f:
        head = f.read(4)
    if head == b"%PDF":
        doc = fitz.open(file_path)
        try:
            return "pdf", len(doc)
        finally:
            doc.close()
    try:
        with open(file_path, "rb") as f:
            Image.open(f).load()
        return "image", 1
    except Exception as e:
        raise ValueError("File is not a PDF or a supported raster image.") from e


def _parse_profile_timestamp(val) -> datetime | None:
    """Parse profiles.last_reset (or similar) to timezone-aware UTC datetime."""
    if val is None:
        return None
    if isinstance(val, datetime):
        dt = val
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    if isinstance(val, str):
        s = val.strip()
        if not s:
            return None
        try:
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            return datetime.fromisoformat(s).astimezone(timezone.utc)
        except ValueError:
            return None
    return None


def _credit_int(val, default: int = 0) -> int:
    try:
        if val is None:
            return default
        return max(0, int(val))
    except (TypeError, ValueError):
        return default


def _credit_balance_dict(
    *,
    free: int,
    paid: int,
    allowance: int,
    free_used: int = 0,
    paid_used: int = 0,
) -> dict:
    free = max(0, int(free))
    paid = max(0, int(paid))
    allowance = max(0, int(allowance))
    return {
        "free_pages_remaining": free,
        "paid_pages_remaining": paid,
        "pages_remaining": free + paid,
        "monthly_free_credit_allowance": allowance,
        "free_used": max(0, int(free_used)),
        "paid_used": max(0, int(paid_used)),
    }


def _credits_api_fields(balance: dict | None) -> dict:
    """API-compatible credit fields. pages_remaining is free+paid for frontend compat."""
    if not isinstance(balance, dict):
        return {
            "pages_remaining": None,
            "free_pages_remaining": None,
            "paid_pages_remaining": None,
            "monthly_free_credit_allowance": None,
        }
    return {
        "pages_remaining": int(balance.get("pages_remaining") or 0),
        "free_pages_remaining": int(balance.get("free_pages_remaining") or 0),
        "paid_pages_remaining": int(balance.get("paid_pages_remaining") or 0),
        "monthly_free_credit_allowance": int(
            balance.get("monthly_free_credit_allowance") or PROFILE_PAGES_MONTHLY_ALLOWANCE
        ),
    }


def _fetch_profile_credit_row(user_id: str) -> tuple[dict | None, str | None]:
    if not supabase_client:
        return None, "Supabase not configured"
    try:
        res = (
            supabase_client.table("profiles")
            .select(_CREDIT_BALANCE_SELECT)
            .eq("id", user_id)
            .limit(1)
            .execute()
        )
        rows = getattr(res, "data", None) or []
        if not rows:
            return (
                None,
                "No profile row found for this user (create a profiles row with the same id as auth.users).",
            )
        return rows[0], None
    except Exception as e:
        print(f"[profiles] read credit balance failed: {e}", file=sys.stderr, flush=True)
        return None, str(e)


def reset_monthly_free_credits(user_id: str) -> tuple[bool, str | None, dict | None]:
    """Set free_pages_remaining = monthly_free_credit_allowance. Does not modify paid credits.

    TODO: Call this helper on a monthly schedule (cron / background job). Lazy reset on
    credit reads also invokes this when last_reset is older than PROFILE_PAGES_RESET_INTERVAL_DAYS.
    """
    row, err = _fetch_profile_credit_row(user_id)
    if row is None:
        return False, err or "No profile.", None
    allowance = _credit_int(row.get("monthly_free_credit_allowance"), PROFILE_PAGES_MONTHLY_ALLOWANCE)
    if allowance <= 0:
        allowance = PROFILE_PAGES_MONTHLY_ALLOWANCE
    paid = _credit_int(row.get("paid_pages_remaining"), 0)
    now = datetime.now(timezone.utc)
    try:
        res = (
            supabase_client.table("profiles")
            .update(
                {
                    "free_pages_remaining": allowance,
                    "last_reset": now.isoformat(),
                }
            )
            .eq("id", user_id)
            .execute()
        )
        rows = getattr(res, "data", None) or []
        if not rows:
            return False, "Monthly free-credit reset did not update any profile row.", None
    except Exception as e:
        print(f"[profiles] reset_monthly_free_credits failed: {e}", file=sys.stderr, flush=True)
        return False, str(e), None
    return True, None, _credit_balance_dict(free=allowance, paid=paid, allowance=allowance)


def _load_credit_balance(
    user_id: str, *, apply_monthly_reset: bool = True
) -> tuple[dict | None, str | None, datetime | None]:
    """Load free/paid balances. Optionally lazy-reset free credits when the monthly window elapsed."""
    row, err = _fetch_profile_credit_row(user_id)
    if row is None:
        return None, err, None

    allowance = _credit_int(row.get("monthly_free_credit_allowance"), PROFILE_PAGES_MONTHLY_ALLOWANCE)
    if allowance <= 0:
        allowance = PROFILE_PAGES_MONTHLY_ALLOWANCE
    free = _credit_int(row.get("free_pages_remaining"), 0)
    paid = _credit_int(row.get("paid_pages_remaining"), 0)
    last_reset = _parse_profile_timestamp(row.get("last_reset"))
    now = datetime.now(timezone.utc)

    if last_reset is None:
        try:
            supabase_client.table("profiles").update({"last_reset": now.isoformat()}).eq(
                "id", user_id
            ).execute()
        except Exception as e:
            print(f"[profiles] set last_reset failed: {e}", file=sys.stderr, flush=True)
            return None, str(e), None
        return _credit_balance_dict(free=free, paid=paid, allowance=allowance), None, now

    if apply_monthly_reset and (now - last_reset) > timedelta(days=PROFILE_PAGES_RESET_INTERVAL_DAYS):
        ok, reset_err, balance = reset_monthly_free_credits(user_id)
        if not ok or balance is None:
            return None, reset_err or "Monthly free-credit reset failed.", None
        return balance, None, now

    return _credit_balance_dict(free=free, paid=paid, allowance=allowance), None, last_reset


def get_total_available_credits(user_id: str) -> tuple[int | None, str | None]:
    """Return free_pages_remaining + paid_pages_remaining (after lazy monthly free reset)."""
    balance, err, _anchor = _load_credit_balance(user_id)
    if balance is None:
        return None, err
    return int(balance["pages_remaining"]), None


def get_credit_balance(user_id: str) -> tuple[dict | None, str | None]:
    """Return full credit balance dict (free, paid, total, allowance)."""
    balance, err, _anchor = _load_credit_balance(user_id)
    return balance, err


def deduct_ocr_credits(user_id: str, pages: int) -> tuple[bool, str | None, dict | None]:
    """Atomically consume OCR credits: free first, then paid. Never goes negative.

    Returns (ok, error, balance_dict). On success balance_dict includes free_used/paid_used.
    """
    pages = int(pages or 0)
    if pages < 0:
        return False, "Credit charge cannot be negative.", None
    if pages == 0:
        balance, err = get_credit_balance(user_id)
        return (True, err, balance) if balance is not None else (False, err or "No profile.", None)

    last_err = "Could not update credit balance."
    for _attempt in range(_CREDIT_UPDATE_MAX_ATTEMPTS):
        balance, err, _anchor = _load_credit_balance(user_id)
        if balance is None:
            return False, err or "No profile row found for this user.", None
        free = int(balance["free_pages_remaining"])
        paid = int(balance["paid_pages_remaining"])
        allowance = int(balance["monthly_free_credit_allowance"])
        total = free + paid
        if total < pages:
            return (
                False,
                f"Not enough credits remaining ({total} left, this file needs {pages}).",
                balance,
            )
        free_used = min(free, pages)
        paid_used = pages - free_used
        new_free = free - free_used
        new_paid = paid - paid_used
        if new_free < 0 or new_paid < 0:
            return False, "Credit calculation error (negative balance prevented).", balance
        try:
            res = (
                supabase_client.table("profiles")
                .update(
                    {
                        "free_pages_remaining": new_free,
                        "paid_pages_remaining": new_paid,
                    }
                )
                .eq("id", user_id)
                .eq("free_pages_remaining", free)
                .eq("paid_pages_remaining", paid)
                .execute()
            )
            rows = getattr(res, "data", None) or []
            if rows:
                return (
                    True,
                    None,
                    _credit_balance_dict(
                        free=new_free,
                        paid=new_paid,
                        allowance=allowance,
                        free_used=free_used,
                        paid_used=paid_used,
                    ),
                )
            last_err = "Credit balance changed while starting this job. Try again."
        except Exception as e:
            print(f"[profiles] deduct_ocr_credits failed: {e}", file=sys.stderr, flush=True)
            return False, str(e), balance
    balance, _err = get_credit_balance(user_id)
    return False, last_err, balance


def refund_ocr_credits(
    user_id: str,
    pages: int,
    *,
    free_used: int | None = None,
    paid_used: int | None = None,
) -> tuple[bool, str | None, dict | None]:
    """Restore OCR credits after a failed charge. Prefer exact free/paid split when known."""
    pages = int(pages or 0)
    if pages <= 0:
        balance, err = get_credit_balance(user_id)
        return (True, err, balance) if balance is not None else (False, err or "No profile.", None)

    if free_used is not None and paid_used is not None:
        free_used = max(0, int(free_used))
        paid_used = max(0, int(paid_used))
        if free_used + paid_used != pages:
            # Fall back to free-first restore when split is inconsistent.
            free_used = None
            paid_used = None

    last_err = "Could not refund credit balance."
    for _attempt in range(_CREDIT_UPDATE_MAX_ATTEMPTS):
        balance, err, _anchor = _load_credit_balance(user_id, apply_monthly_reset=False)
        if balance is None:
            return False, err or "No profile row found for this user.", None
        free = int(balance["free_pages_remaining"])
        paid = int(balance["paid_pages_remaining"])
        allowance = int(balance["monthly_free_credit_allowance"])

        if free_used is not None and paid_used is not None:
            add_free, add_paid = free_used, paid_used
        else:
            # Best-effort reverse of free-first consumption without a recorded split:
            # restore free up to monthly allowance, remainder to paid.
            room_in_free = max(0, allowance - free)
            add_free = min(pages, room_in_free)
            add_paid = pages - add_free

        new_free = free + add_free
        new_paid = paid + add_paid
        try:
            res = (
                supabase_client.table("profiles")
                .update(
                    {
                        "free_pages_remaining": new_free,
                        "paid_pages_remaining": new_paid,
                    }
                )
                .eq("id", user_id)
                .eq("free_pages_remaining", free)
                .eq("paid_pages_remaining", paid)
                .execute()
            )
            rows = getattr(res, "data", None) or []
            if rows:
                return True, None, _credit_balance_dict(free=new_free, paid=new_paid, allowance=allowance)
            last_err = "Credit balance changed while refunding. Try again."
        except Exception as e:
            print(f"[profiles] refund_ocr_credits failed: {e}", file=sys.stderr, flush=True)
            return False, str(e), balance
    balance, _err = get_credit_balance(user_id)
    return False, last_err, balance


def add_paid_credits(user_id: str, credits: int) -> tuple[bool, str | None, dict | None]:
    """Increment ONLY paid_pages_remaining (Stripe purchases). Never touches free credits."""
    credits = int(credits or 0)
    if credits <= 0:
        balance, err = get_credit_balance(user_id)
        return (True, err, balance) if balance is not None else (False, err or "No profile.", None)

    last_err = "Could not add paid credits."
    for _attempt in range(_CREDIT_UPDATE_MAX_ATTEMPTS):
        balance, err, _anchor = _load_credit_balance(user_id, apply_monthly_reset=False)
        if balance is None:
            return False, err or "No profile row found for this user.", None
        free = int(balance["free_pages_remaining"])
        paid = int(balance["paid_pages_remaining"])
        allowance = int(balance["monthly_free_credit_allowance"])
        new_paid = paid + credits
        try:
            res = (
                supabase_client.table("profiles")
                .update({"paid_pages_remaining": new_paid})
                .eq("id", user_id)
                .eq("paid_pages_remaining", paid)
                .execute()
            )
            rows = getattr(res, "data", None) or []
            if rows:
                return True, None, _credit_balance_dict(free=free, paid=new_paid, allowance=allowance)
            last_err = "Paid credit balance changed while applying payment. Try again."
        except Exception as e:
            print(f"[profiles] add_paid_credits failed: {e}", file=sys.stderr, flush=True)
            return False, str(e), balance
    balance, _err = get_credit_balance(user_id)
    return False, last_err, balance


def _stripe_client():
    if not STRIPE_SECRET_KEY:
        raise RuntimeError("STRIPE_SECRET_KEY is not configured.")
    import stripe

    stripe.api_key = STRIPE_SECRET_KEY
    return stripe


def _payment_row_for_checkout_session(checkout_session_id: str) -> dict | None:
    if not supabase_client or not checkout_session_id:
        return None
    try:
        res = (
            supabase_client.table("payments")
            .select("id,stripe_checkout_session_id,status,credits_granted")
            .eq("stripe_checkout_session_id", checkout_session_id)
            .limit(1)
            .execute()
        )
        rows = getattr(res, "data", None) or []
        return rows[0] if rows else None
    except Exception as e:
        print(f"[stripe] payment lookup failed: {e}", file=sys.stderr, flush=True)
        return None


def _is_unique_violation(exc: BaseException) -> bool:
    text = str(exc).lower()
    return (
        "duplicate" in text
        or "unique" in text
        or "23505" in text
        or "already exists" in text
    )


def _delete_payment_by_checkout_session(checkout_session_id: str) -> None:
    """Best-effort compensate if credits could not be granted after insert."""
    if not supabase_client or not checkout_session_id:
        return
    try:
        supabase_client.table("payments").delete().eq(
            "stripe_checkout_session_id", checkout_session_id
        ).execute()
    except Exception as e:
        print(
            f"[stripe] compensate delete failed for session {checkout_session_id}: {e}",
            file=sys.stderr,
            flush=True,
        )


def _extract_checkout_price_id(session_obj) -> str | None:
    """Resolve the purchased Stripe price id from a Checkout Session."""
    meta = getattr(session_obj, "metadata", None) or {}
    if isinstance(meta, dict):
        from_meta = (meta.get("price_id") or "").strip()
        if from_meta:
            return from_meta
    try:
        line_items = getattr(session_obj, "line_items", None)
        data = getattr(line_items, "data", None) if line_items is not None else None
        if data:
            price = getattr(data[0], "price", None)
            pid = getattr(price, "id", None) if price is not None else None
            if pid:
                return str(pid)
    except Exception:
        pass
    return None


def _fulfill_checkout_session(session_obj) -> tuple[bool, str, int]:
    """Insert payment + grant paid credits exactly once. Returns (ok, message, http_status)."""
    if not supabase_client:
        return False, "Supabase is not configured.", 500

    checkout_session_id = (getattr(session_obj, "id", None) or "").strip()
    if not checkout_session_id:
        return False, "Checkout session missing id.", 400

    existing = _payment_row_for_checkout_session(checkout_session_id)
    if existing:
        print(
            f"[stripe] duplicate webhook ignored session_id={checkout_session_id}",
            file=sys.stderr,
            flush=True,
        )
        return True, "already_processed", 200

    meta = getattr(session_obj, "metadata", None) or {}
    if not isinstance(meta, dict):
        meta = {}
    user_id = (meta.get("user_id") or "").strip()
    if not user_id:
        return False, "Checkout session missing metadata.user_id.", 400

    price_id = _extract_checkout_price_id(session_obj)
    if not price_id or price_id not in PRICE_ID_TO_CREDITS:
        return False, f"Unknown or missing price_id on checkout session: {price_id!r}", 400
    credits_granted = int(PRICE_ID_TO_CREDITS[price_id])

    payment_intent = getattr(session_obj, "payment_intent", None)
    if payment_intent is not None and not isinstance(payment_intent, str):
        payment_intent = getattr(payment_intent, "id", None)
    payment_intent_id = (str(payment_intent).strip() if payment_intent else None) or None

    amount_total = getattr(session_obj, "amount_total", None)
    try:
        amount_paid_cents = int(amount_total) if amount_total is not None else None
    except (TypeError, ValueError):
        amount_paid_cents = None
    currency = (getattr(session_obj, "currency", None) or "usd").strip().lower()

    payment_row = {
        "user_id": user_id,
        "provider": "stripe",
        "stripe_checkout_session_id": checkout_session_id,
        "stripe_payment_intent_id": payment_intent_id,
        "stripe_price_id": price_id,
        "credits_granted": credits_granted,
        "amount_paid_cents": amount_paid_cents,
        "currency": currency,
        "status": "completed",
    }

    try:
        supabase_client.table("payments").insert(payment_row).execute()
        print(
            f"[stripe] payment inserted session_id={checkout_session_id} "
            f"user_id={user_id} credits={credits_granted}",
            file=sys.stderr,
            flush=True,
        )
    except Exception as e:
        if _is_unique_violation(e) or _payment_row_for_checkout_session(checkout_session_id):
            print(
                f"[stripe] duplicate webhook ignored session_id={checkout_session_id}",
                file=sys.stderr,
                flush=True,
            )
            return True, "already_processed", 200
        print(f"[stripe] payment insert failed: {e}", file=sys.stderr, flush=True)
        return False, "Could not record payment.", 500

    ok, err, _balance = add_paid_credits(user_id, credits_granted)
    if not ok:
        print(
            f"[stripe] credits grant failed session_id={checkout_session_id} "
            f"user_id={user_id} credits={credits_granted}: {err}",
            file=sys.stderr,
            flush=True,
        )
        _delete_payment_by_checkout_session(checkout_session_id)
        return False, err or "Could not grant credits.", 500

    print(
        f"[stripe] credits granted session_id={checkout_session_id} "
        f"user_id={user_id} credits={credits_granted}",
        file=sys.stderr,
        flush=True,
    )
    print(
        f"[stripe] checkout completed session_id={checkout_session_id} user_id={user_id}",
        file=sys.stderr,
        flush=True,
    )
    return True, "ok", 200


_QPDF_BIN = shutil.which("qpdf")


def _pdf_is_linearized(raw: bytes) -> bool:
    """A linearized ("fast web view") PDF declares /Linearized in its first object."""
    return b"/Linearized" in raw[:2048]


def _pdf_is_linearized_path(file_path: str) -> bool:
    with open(file_path, "rb") as f:
        head = f.read(2048)
    return b"/Linearized" in head


def _linearize_pdf_file(in_path: str) -> str:
    """Return a linearized PDF path, or in_path when linearization is skipped/failed."""
    prof = _active_upload_profile()
    t0 = time.perf_counter()
    if not in_path or not os.path.isfile(in_path):
        if prof:
            prof.log_step("pdf_linearize", time.perf_counter() - t0, skipped="missing_input")
        return in_path
    with open(in_path, "rb") as f:
        if f.read(4) != b"%PDF":
            if prof:
                prof.log_step("pdf_linearize", time.perf_counter() - t0, skipped="not_pdf")
            return in_path
    if _pdf_is_linearized_path(in_path):
        if prof:
            prof.log_step("pdf_linearize", time.perf_counter() - t0, skipped="already_linearized")
        return in_path
    if not _QPDF_BIN:
        print("[linearize] qpdf not found on PATH; skipping linearization", file=sys.stderr, flush=True)
        if prof:
            prof.log_step("pdf_linearize", time.perf_counter() - t0, skipped="no_qpdf")
        return in_path

    out_path = in_path + ".lin.pdf"
    try:
        proc = subprocess.run(
            [_QPDF_BIN, "--linearize", in_path, out_path],
            capture_output=True,
            timeout=120,
        )
        if proc.returncode not in (0, 3) or not os.path.exists(out_path):
            print(
                f"[linearize] qpdf failed (exit {proc.returncode}): "
                f"{proc.stderr.decode('utf-8', 'replace')[:300]}",
                file=sys.stderr,
                flush=True,
            )
            if prof:
                prof.log_step("pdf_linearize", time.perf_counter() - t0, result="failed")
            return in_path
        with open(out_path, "rb") as f:
            if f.read(4) != b"%PDF":
                if prof:
                    prof.log_step("pdf_linearize", time.perf_counter() - t0, result="invalid_output")
                return in_path
        if prof:
            prof.log_step(
                "pdf_linearize",
                time.perf_counter() - t0,
                result="ok",
                input_bytes=os.path.getsize(in_path),
                output_bytes=os.path.getsize(out_path),
            )
        return out_path
    except Exception as e:
        print(f"[linearize] error: {e}", file=sys.stderr, flush=True)
        if prof:
            prof.log_step("pdf_linearize", time.perf_counter() - t0, result="error")
        return in_path


def _linearize_pdf_bytes(raw: bytes) -> bytes:
    """Return a linearized copy of a PDF so PDF.js can render page 1 from the first
    ~few hundred KB via a single range request instead of downloading the whole file.

    Best-effort: if qpdf is unavailable, the input isn't a PDF, it's already linearized,
    or the tool fails, the original bytes are returned unchanged so ingestion never breaks.
    """
    prof = _active_upload_profile()
    t0 = time.perf_counter()
    if not raw or raw[:4] != b"%PDF":
        if prof:
            prof.log_step("pdf_linearize", time.perf_counter() - t0, skipped="not_pdf")
        return raw
    if _pdf_is_linearized(raw):
        if prof:
            prof.log_step("pdf_linearize", time.perf_counter() - t0, skipped="already_linearized")
        return raw
    if not _QPDF_BIN:
        print("[linearize] qpdf not found on PATH; skipping linearization", file=sys.stderr, flush=True)
        if prof:
            prof.log_step("pdf_linearize", time.perf_counter() - t0, skipped="no_qpdf")
        return raw

    in_path = out_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as fin:
            fin.write(raw)
            in_path = fin.name
        out_path = in_path + ".lin.pdf"
        proc = subprocess.run(
            [_QPDF_BIN, "--linearize", in_path, out_path],
            capture_output=True,
            timeout=120,
        )
        # qpdf exit codes: 0 = success, 3 = success with warnings. Both produce valid output.
        if proc.returncode not in (0, 3) or not os.path.exists(out_path):
            print(
                f"[linearize] qpdf failed (exit {proc.returncode}): "
                f"{proc.stderr.decode('utf-8', 'replace')[:300]}",
                file=sys.stderr,
                flush=True,
            )
            if prof:
                prof.log_step("pdf_linearize", time.perf_counter() - t0, result="failed")
            return raw
        with open(out_path, "rb") as f:
            out = f.read()
        result = out if out and out[:4] == b"%PDF" else raw
        if prof:
            prof.log_step(
                "pdf_linearize",
                time.perf_counter() - t0,
                result="ok",
                input_bytes=len(raw),
                output_bytes=len(result),
            )
        return result
    except Exception as e:
        print(f"[linearize] error: {e}", file=sys.stderr, flush=True)
        if prof:
            prof.log_step("pdf_linearize", time.perf_counter() - t0, result="error")
        return raw
    finally:
        for p in (in_path, out_path):
            if p and os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass


def _mimetype_for_stored_original(filename: str | None, raw: bytes) -> str:
    if len(raw) >= 4 and raw[:4] == b"%PDF":
        return "application/pdf"
    fn = (filename or "").lower()
    guessed, _ = mimetypes.guess_type(fn)
    if guessed and guessed.startswith("image/"):
        return guessed
    try:
        im = Image.open(io.BytesIO(raw))
        try:
            fmt = (im.format or "").upper()
        finally:
            im.close()
        return {
            "JPEG": "image/jpeg",
            "JPG": "image/jpeg",
            "PNG": "image/png",
            "WEBP": "image/webp",
            "GIF": "image/gif",
            "TIFF": "image/tiff",
            "BMP": "image/bmp",
        }.get(fmt, "application/octet-stream")
    except Exception:
        return "application/octet-stream"


def _render_stored_document_page_png(file_bytes: bytes, kind: str, page_num: int) -> bytes:
    if kind == "pdf":
        doc = fitz.open(stream=file_bytes, filetype="pdf")
        try:
            if page_num < 1 or page_num > len(doc):
                raise ValueError("Page number out of range")
            page = doc[page_num - 1]
            mat = fitz.Matrix(2, 2)
            pix = page.get_pixmap(matrix=mat)
            return pix.tobytes("png")
        finally:
            doc.close()
    if page_num != 1:
        raise ValueError("Page number out of range")
    im = Image.open(io.BytesIO(file_bytes))
    try:
        im.load()
        rgb = _pil_to_rgb_white_background(im)
        buf = io.BytesIO()
        rgb.save(buf, format="PNG")
        return buf.getvalue()
    finally:
        try:
            im.close()
        except Exception:
            pass


def _import_bundle_cleanup_paths(
    original_file_path: str | None,
    temp_json_paths: list[str] | None = None,
) -> None:
    if original_file_path:
        _storage_delete(SUPABASE_STORAGE_BUCKET, original_file_path)
    for path in temp_json_paths or []:
        if path:
            delete_from_gbucket(path)


@app.route("/api/files/import-bundle", methods=["POST"])
def api_files_import_bundle():
    """Import an existing local bundle (original file + original/editable JSON) without OCR."""
    if not supabase_client:
        return jsonify({"error": "Supabase is not configured on the server."}), 503

    token = _bearer_token_from_request()
    if not token:
        return jsonify({"error": "Missing Authorization bearer token."}), 401
    user_id = supabase_access_token_to_user_id(token)
    if not user_id:
        return jsonify({"error": "Invalid or expired session token."}), 401

    if request.is_json:
        body = request.get_json(silent=True) or {}
        file_row_id = (body.get("file_id") or "").strip() or str(uuid.uuid4())
        original_file_path = (body.get("file_path") or "").strip()
        original_json_path = (body.get("original_json_path") or "").strip()
        editable_json_path = (body.get("editable_json_path") or "").strip()
        file_name = (body.get("file_name") or "").strip() or "imported"

        if not original_file_path:
            return jsonify({"error": "file_path is required."}), 400
        if not original_json_path:
            return jsonify({"error": "original_json_path is required."}), 400
        if not editable_json_path:
            return jsonify({"error": "editable_json_path is required."}), 400

        for path in (original_file_path, original_json_path, editable_json_path):
            if not _user_owned_storage_path(user_id, path):
                return jsonify({"error": "Storage paths must be under your user folder."}), 403

        try:
            original_data, editable_data = _download_json_dicts_from_storage_parallel(
                original_json_path, editable_json_path
            )
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except Exception as e:
            print(f"[import-bundle] JSON validation download failed: {e}", file=sys.stderr, flush=True)
            return jsonify({"error": f"Could not read uploaded JSON from storage: {e!s}"}), 502

        if not _ocr_json_page_keys(original_data):
            return jsonify({"error": "original_json does not look like OCR output (expected page_N keys)."}), 400
        if not _ocr_json_page_keys(editable_data):
            return jsonify({"error": "editable_json does not look like OCR output (expected page_N keys)."}), 400

        page_keys = _ocr_json_page_keys(editable_data)
        doc_pages = len(page_keys)
        doc_kind = _kind_from_filename(file_name or original_file_path)

        try:
            json_fields, page_count = _store_bundle_json(user_id, file_row_id, original_data, editable_data)
        except Exception as e:
            print(f"[import-bundle] JSON storage failed: {e}", file=sys.stderr, flush=True)
            return jsonify({"error": f"Could not store OCR JSON: {e!s}"}), 502

        insert_row = {
            "id": file_row_id,
            "user_id": user_id,
            "original_file_path": original_file_path,
            "file_name": file_name,
            "status": "completed",
            "credits_used": None,
            **json_fields,
        }

        try:
            ins = supabase_client.table("files").insert(insert_row).execute()
        except Exception as e:
            print(f"[import-bundle] DB insert failed: {e}", file=sys.stderr, flush=True)
            return jsonify({"error": f"Database insert failed: {e!s}"}), 500

        row = None
        if getattr(ins, "data", None) is not None:
            row = ins.data[0] if isinstance(ins.data, list) and len(ins.data) else ins.data

        _defer_import_temp_json_cleanup(original_json_path, editable_json_path)

        return jsonify(
            {
                "success": True,
                "file_id": file_row_id,
                "file_name": file_name,
                "page_count": page_count or doc_pages,
                "kind": doc_kind,
                "row": row,
            }
        )

    original_upload = request.files.get("original_file")
    original_json_upload = request.files.get("original_json")
    editable_json_upload = request.files.get("editable_json")
    if not original_upload or not original_upload.filename:
        return jsonify({"error": "original_file is required (PDF or image)."}), 400
    if not original_json_upload or not original_json_upload.filename:
        return jsonify({"error": "original_json is required."}), 400
    if not editable_json_upload or not editable_json_upload.filename:
        return jsonify({"error": "editable_json is required."}), 400

    original_bytes = original_upload.read()
    if not original_bytes:
        return jsonify({"error": "Original file was empty."}), 400

    try:
        original_data, _ = _parse_uploaded_json_file(original_json_upload)
        editable_data, _ = _parse_uploaded_json_file(editable_json_upload)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    if not _ocr_json_page_keys(original_data):
        return jsonify({"error": "original_json does not look like OCR output (expected page_N keys)."}), 400
    if not _ocr_json_page_keys(editable_data):
        return jsonify({"error": "editable_json does not look like OCR output (expected page_N keys)."}), 400

    try:
        doc_kind, doc_pages = _document_kind_and_page_count(original_bytes)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    # Linearize PDFs so the viewer streams page 1 quickly (best-effort; no-op otherwise).
    if doc_kind == "pdf":
        original_bytes = _linearize_pdf_bytes(original_bytes)

    file_name = (request.form.get("file_name") or original_upload.filename or "imported").strip()
    safe_name = _sanitize_storage_filename(file_name)
    file_row_id = str(uuid.uuid4())
    original_file_path = f"{user_id}/{file_row_id}-{safe_name}"

    try:
        content_type = _mimetype_for_stored_original(file_name, original_bytes)
        _storage_upload(SUPABASE_STORAGE_BUCKET, original_file_path, original_bytes, content_type)
    except Exception as e:
        print(f"[import-bundle] original upload failed: {e}", file=sys.stderr, flush=True)
        return jsonify({"error": f"Could not store original file: {e!s}"}), 502

    try:
        json_fields, page_count = _store_bundle_json(user_id, file_row_id, original_data, editable_data)
    except Exception as e:
        _import_bundle_cleanup_paths(original_file_path)
        print(f"[import-bundle] JSON storage failed: {e}", file=sys.stderr, flush=True)
        return jsonify({"error": f"Could not store OCR JSON: {e!s}"}), 502

    insert_row = {
        "id": file_row_id,
        "user_id": user_id,
        "original_file_path": original_file_path,
        "file_name": file_name,
        "status": "completed",
        "credits_used": None,
        **json_fields,
    }

    try:
        ins = supabase_client.table("files").insert(insert_row).execute()
    except Exception as e:
        _import_bundle_cleanup_paths(original_file_path)
        print(f"[import-bundle] DB insert failed: {e}", file=sys.stderr, flush=True)
        return jsonify({"error": f"Database insert failed: {e!s}"}), 500

    row = None
    if getattr(ins, "data", None) is not None:
        row = ins.data[0] if isinstance(ins.data, list) and len(ins.data) else ins.data

    page_keys = _ocr_json_page_keys(editable_data)
    return jsonify(
        {
            "success": True,
            "file_id": file_row_id,
            "file_name": file_name,
            "page_count": page_count or len(page_keys) or doc_pages,
            "kind": doc_kind,
            "row": row,
        }
    )


@app.route("/api/files/<uuid:file_id>/name", methods=["PATCH"])
def api_file_rename(file_id):
    token = _bearer_token_from_request()
    if not token:
        return jsonify({"error": "Missing Authorization bearer token."}), 401
    user_id = supabase_access_token_to_user_id(token)
    if not user_id:
        return jsonify({"error": "Invalid or expired token."}), 401
    if not request.is_json:
        return jsonify({"error": "Expected application/json."}), 400

    access = _file_access_for_user(str(file_id), user_id)
    if not access:
        return jsonify({"error": "File not found."}), 404
    if not access.is_owner:
        return jsonify({"error": "Only the file owner can rename this file."}), 403

    body = request.get_json(silent=True) or {}
    file_name = str(body.get("file_name") or "").strip()
    if not file_name:
        return jsonify({"error": "File name cannot be empty."}), 400
    if len(file_name) > 255:
        return jsonify({"error": "File name must be 255 characters or fewer."}), 400
    if any(ord(ch) < 32 for ch in file_name):
        return jsonify({"error": "File name contains unsupported characters."}), 400

    old_name = str(access.row.get("file_name") or "").strip()
    if old_name.lower().endswith(".pdf") and not file_name.lower().endswith(".pdf"):
        return jsonify({"error": "PDF file names must keep the .pdf extension."}), 400
    if file_name == old_name:
        return jsonify({"success": True, "file_id": str(file_id), "file_name": file_name})

    try:
        result = (
            supabase_client.table("files")
            .update({"file_name": file_name})
            .eq("id", str(file_id))
            .eq("user_id", user_id)
            .execute()
        )
        rows = getattr(result, "data", None) or []
        if not rows:
            return jsonify({"error": "File could not be renamed."}), 409
        return jsonify({"success": True, "file_id": str(file_id), "file_name": file_name})
    except Exception as e:
        print(f"[api/files/{file_id}/name] rename failed: {e}", file=sys.stderr, flush=True)
        return jsonify({"error": "Could not rename the file."}), 500


@app.route("/api/files/<uuid:file_id>/json/meta", methods=["GET"])
def api_file_json_meta(file_id):
    try:
        token = _bearer_token_from_request()
        if not token:
            return jsonify({"error": "Missing Authorization bearer token."}), 401
        user_id = supabase_access_token_to_user_id(token)
        if not user_id:
            return jsonify({"error": "Invalid or expired token."}), 401
        access = _file_access_for_user(str(file_id), user_id)
        if not access:
            return jsonify({"error": "File not found."}), 404
        row = access.row
        _status, err_resp = _file_row_json_status(row)
        if err_resp:
            return err_resp
        page_count = _page_count_for_row(row)
        owner_username = None
        if not access.is_owner:
            owner_username = profile_user_display(
                supabase_client, access.owner_user_id, ensure_if_missing=True
            ).get("username")
        return jsonify(
            {
                "file_id": str(file_id),
                "file_name": row.get("file_name"),
                "page_count": page_count,
                "original_json_path": row.get("original_json_path"),
                "editable_json_path": row.get("editable_json_path"),
                "storage": "inline" if _row_uses_inline_json(row) else "per-page",
                "access": access_payload(access, owner_username=owner_username),
            }
        )
    except Exception as e:
        print(f"[api/files/{file_id}/json/meta] error: {e}", file=sys.stderr, flush=True)
        traceback.print_exc(file=sys.stderr)
        return jsonify({"error": str(e), "error_type": "server"}), 500


@app.route("/api/files/<uuid:file_id>/json/<int:page_num>", methods=["GET"])
def api_file_get_json_page(file_id, page_num):
    try:
        token = _bearer_token_from_request()
        if not token:
            return jsonify({"error": "Missing Authorization bearer token."}), 401
        user_id = supabase_access_token_to_user_id(token)
        if not user_id:
            return jsonify({"error": "Invalid or expired token."}), 401
        row = _files_row_for_user(str(file_id), user_id)
        if not row:
            return jsonify({"error": "File not found."}), 404
        _status, err_resp = _file_row_json_status(row)
        if err_resp:
            return err_resp
        if page_num < 1:
            return jsonify({"error": "Page number must be >= 1."}), 400
        page_count = _page_count_for_row(row)
        if page_count and page_num > page_count:
            return jsonify({"error": "Page number out of range."}), 400
        data = _load_editable_page_data(row, page_num)
        return _json_http_response(data)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        print(f"[api/files/{file_id}/json/{page_num}] error: {e}", file=sys.stderr, flush=True)
        traceback.print_exc(file=sys.stderr)
        return jsonify({"error": str(e), "error_type": "server"}), 500


@app.route("/api/files/<uuid:file_id>/json/<int:page_num>/original", methods=["GET"])
def api_file_get_json_page_original(file_id, page_num):
    try:
        token = _bearer_token_from_request()
        if not token:
            return jsonify({"error": "Missing Authorization bearer token."}), 401
        user_id = supabase_access_token_to_user_id(token)
        if not user_id:
            return jsonify({"error": "Invalid or expired token."}), 401
        row = _files_row_for_user(str(file_id), user_id)
        if not row:
            return jsonify({"error": "File not found."}), 404
        _status, err_resp = _file_row_json_status(row)
        if err_resp:
            return err_resp
        if page_num < 1:
            return jsonify({"error": "Page number must be >= 1."}), 400
        page_count = _page_count_for_row(row)
        if page_count and page_num > page_count:
            return jsonify({"error": "Page number out of range."}), 400
        data = _load_original_page_data(row, page_num)
        return _json_http_response(data)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        print(f"[api/files/{file_id}/json/{page_num}/original] error: {e}", file=sys.stderr, flush=True)
        traceback.print_exc(file=sys.stderr)
        return jsonify({"error": str(e), "error_type": "server"}), 500


@app.route("/api/files/<uuid:file_id>/json/<int:page_num>", methods=["POST"])
def api_file_save_json_page(file_id, page_num):
    token = _bearer_token_from_request()
    if not token:
        return jsonify({"error": "Missing Authorization bearer token."}), 401
    user_id = supabase_access_token_to_user_id(token)
    if not user_id:
        return jsonify({"error": "Invalid or expired token."}), 401
    access = _file_access_for_user(str(file_id), user_id)
    if not access:
        return jsonify({"error": "File not found."}), 404
    if not access.can_edit:
        return jsonify({"error": "You do not have permission to edit this file."}), 403
    row = access.row
    _status, err_resp = _file_row_json_status(row)
    if err_resp:
        return err_resp
    if page_num < 1:
        return jsonify({"error": "Page number must be >= 1."}), 400
    if not request.is_json:
        return jsonify({"error": "Expected application/json."}), 400
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"error": "JSON body must be an object."}), 400
    page_count = _page_count_for_row(row)
    if page_count and page_num > page_count:
        return jsonify({"error": "Page number out of range."}), 400
    try:
        _save_editable_page_data(row, page_num, body)
    except Exception as e:
        print(f"[api/files/{file_id}/json/{page_num}] save failed: {e}", file=sys.stderr, flush=True)
        return jsonify({"error": str(e)}), 500
    return jsonify({"success": True, "page": page_num})


def _validate_page_num_for_row(row: dict, page_num: int):
    if page_num < 1:
        return jsonify({"error": "Page number must be >= 1."}), 400
    page_count = _page_count_for_row(row)
    if page_count and page_num > page_count:
        return jsonify({"error": "Page number out of range."}), 400
    return None


@app.route("/api/files/<uuid:file_id>/pages/<int:page_num>/metadata", methods=["GET"])
def api_file_page_metadata_get(file_id, page_num):
    token = _bearer_token_from_request()
    if not token:
        return jsonify({"error": "Missing Authorization bearer token."}), 401
    user_id = supabase_access_token_to_user_id(token)
    if not user_id:
        return jsonify({"error": "Invalid or expired token."}), 401
    access = _file_access_for_user(str(file_id), user_id)
    if not access:
        return jsonify({"error": "File not found."}), 404
    row = access.row
    _status, err_resp = _file_row_json_status(row)
    if err_resp:
        return err_resp
    page_err = _validate_page_num_for_row(row, page_num)
    if page_err:
        return page_err
    if not supabase_client:
        return jsonify({"error": "Supabase is not configured."}), 503
    try:
        meta = get_page_metadata(supabase_client, str(file_id), page_num)
        return jsonify(meta)
    except Exception as e:
        print(f"[api/files/{file_id}/pages/{page_num}/metadata] get failed: {e}", file=sys.stderr, flush=True)
        traceback.print_exc(file=sys.stderr)
        return jsonify({"error": str(e), "error_type": "server"}), 500


@app.route("/api/files/<uuid:file_id>/pages/<int:page_num>/metadata", methods=["PUT"])
def api_file_page_metadata_upsert(file_id, page_num):
    token = _bearer_token_from_request()
    if not token:
        return jsonify({"error": "Missing Authorization bearer token."}), 401
    user_id = supabase_access_token_to_user_id(token)
    if not user_id:
        return jsonify({"error": "Invalid or expired token."}), 401
    access = _file_access_for_user(str(file_id), user_id)
    if not access:
        return jsonify({"error": "File not found."}), 404
    if not access.can_edit:
        return jsonify({"error": "You do not have permission to edit page metadata."}), 403
    row = access.row
    _status, err_resp = _file_row_json_status(row)
    if err_resp:
        return err_resp
    page_err = _validate_page_num_for_row(row, page_num)
    if page_err:
        return page_err
    if not request.is_json:
        return jsonify({"error": "Expected application/json."}), 400
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"error": "JSON body must be an object."}), 400
    patch = normalize_metadata_patch(body)
    if not patch:
        return jsonify({"error": "No metadata fields to update."}), 400
    if not supabase_client:
        return jsonify({"error": "Supabase is not configured."}), 503
    try:
        meta = upsert_page_metadata(supabase_client, str(file_id), page_num, user_id, patch)
        return jsonify(meta)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        print(f"[api/files/{file_id}/pages/{page_num}/metadata] upsert failed: {e}", file=sys.stderr, flush=True)
        traceback.print_exc(file=sys.stderr)
        return jsonify({"error": str(e), "error_type": "server"}), 500


@app.route("/api/files/<uuid:file_id>/metadata/collaborators", methods=["GET"])
def api_file_metadata_collaborators(file_id):
    token = _bearer_token_from_request()
    if not token:
        return jsonify({"error": "Missing Authorization bearer token."}), 401
    user_id = supabase_access_token_to_user_id(token)
    if not user_id:
        return jsonify({"error": "Invalid or expired token."}), 401
    access = _file_access_for_user(str(file_id), user_id)
    if not access:
        return jsonify({"error": "File not found."}), 404
    if not supabase_client:
        return jsonify({"error": "Supabase is not configured."}), 503
    try:
        collaborators = list_metadata_collaborators(supabase_client, str(file_id), access.owner_user_id)
        return jsonify({"collaborators": collaborators})
    except Exception as e:
        print(f"[api/files/{file_id}/metadata/collaborators] error: {e}", file=sys.stderr, flush=True)
        traceback.print_exc(file=sys.stderr)
        return jsonify({"error": str(e), "error_type": "server"}), 500


@app.route("/api/files/shared-with-me", methods=["GET"])
def api_files_shared_with_me():
    token = _bearer_token_from_request()
    if not token:
        return jsonify({"error": "Missing Authorization bearer token."}), 401
    user_id = supabase_access_token_to_user_id(token)
    if not user_id:
        return jsonify({"error": "Invalid or expired token."}), 401
    if not supabase_client:
        return jsonify({"error": "Supabase is not configured."}), 503
    try:
        res = (
            supabase_client.table("shared_files")
            .select(
                "id,permission,created_at,file_id,"
                "files(id,file_name,original_file_path,status,credits_used,processing_duration_seconds,created_at,user_id)"
            )
            .eq("shared_with_user_id", user_id)
            .order("created_at", desc=True)
            .execute()
        )
        items = []
        for share in getattr(res, "data", None) or []:
            file_row = share.get("files")
            if not isinstance(file_row, dict):
                continue
            owner_id = str(file_row.get("user_id") or "")
            owner = (
                profile_user_display(supabase_client, owner_id, ensure_if_missing=True)
                if owner_id
                else {"username": None}
            )
            items.append(
                {
                    "share_id": share.get("id"),
                    "permission": share.get("permission") or "view",
                    "created_at": share.get("created_at"),
                    "file": {
                        "id": file_row.get("id"),
                        "file_name": file_row.get("file_name"),
                        "original_file_path": file_row.get("original_file_path"),
                        "status": file_row.get("status"),
                        "credits_used": file_row.get("credits_used"),
                        "processing_duration_seconds": file_row.get("processing_duration_seconds"),
                        "created_at": file_row.get("created_at"),
                    },
                    "owner": {
                        "username": owner.get("username"),
                    },
                }
            )
        return jsonify({"files": items})
    except Exception as e:
        print(f"[api/files/shared-with-me] error: {e}", file=sys.stderr, flush=True)
        traceback.print_exc(file=sys.stderr)
        return jsonify({"error": str(e), "error_type": "server"}), 500


@app.route("/api/files/<uuid:file_id>/shares", methods=["GET"])
def api_file_list_shares(file_id):
    token = _bearer_token_from_request()
    if not token:
        return jsonify({"error": "Missing Authorization bearer token."}), 401
    user_id = supabase_access_token_to_user_id(token)
    if not user_id:
        return jsonify({"error": "Invalid or expired token."}), 401
    access = _file_access_for_user(str(file_id), user_id)
    if not access or not access.is_owner:
        return jsonify({"error": "File not found."}), 404
    if not supabase_client:
        return jsonify({"error": "Supabase is not configured."}), 503
    try:
        res = (
            supabase_client.table("shared_files")
            .select("id,shared_with_user_id,permission,created_at")
            .eq("file_id", str(file_id))
            .order("created_at")
            .execute()
        )
        shares = []
        for row in getattr(res, "data", None) or []:
            uid = str(row.get("shared_with_user_id") or "")
            disp = (
                profile_user_display(supabase_client, uid, ensure_if_missing=True)
                if uid
                else {"username": None}
            )
            shares.append(
                {
                    "id": row.get("id"),
                    "permission": row.get("permission") or "view",
                    "created_at": row.get("created_at"),
                    "username": disp.get("username"),
                }
            )
        return jsonify({"shares": shares})
    except Exception as e:
        print(f"[api/files/{file_id}/shares] error: {e}", file=sys.stderr, flush=True)
        traceback.print_exc(file=sys.stderr)
        return jsonify({"error": str(e), "error_type": "server"}), 500


@app.route("/api/files/<uuid:file_id>/share", methods=["POST"])
def api_file_share(file_id):
    token = _bearer_token_from_request()
    if not token:
        return jsonify({"error": "Missing Authorization bearer token."}), 401
    user_id = supabase_access_token_to_user_id(token)
    if not user_id:
        return jsonify({"error": "Invalid or expired token."}), 401
    access = _file_access_for_user(str(file_id), user_id)
    if not access or not access.is_owner:
        return jsonify({"error": "File not found."}), 404
    if not request.is_json:
        return jsonify({"error": "Expected application/json."}), 400
    body = request.get_json(silent=True) or {}
    username_raw = (body.get("username") or "").strip()
    if not username_raw:
        return jsonify({"error": "Recipient username is required."}), 400
    username = normalize_username_input(username_raw)
    fmt_err = validate_username_format(username)
    if fmt_err:
        return jsonify({"error": fmt_err}), 400
    permission = (body.get("permission") or "view").strip().lower()
    if permission not in ("view", "edit"):
        return jsonify({"error": "Permission must be 'view' or 'edit'."}), 400
    if not supabase_client:
        return jsonify({"error": "Supabase is not configured."}), 503
    recipient_id = lookup_user_id_by_username(supabase_client, username)
    if not recipient_id:
        return jsonify(
            {
                "error": "No user found with that username. Check the spelling and try again.",
            }
        ), 404
    if recipient_id == str(user_id):
        return jsonify({"error": "You cannot share a file with yourself."}), 400
    try:
        existing = (
            supabase_client.table("shared_files")
            .select("id,permission")
            .eq("file_id", str(file_id))
            .eq("shared_with_user_id", recipient_id)
            .limit(1)
            .execute()
        )
        rows = getattr(existing, "data", None) or []
        if rows:
            return jsonify(
                {
                    "success": True,
                    "share_id": rows[0].get("id"),
                    "already_shared": True,
                    "permission": rows[0].get("permission") or "view",
                }
            )
        ins = (
            supabase_client.table("shared_files")
            .insert(
                {
                    "file_id": str(file_id),
                    "shared_with_user_id": recipient_id,
                    "permission": permission,
                }
            )
            .execute()
        )
        inserted = (getattr(ins, "data", None) or [None])[0]
        share_id = inserted.get("id") if isinstance(inserted, dict) else None
        return jsonify({"success": True, "share_id": share_id, "permission": permission})
    except Exception as e:
        print(f"[api/files/{file_id}/share] error: {e}", file=sys.stderr, flush=True)
        traceback.print_exc(file=sys.stderr)
        return jsonify({"error": str(e), "error_type": "server"}), 500


@app.route("/api/files/<uuid:file_id>/shares/<uuid:share_id>", methods=["DELETE"])
def api_file_unshare(file_id, share_id):
    token = _bearer_token_from_request()
    if not token:
        return jsonify({"error": "Missing Authorization bearer token."}), 401
    user_id = supabase_access_token_to_user_id(token)
    if not user_id:
        return jsonify({"error": "Invalid or expired token."}), 401
    access = _file_access_for_user(str(file_id), user_id)
    if not access or not access.is_owner:
        return jsonify({"error": "File not found."}), 404
    if not supabase_client:
        return jsonify({"error": "Supabase is not configured."}), 503
    try:
        supabase_client.table("shared_files").delete().eq("id", str(share_id)).eq(
            "file_id", str(file_id)
        ).execute()
        return jsonify({"success": True})
    except Exception as e:
        print(f"[api/files/{file_id}/shares/{share_id}] error: {e}", file=sys.stderr, flush=True)
        traceback.print_exc(file=sys.stderr)
        return jsonify({"error": str(e), "error_type": "server"}), 500


@app.route("/api/files/<uuid:file_id>/transfer-ownership", methods=["POST"])
def api_file_transfer_ownership(file_id):
    token = _bearer_token_from_request()
    if not token:
        return jsonify({"error": "Missing Authorization bearer token."}), 401
    user_id = supabase_access_token_to_user_id(token)
    if not user_id:
        return jsonify({"error": "Invalid or expired token."}), 401
    access = _file_access_for_user(str(file_id), user_id)
    if not access or not access.is_owner:
        return jsonify({"error": "File not found."}), 404
    if not request.is_json:
        return jsonify({"error": "Expected application/json."}), 400
    body = request.get_json(silent=True) or {}
    username_raw = (body.get("username") or "").strip()
    if not username_raw:
        return jsonify({"error": "Recipient username is required."}), 400
    username = normalize_username_input(username_raw)
    fmt_err = validate_username_format(username)
    if fmt_err:
        return jsonify({"error": fmt_err}), 400
    if not supabase_client:
        return jsonify({"error": "Supabase is not configured."}), 503
    recipient_id = lookup_user_id_by_username(supabase_client, username)
    if not recipient_id:
        return jsonify(
            {
                "error": "No user found with that username. Check the spelling and try again.",
            }
        ), 404
    if recipient_id == str(user_id):
        return jsonify({"error": "You cannot transfer ownership to yourself."}), 400
    try:
        result = transfer_file_ownership(
            supabase_client,
            file_id=str(file_id),
            current_owner_id=str(user_id),
            recipient_id=recipient_id,
        )
        return jsonify(result)
    except TransferError as te:
        return jsonify({"error": te.message}), te.status_code
    except Exception as e:
        print(f"[api/files/{file_id}/transfer-ownership] error: {e}", file=sys.stderr, flush=True)
        traceback.print_exc(file=sys.stderr)
        return jsonify({"error": str(e), "error_type": "server"}), 500


@app.route("/api/files/<uuid:file_id>/json", methods=["GET"])
def api_file_get_json_removed(file_id):
    return jsonify({"error": "Full-document JSON is no longer supported. Use /json/meta and /json/<page>."}), 410


@app.route("/api/files/<uuid:file_id>/pdf/info", methods=["GET"])
def api_file_pdf_info(file_id):
    token = _bearer_token_from_request()
    if not token:
        return jsonify({"error": "Missing Authorization bearer token."}), 401
    user_id = supabase_access_token_to_user_id(token)
    if not user_id:
        return jsonify({"error": "Invalid or expired token."}), 401
    row = _files_row_for_user(str(file_id), user_id)
    if not row:
        return jsonify({"error": "File not found."}), 404
    path = row.get("original_file_path")
    if not path:
        return jsonify({"error": "No storage path on record."}), 400
    try:
        raw = download_from_gbucket(path)
    except Exception as e:
        return jsonify({"error": f"Storage download failed: {e!s}"}), 502
    try:
        kind, page_count = _document_kind_and_page_count(raw)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"page_count": page_count, "kind": kind})


def _kind_from_filename(filename: str | None) -> str:
    name = (filename or "").lower()
    if name.endswith(".pdf"):
        return "pdf"
    return "image"


def _create_signed_storage_url(storage_path: str, expires_in: int = 3600) -> str:
    if not supabase_client:
        raise RuntimeError("Supabase is not configured.")
    res = supabase_client.storage.from_(SUPABASE_STORAGE_BUCKET).create_signed_url(storage_path, expires_in)
    data = getattr(res, "data", None)
    if data is None and isinstance(res, dict):
        data = res
    if isinstance(data, dict):
        url = data.get("signedUrl") or data.get("signedURL") or data.get("signed_url")
        if url:
            return url
    raise RuntimeError("Could not create signed storage URL.")


# Cache signed URLs per storage path. PDF.js issues many byte-range requests per document,
# and re-signing (a Supabase network round-trip) on every range request dominates latency.
# Signed URLs are valid for an hour, so reuse one until shortly before it expires.
_SIGNED_URL_CACHE: dict[str, tuple[str, float]] = {}
_SIGNED_URL_CACHE_TTL = 3000  # seconds; refresh well before the 3600s signed-URL expiry


def _cached_signed_storage_url(storage_path: str, expires_in: int = 3600) -> str:
    now = time.monotonic()
    hit = _SIGNED_URL_CACHE.get(storage_path)
    if hit and hit[1] > now:
        return hit[0]
    url = _create_signed_storage_url(storage_path, expires_in)
    _SIGNED_URL_CACHE[storage_path] = (url, now + min(_SIGNED_URL_CACHE_TTL, max(expires_in - 60, 0)))
    return url


@app.route("/api/files/<uuid:file_id>/original-url", methods=["GET"])
def api_file_original_url(file_id):
    """Return a short-lived signed URL for direct PDF.js / image viewing from Supabase Storage."""
    token = _bearer_token_from_request()
    if not token:
        return jsonify({"error": "Missing Authorization bearer token."}), 401
    user_id = supabase_access_token_to_user_id(token)
    if not user_id:
        return jsonify({"error": "Invalid or expired token."}), 401
    row = _files_row_for_user(str(file_id), user_id)
    if not row:
        return jsonify({"error": "File not found."}), 404
    path = row.get("original_file_path")
    if not path:
        return jsonify({"error": "No storage path on record."}), 400
    expires_in = 3600
    try:
        signed_url = _create_signed_storage_url(path, expires_in)
    except Exception as e:
        print(f"[api/files/{file_id}/original-url] signed URL failed: {e}", file=sys.stderr, flush=True)
        return jsonify({"error": f"Could not create signed URL: {e!s}"}), 502
    return jsonify(
        {
            "url": signed_url,
            "expires_in": expires_in,
            "kind": _kind_from_filename(row.get("file_name")),
            "file_name": row.get("file_name"),
        }
    )


_RANGE_PROXY_CHUNK = 256 * 1024
_RANGE_PROXY_PASSTHROUGH_HEADERS = (
    "Content-Type",
    "Content-Length",
    "Content-Range",
    "Accept-Ranges",
    "ETag",
    "Last-Modified",
    "Cache-Control",
)

# Reuse TLS connections to Supabase Storage across range requests (PDF.js issues many).
_RANGE_PROXY_SESSION = requests.Session()
_RANGE_PROXY_ADAPTER = HTTPAdapter(pool_connections=4, pool_maxsize=32, max_retries=0)
_RANGE_PROXY_SESSION.mount("https://", _RANGE_PROXY_ADAPTER)
_RANGE_PROXY_SESSION.mount("http://", _RANGE_PROXY_ADAPTER)


@app.route("/api/files/<uuid:file_id>/original-stream", methods=["GET"])
def api_file_original_stream(file_id):
    """Same-origin streaming proxy that preserves HTTP Range semantics for PDF.js.

    Supabase signed URLs are cross-origin and do not send Access-Control-Expose-Headers,
    so the browser hides Content-Length / Accept-Ranges / Content-Range from JS and PDF.js
    cannot detect range support. Serving the bytes from our own origin makes every header
    readable and lets PDF.js issue 206 range requests. The client Range header is forwarded
    upstream and the response is streamed in chunks (never buffered whole in memory).
    """
    token = _bearer_token_from_request()
    if not token:
        return jsonify({"error": "Missing Authorization bearer token."}), 401
    user_id = supabase_access_token_to_user_id(token)
    if not user_id:
        return jsonify({"error": "Invalid or expired token."}), 401
    row = _files_row_for_user(str(file_id), user_id)
    if not row:
        return jsonify({"error": "File not found."}), 404
    path = row.get("original_file_path")
    if not path:
        return jsonify({"error": "No storage path on record."}), 400

    try:
        signed_url = _cached_signed_storage_url(path, 3600)
    except Exception as e:
        print(f"[api/files/{file_id}/original-stream] signed URL failed: {e}", file=sys.stderr, flush=True)
        return jsonify({"error": f"Could not create signed URL: {e!s}"}), 502

    upstream_headers = {}
    for h in ("Range", "If-Range", "If-None-Match", "If-Modified-Since"):
        val = request.headers.get(h)
        if val:
            upstream_headers[h] = val

    try:
        upstream = _RANGE_PROXY_SESSION.get(
            signed_url,
            headers=upstream_headers,
            stream=True,
            timeout=120,
        )
    except requests.RequestException as e:
        print(f"[api/files/{file_id}/original-stream] proxy failed: {e}", file=sys.stderr, flush=True)
        return jsonify({"error": f"Storage proxy failed: {e!s}"}), 502

    if upstream.status_code in (304, 416):
        resp = Response(status=upstream.status_code)
        cr = upstream.headers.get("Content-Range")
        if cr:
            resp.headers["Content-Range"] = cr
        resp.headers["Accept-Ranges"] = "bytes"
        upstream.close()
        return resp

    if upstream.status_code >= 400:
        upstream.close()
        return jsonify(
            {"error": f"Upstream storage error: {upstream.status_code} {upstream.reason}"}
        ), 502

    status_code = upstream.status_code

    def generate():
        try:
            for chunk in upstream.iter_content(chunk_size=_RANGE_PROXY_CHUNK):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    resp = Response(generate(), status=status_code)
    for h in _RANGE_PROXY_PASSTHROUGH_HEADERS:
        val = upstream.headers.get(h)
        if val is not None:
            resp.headers[h] = val
    if "Content-Type" not in resp.headers:
        resp.headers["Content-Type"] = _mimetype_for_stored_original(row.get("file_name"), b"")
    resp.headers["Accept-Ranges"] = "bytes"
    if "Cache-Control" not in resp.headers:
        resp.headers["Cache-Control"] = "private, max-age=0"
    return resp


@app.route("/api/files/<uuid:file_id>/original", methods=["GET"])
def api_file_original(file_id):
    """Stream the stored file bytes (PDF or image) for client-side viewing (iframe / img)."""
    token = _bearer_token_from_request()
    if not token:
        return jsonify({"error": "Missing Authorization bearer token."}), 401
    user_id = supabase_access_token_to_user_id(token)
    if not user_id:
        return jsonify({"error": "Invalid or expired token."}), 401
    row = _files_row_for_user(str(file_id), user_id)
    if not row:
        return jsonify({"error": "File not found."}), 404
    path = row.get("original_file_path")
    if not path:
        return jsonify({"error": "No storage path on record."}), 400
    try:
        raw = download_from_gbucket(path)
    except Exception as e:
        return jsonify({"error": f"Storage download failed: {e!s}"}), 502
    mime = _mimetype_for_stored_original(row.get("file_name"), raw)
    dl_name = row.get("file_name") or None
    return send_file(
        io.BytesIO(raw),
        mimetype=mime,
        as_attachment=False,
        download_name=dl_name,
        max_age=0,
    )


@app.route("/api/files/<uuid:file_id>/pdf/<int:page_num>", methods=["GET"])
def api_file_pdf_page(file_id, page_num):
    token = _bearer_token_from_request()
    if not token:
        return jsonify({"error": "Missing Authorization bearer token."}), 401
    user_id = supabase_access_token_to_user_id(token)
    if not user_id:
        return jsonify({"error": "Invalid or expired token."}), 401
    row = _files_row_for_user(str(file_id), user_id)
    if not row:
        return jsonify({"error": "File not found."}), 404
    path = row.get("original_file_path")
    if not path:
        return jsonify({"error": "No storage path on record."}), 400
    try:
        raw = download_from_gbucket(path)
    except Exception as e:
        return jsonify({"error": f"Storage download failed: {e!s}"}), 502
    try:
        kind, total = _document_kind_and_page_count(raw)
        if page_num < 1 or page_num > total:
            return jsonify({"error": "Page number out of range"}), 400
        png = _render_stored_document_page_png(raw, kind, page_num)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        print(f"[api/files] render page failed: {e}", file=sys.stderr, flush=True)
        return jsonify({"error": str(e)}), 500
    return send_file(io.BytesIO(png), mimetype="image/png")


def upload_pdf():
    """Run OCR in memory; return a ZIP with JSON + compiled full text (nothing written to disk)."""
    import sys

    try:
        print("[Upload PDF] Starting upload_pdf (memory-only, no server persistence)", file=sys.stderr, flush=True)

        if "file" not in request.files:
            return jsonify({"error": "No file provided"}), 400

        pdf_file = request.files["file"]
        if pdf_file.filename == "":
            return jsonify({"error": "No file selected"}), 400

        original_filename = pdf_file.filename or "document.pdf"
        print(f"[Upload PDF] Reading file: {original_filename}", file=sys.stderr, flush=True)
        pdf_bytes = pdf_file.read()
        if not pdf_bytes:
            return jsonify({"error": "Empty file"}), 400

        def log_progress(page_num, total_pages, message):
            print(f"[OCR Progress] {message}", file=sys.stderr, flush=True)

        result = extract_text_with_locations(pdf_bytes, progress_callback=log_progress, save_callback=None)

        base = os.path.splitext(original_filename)[0] or "document"
        stem = re.sub(r"[^\w\-]", "_", base).strip("_")[:120] or "document"
        json_member = f"{stem}.json"
        txt_member = f"{stem}_FULL_TEXT.txt"
        compiled_txt = ocr_result_compiled_full_text(result)
        zip_buf = io.BytesIO()
        with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(json_member, json.dumps(result, ensure_ascii=False, indent=2))
            zf.writestr(txt_member, compiled_txt)
        zip_buf.seek(0)
        zip_name = f"{stem}_ocr_export.zip"
        return send_file(
            zip_buf,
            as_attachment=True,
            download_name=zip_name,
            mimetype="application/zip",
        )
    except VisionConfigurationError as e:
        return jsonify_vision_configuration_error(e)
    except EmptyPagesError as e:
        import traceback

        print(f"[Upload PDF Error] {str(e)}", file=sys.stderr, flush=True)
        print(traceback.format_exc(), file=sys.stderr)
        return jsonify(
            {
                "error": str(e),
                "error_type": "empty_pages",
                "start_page": e.start_page,
                "end_page": e.end_page,
            }
        ), 500
    except Exception as e:
        error_str = str(e)
        if (
            "OCR API error" in error_str
            or "Google Vision API" in error_str
            or "quota" in error_str.lower()
            or "billing" in error_str.lower()
        ):
            import traceback

            print(f"[Upload PDF Error] {error_str}", file=sys.stderr, flush=True)
            print(traceback.format_exc(), file=sys.stderr)
            return jsonify({"error": error_str, "error_type": "api_error"}), 500
        import traceback

        print(f"[Upload PDF Error] {str(e)}", file=sys.stderr, flush=True)
        print(traceback.format_exc(), file=sys.stderr)
        return jsonify({"error": str(e)}), 500


@app.route("/pdf-json", methods=["POST"])
def get_pdf_json():
    """Extract text with location info from PDF; OCR runs in memory only (no OCR results written to disk)."""
    try:
        if "file" not in request.files:
            return jsonify({"error": "No file provided"}), 400

        pdf_file = request.files["file"]
        if pdf_file.filename == "":
            return jsonify({"error": "No file selected"}), 400

        import sys

        print(f"[PDF-JSON] Reading file: {pdf_file.filename}", file=sys.stderr, flush=True)
        pdf_bytes = pdf_file.read()
        print(f"[PDF-JSON] Read {len(pdf_bytes)} bytes", file=sys.stderr, flush=True)

        if not pdf_bytes:
            return jsonify({"error": "Empty file"}), 400

        def log_progress(page_num, total_pages, message):
            print(f"[OCR Progress] {message}", file=sys.stderr, flush=True)

        result = extract_text_with_locations(pdf_bytes, progress_callback=log_progress, save_callback=None)
        page_count = len(result)
        print(
            f"[PDF-JSON] OCR complete, returning {page_count} pages (no server-side storage)",
            file=sys.stderr,
            flush=True,
        )
        return jsonify(result)
    except VisionConfigurationError as e:
        return jsonify_vision_configuration_error(e)
    except EmptyPagesError as e:
        import traceback

        print(f"[PDF-JSON Error] {str(e)}", file=sys.stderr, flush=True)
        print(traceback.format_exc(), file=sys.stderr)
        return jsonify(
            {
                "error": str(e),
                "error_type": "empty_pages",
                "start_page": e.start_page,
                "end_page": e.end_page,
            }
        ), 500
    except Exception as e:
        error_str = str(e)
        if (
            "OCR API error" in error_str
            or "Google Vision API" in error_str
            or "quota" in error_str.lower()
            or "billing" in error_str.lower()
        ):
            import traceback

            print(f"[PDF-JSON Error] {error_str}", file=sys.stderr, flush=True)
            print(traceback.format_exc(), file=sys.stderr)
            return jsonify({"error": error_str, "error_type": "api_error"}), 500
        import traceback

        error_msg = f"[PDF-JSON Error] {str(e)}"
        print(error_msg, file=sys.stderr, flush=True)
        print(traceback.format_exc(), file=sys.stderr)
        return jsonify({"error": str(e)}), 500


@app.route("/api/bundles")
def list_bundles():
    """List bundles previously saved under uploads/bundles (legacy folders only; new OCR is not stored on the server)."""
    bundles = []
    if os.path.exists(BUNDLES_DIR):
        for bundle_name in sorted(os.listdir(BUNDLES_DIR), reverse=True):
            bundle_path = os.path.join(BUNDLES_DIR, bundle_name)
            if os.path.isdir(bundle_path):
                files = os.listdir(bundle_path)
                pdf_file = next((f for f in files if f.endswith('.pdf')), None)
                json_file = next((f for f in files if f.endswith('.json')), None)
                
                if pdf_file and json_file:
                    bundles.append({
                        "id": bundle_name,
                        "name": bundle_name,
                        "pdf_file": pdf_file,
                        "json_file": json_file,
                        "created": os.path.getctime(bundle_path)
                    })
    
    return jsonify(bundles)


@app.route("/api/bundle/<bundle_id>/json")
def get_bundle_json(bundle_id):
    """Get editable JSON data for a specific bundle."""
    bundle_path = _resolve_bundle_dir(bundle_id)
    if not bundle_path:
        return jsonify({"error": "Bundle not found"}), 404
    
    # Always return editable.json
    editable_json_path = os.path.join(bundle_path, 'editable.json')
    if not os.path.exists(editable_json_path):
        # Fallback to original.json if editable doesn't exist (for old bundles)
        original_json_path = os.path.join(bundle_path, 'original.json')
        if os.path.exists(original_json_path):
            editable_json_path = original_json_path
        else:
            return jsonify({"error": "JSON file not found"}), 404
    
    with open(editable_json_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    return jsonify(data)


@app.route("/api/bundle/<bundle_id>/save", methods=["POST"])
def save_bundle_json(bundle_id):
    """Save edited JSON data for a specific bundle."""
    bundle_path = _resolve_bundle_dir(bundle_id)
    if not bundle_path:
        return jsonify({"error": "Bundle not found"}), 404
    
    try:
        edited_data = request.json
        if not edited_data:
            return jsonify({"error": "No data provided"}), 400
        
        # Load original JSON to preserve structure
        original_json_path = os.path.join(bundle_path, 'original.json')
        if not os.path.exists(original_json_path):
            return jsonify({"error": "Original JSON not found"}), 404
        
        with open(original_json_path, 'r', encoding='utf-8') as f:
            original_data = json.load(f)
        
        # Merge edited text back into structure while preserving coordinates
        updated_data = merge_edited_text(original_data, edited_data)
        
        # Save updated editable JSON
        editable_json_path = os.path.join(bundle_path, 'editable.json')
        with open(editable_json_path, 'w', encoding='utf-8') as f:
            json.dump(updated_data, f, ensure_ascii=False, indent=2)
        
        return jsonify({"success": True, "message": "Changes saved successfully"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def merge_edited_text(original_data, edited_data):
    """
    Merge edited text back into the original structure, preserving bounding boxes.
    Maps edited text to words based on order and position.
    """
    updated_data = {}
    
    for page_key in original_data:
        if page_key not in edited_data:
            updated_data[page_key] = original_data[page_key]
            continue
        
        original_page = original_data[page_key]
        edited_page = edited_data[page_key]
        updated_page = {
            "text": edited_page.get("text", original_page.get("text", "")),
            "full_text": edited_page.get("full_text", original_page.get("full_text", "")),
            "blocks": [],
            "paragraphs": [],
            "words": []
        }
        
        # Get all words from original in order
        original_words = original_page.get("words", [])
        original_text = original_page.get("full_text", original_page.get("text", ""))
        edited_text = edited_page.get("full_text", edited_page.get("text", ""))
        
        # If text hasn't changed, keep original structure
        if original_text == edited_text:
            updated_page = original_page.copy()
        else:
            # Map edited text to words
            updated_words = map_text_to_words(original_words, original_text, edited_text)
            updated_page["words"] = updated_words
            
            # Rebuild paragraphs and blocks
            updated_paragraphs = []
            updated_blocks = []
            
            # Process blocks and paragraphs
            for block in original_page.get("blocks", []):
                updated_block = {
                    "bounding_box": block.get("bounding_box", {}),
                    "paragraphs": []
                }
                
                for para in block.get("paragraphs", []):
                    para_text = para.get("text", "")
                    para_words = para.get("words", [])
                    
                    # Find corresponding words in updated_words
                    para_start_idx = find_word_index_in_list(para_words[0] if para_words else None, original_words)
                    if para_start_idx >= 0:
                        para_word_count = len(para_words)
                        updated_para_words = updated_words[para_start_idx:para_start_idx + para_word_count]
                        
                        # Reconstruct paragraph text from updated words
                        updated_para_text = " ".join([w.get("text", "") for w in updated_para_words])
                        
                        updated_para = {
                            "text": updated_para_text,
                            "bounding_box": para.get("bounding_box", {}),
                            "words": updated_para_words
                        }
                        updated_block["paragraphs"].append(updated_para)
                        updated_paragraphs.append(updated_para)
                    else:
                        # Keep original if can't map
                        updated_block["paragraphs"].append(para)
                        updated_paragraphs.append(para)
                
                updated_blocks.append(updated_block)
            
            updated_page["paragraphs"] = updated_paragraphs
            updated_page["blocks"] = updated_blocks
        
        updated_data[page_key] = updated_page
    
    return updated_data


def map_text_to_words(original_words, original_text, edited_text):
    """
    Map edited text back to word structure, preserving bounding boxes.
    Uses word order to match edited text to original words.
    Handles added/removed words by distributing coordinates proportionally.
    Newlines are preserved in full_text but don't affect word coordinate mapping.
    """
    if not original_words:
        # If no original words, create new word entries
        edited_words_list = edited_text.split()
        return [{
            "text": word,
            "bounding_box": {"vertices": []},
            "confidence": None
        } for word in edited_words_list]
    
    # For coordinate mapping, normalize whitespace (newlines become spaces)
    # This ensures word order matching works correctly
    original_text_normalized = ' '.join(original_text.split())
    edited_text_normalized = ' '.join(edited_text.split())
    
    # Split normalized texts into words for coordinate mapping
    original_words_list = original_text_normalized.split()
    edited_words_list = edited_text_normalized.split()
    
    updated_words = []
    original_idx = 0
    
    for edited_word in edited_words_list:
        if original_idx < len(original_words):
            # Use original word structure but update text
            updated_word = original_words[original_idx].copy()
            updated_word["text"] = edited_word
            updated_words.append(updated_word)
            original_idx += 1
        else:
            # New word added - use last word's bounding box
            if original_words:
                last_word = original_words[-1]
                new_word = {
                    "text": edited_word,
                    "bounding_box": last_word.get("bounding_box", {}).copy() if isinstance(last_word.get("bounding_box"), dict) else {"vertices": []},
                    "confidence": None
                }
            else:
                new_word = {
                    "text": edited_word,
                    "bounding_box": {"vertices": []},
                    "confidence": None
                }
            updated_words.append(new_word)
    
    return updated_words


def find_word_index_in_list(word, word_list):
    """Find the index of a word in a word list by comparing text and position."""
    if not word or not word_list:
        return -1
    
    word_text = word.get("text", "")
    for i, w in enumerate(word_list):
        if w.get("text", "") == word_text:
            return i
    return -1


@app.route("/api/bundle/<bundle_id>/original")
def get_bundle_original_pdf(bundle_id):
    """Serve the bundle's PDF file for embedded viewing (browser PDF UI)."""
    bundle_path = _resolve_bundle_dir(bundle_id)
    if not bundle_path:
        return jsonify({"error": "Bundle not found"}), 404
    pdf_files = [f for f in os.listdir(bundle_path) if f.endswith(".pdf")]
    if not pdf_files:
        return jsonify({"error": "PDF file not found"}), 404
    pdf_path = os.path.join(bundle_path, pdf_files[0])
    return send_file(pdf_path, mimetype="application/pdf", as_attachment=False, max_age=0)


@app.route("/api/bundle/<bundle_id>/pdf/<int:page_num>")
def get_pdf_page(bundle_id, page_num):
    """Get a specific page of a PDF as an image."""
    bundle_path = _resolve_bundle_dir(bundle_id)
    if not bundle_path:
        return jsonify({"error": "Bundle not found"}), 404
    
    pdf_files = [f for f in os.listdir(bundle_path) if f.endswith('.pdf')]
    if not pdf_files:
        return jsonify({"error": "PDF file not found"}), 404
    
    pdf_path = os.path.join(bundle_path, pdf_files[0])
    doc = fitz.open(pdf_path)
    
    if page_num < 1 or page_num > len(doc):
        doc.close()
        return jsonify({"error": "Page number out of range"}), 400
    
    # Render page to image
    page = doc[page_num - 1]  # 0-indexed
    mat = fitz.Matrix(2, 2)  # 2x zoom for better quality
    pix = page.get_pixmap(matrix=mat)
    img_bytes = pix.tobytes("png")
    doc.close()
    
    return send_file(io.BytesIO(img_bytes), mimetype='image/png')


@app.route("/api/bundle/<bundle_id>/pdf/info")
def get_pdf_info(bundle_id):
    """Get PDF info (number of pages)."""
    bundle_path = _resolve_bundle_dir(bundle_id)
    if not bundle_path:
        return jsonify({"error": "Bundle not found"}), 404
    
    pdf_files = [f for f in os.listdir(bundle_path) if f.endswith('.pdf')]
    if not pdf_files:
        return jsonify({"error": "PDF file not found"}), 404
    
    pdf_path = os.path.join(bundle_path, pdf_files[0])
    doc = fitz.open(pdf_path)
    page_count = len(doc)
    doc.close()
    
    return jsonify({"page_count": page_count})


@app.route("/api/bundle/<bundle_id>/generate-text", methods=["POST"])
def generate_text_file(bundle_id):
    """Generate a text file from editable.json, replacing any existing text file."""
    bundle_path = _resolve_bundle_dir(bundle_id)
    if not bundle_path:
        return jsonify({"error": "Bundle not found"}), 404
    
    try:
        data = request.json
        if not data or 'text' not in data:
            return jsonify({"error": "No text data provided"}), 400
        
        text_content = data['text']
        
        # Extract book name from bundle_id (remove timestamp pattern)
        # Bundle name format: BookName_YYYYMMDD_HHMMSS
        timestamp_pattern = re.compile(r'_\d{8}_\d{6}$')
        book_name = timestamp_pattern.sub('', bundle_id)
        
        # If no timestamp pattern found, try to remove last underscore and numbers
        if book_name == bundle_id:
            book_name = re.sub(r'_\d+$', '', bundle_id)
        
        # Sanitize for filename
        book_name = re.sub(r'[^a-zA-Z0-9_-]', '_', book_name)
        book_name = re.sub(r'_+', '_', book_name).strip('_')
        
        # Generate filename
        filename = f"{book_name}_FULL_TEXT.txt"
        text_file_path = os.path.join(bundle_path, filename)
        
        # Delete old text file if it exists (check for both old and new naming)
        old_text_file = os.path.join(bundle_path, 'extracted_text.txt')
        if os.path.exists(old_text_file):
            os.remove(old_text_file)
        if os.path.exists(text_file_path):
            os.remove(text_file_path)
        
        # Write new text file
        with open(text_file_path, 'w', encoding='utf-8') as f:
            f.write(text_content)
        
        return jsonify({"success": True, "message": "Text file generated successfully", "filename": filename})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


register_admin(
    app,
    {
        "SUPERADMINS": SUPERADMINS,
        "supabase_client": supabase_client,
        "supabase_browser_config": supabase_browser_config,
        "_seo_context": _seo_context,
        "_bearer_token_from_request": _bearer_token_from_request,
        "_auth_user_from_access_token": _auth_user_from_access_token,
        "_storage_object_url": _storage_object_url,
        "_storage_http_session": _storage_http_session,
        "_storage_auth_headers": _storage_auth_headers,
        "SUPABASE_STORAGE_BUCKET": SUPABASE_STORAGE_BUCKET,
    },
)


if __name__ == "__main__":
    debug = _env_truthy("FLASK_DEBUG")
    if _is_production() and debug:
        raise RuntimeError("Do not enable FLASK_DEBUG when FLASK_ENV=production.")
    port = int((os.environ.get("PORT") or "5000").strip())
    host = (os.environ.get("HOST") or "127.0.0.1").strip()
    app.run(debug=debug, host=host, port=port)
