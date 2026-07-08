from flask import Flask, render_template, request, send_file, jsonify, session, redirect, url_for, g, Response
from dotenv import load_dotenv
from google.cloud import vision
from google.oauth2 import service_account
import fitz  # PyMuPDF
from PIL import Image, UnidentifiedImageError
import io, mimetypes, tempfile, os, json, re, zipfile, sys, urllib.request, urllib.parse, math, traceback, gzip
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

load_dotenv()

supabase_url = os.environ.get("SUPABASE_URL")
supabase_secret_key = os.environ.get("SUPABASE_SECRET_KEY")
supabase_publishable_key = os.environ.get("SUPABASE_PUBLISHABLE_KEY")

supabase_browser_config = {"supabase_url": supabase_url, "supabase_publishable_key": supabase_publishable_key}

supabase_client = (
    create_client(supabase_url, supabase_secret_key) if supabase_url and supabase_secret_key else None
)

# Monthly page quota reset (profiles.last_reset + profiles.pages_remaining)
PROFILE_PAGES_MONTHLY_ALLOWANCE = 20
PROFILE_PAGES_RESET_INTERVAL_DAYS = 30
SUPABASE_STORAGE_BUCKET = "gbucket"
SUPABASE_JSON_BUCKET = (os.environ.get("SUPABASE_JSON_BUCKET") or SUPABASE_STORAGE_BUCKET).strip()

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


# Temporary render-concurrency instrumentation (remove after the spike is located).
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


@app.after_request
def _cors_process(resp):
    if request.path == "/process":
        origin = (os.environ.get("CORS_PROCESS_ORIGIN") or "*").strip()
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
        resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
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
SESSION_CREDS_ROOT = os.path.join(UPLOADS_DIR, "session_credentials")

# Session credential storage only; OCR results are not written under BUNDLES_DIR (client download only).
os.makedirs(SESSION_CREDS_ROOT, exist_ok=True)


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


def _is_service_account_key(data):
    return (
        isinstance(data, dict)
        and data.get("type") == "service_account"
        and data.get("private_key")
        and data.get("client_email")
    )


def _session_credential_path():
    sid = session.get("cred_session_id")
    if not sid:
        return None
    return os.path.join(SESSION_CREDS_ROOT, sid, "google-ocr-key.json")


def _default_shared_ocr_key_path() -> str | None:
    """Path to the server default Vision service account JSON (used when the user has not uploaded a key).

    Set in environment (e.g. ``.env``):

    - ``GURMUKHI_OCR_KEY_PATH`` — preferred; absolute path to the JSON file.
    - ``GOOGLE_APPLICATION_CREDENTIALS`` — used only if the above is unset/invalid but this points to an existing file.

    Session credentials from ``/setup-credentials`` always override when present.
    """
    for env_name in ("GURMUKHI_OCR_KEY_PATH", "GOOGLE_APPLICATION_CREDENTIALS"):
        p = (os.environ.get(env_name) or "").strip()
        if p and os.path.isfile(p):
            return p
    return None


def _ensure_cred_session_id():
    session.permanent = True
    if "cred_session_id" not in session:
        session["cred_session_id"] = str(uuid.uuid4())
        session.modified = True


def has_ocr_credentials():
    sp = _session_credential_path()
    if sp and os.path.isfile(sp):
        return True
    return _default_shared_ocr_key_path() is not None


def get_credentials_file_path():
    """Path to Vision service account JSON: session upload wins, else server default."""
    sp = _session_credential_path()
    if sp and os.path.isfile(sp):
        return sp
    return _default_shared_ocr_key_path()


def get_vision_client():
    """Build Vision client for the current request (cached on g)."""
    if hasattr(g, "vision_client"):
        return g.vision_client
    path = get_credentials_file_path()
    if not path:
        raise RuntimeError("No Google Cloud credentials configured (upload a key or set GURMUKHI_OCR_KEY_PATH).")
    creds = service_account.Credentials.from_service_account_file(path)
    g.vision_client = vision.ImageAnnotatorClient(credentials=creds)
    return g.vision_client


# Vision accepts large images but very large rasters or malformed buffers cause INVALID_ARGUMENT / "bad image data".
# See: https://cloud.google.com/vision/docs/supported-files
_VISION_MAX_PIXELS = 75_000_000
_VISION_MAX_IMAGE_BYTES = 20 * 1024 * 1024
_VISION_MAX_EDGE = 16_384

# PDF page rasterization: target 300 DPI, but cap the longest edge so huge pages (e.g. oversized
# scans embedded in the PDF) do not allocate hundreds of MB per pixmap.
_RENDER_TARGET_DPI = 300
_RENDER_MAX_EDGE = 5000


def render_pdf_page_to_image(doc, page_index):
    """Render one PDF page for OCR. Targets 300 DPI; scales down proportionally when the page
    would exceed _RENDER_MAX_EDGE px on its longest side. Returns a PIL Image; caller closes it.

    Forces RGB (no stray alpha), converts CMYK/grayscale PDF content to RGB so channel count matches PIL.
    """
    global _render_inflight
    with _render_inflight_lock:
        _render_inflight += 1
        inflight = _render_inflight
    _log_mem(f"render_start page={page_index + 1} inflight={inflight}")
    try:
        page = doc[page_index]
        scale = _RENDER_TARGET_DPI / 72
        rect = page.rect
        exp_w = rect.width * scale
        exp_h = rect.height * scale
        max_dim = max(exp_w, exp_h)
        if max_dim > _RENDER_MAX_EDGE:
            scale *= _RENDER_MAX_EDGE / max_dim
        mat = fitz.Matrix(scale, scale)
        pix = page.get_pixmap(matrix=mat, alpha=False)
        _log_mem(
            f"render_after_get_pixmap page={page_index + 1} inflight={inflight} "
            f"w={pix.width} h={pix.height} n={pix.n} px_bytes={pix.width * pix.height * pix.n}"
        )
        try:
            if pix.n != 3:
                rgb_pix = fitz.Pixmap(fitz.csRGB, pix)
                del pix
                pix = rgb_pix
                _log_mem(f"render_after_rgb_convert page={page_index + 1} inflight={inflight}")
            if pix.width < 1 or pix.height < 1:
                raise ValueError(f"Degenerate page render: {pix.width}x{pix.height}")
            img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
            _log_mem(f"render_after_frombytes page={page_index + 1} inflight={inflight}")
            return img
        finally:
            del pix
    finally:
        with _render_inflight_lock:
            _render_inflight -= 1


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

    if response.full_text_annotation:
        for page_annotation in response.full_text_annotation.pages:
            for block in page_annotation.blocks:
                block_data = {
                    "bounding_box": {
                        "vertices": [
                            {"x": v.x, "y": v.y} for v in block.bounding_box.vertices
                        ]
                        if block.bounding_box and block.bounding_box.vertices
                        else []
                    },
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
                                    "bounding_box": {
                                        "vertices": [
                                            {"x": v.x, "y": v.y} for v in word.bounding_box.vertices
                                        ]
                                        if word.bounding_box and word.bounding_box.vertices
                                        else []
                                    },
                                    "confidence": _safe_ocr_float(getattr(word, "confidence", None)),
                                }
                            )

                    para_data = {
                        "text": para_text.strip(),
                        "bounding_box": {
                            "vertices": [
                                {"x": v.x, "y": v.y} for v in paragraph.bounding_box.vertices
                            ]
                            if paragraph.bounding_box and paragraph.bounding_box.vertices
                            else []
                        },
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
    """
    Number of parallel Vision calls. Set OCR_MAX_WORKERS=1 to force one page at a time
    (lowest load on your Google quota, easier debugging). Default: up to 8, capped by page count.
    """
    if total_pages < 2:
        return 1
    raw = os.environ.get("OCR_MAX_WORKERS", "").strip()
    if raw == "1":
        return 1
    if raw.isdigit() and int(raw) >= 1:
        w = int(raw)
    else:
        w = min(8, max(2, (os.cpu_count() or 4) // 2))
    return max(1, min(w, 16, total_pages))


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
    """One worker: open PDF, render a single page, OCR, return 1-based page number and `page_data`."""
    import sys

    page_num = page_index + 1
    prof = _active_upload_profile()
    if page_index == 0:
        _log_mem("parallel_page1_start")
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    if page_index == 0:
        _log_mem("parallel_page1_after_fitz_open")
    try:
        page_image = None
        try:
            t_render = time.perf_counter()
            page_image = render_pdf_page_to_image(doc, page_index)
            if prof:
                prof.add("pdf_render", time.perf_counter() - t_render, "pdf_render_pages")
            if page_index == 0:
                _log_mem("parallel_page1_after_render")
            try:
                t_ocr = time.perf_counter()
                response = ocr_pil_with_client(vision_client, page_image)
                if prof:
                    prof.add("ocr_api", time.perf_counter() - t_ocr, "ocr_api_pages")
                if page_index == 0:
                    _log_mem("parallel_page1_after_vision_ocr")
            except VisionConfigurationError:
                raise
            except Exception as api_error:
                error_msg = f"OCR API error on page {page_num}: {str(api_error)}"
                print(f"[OCR Processing Error] {error_msg}", file=sys.stderr, flush=True)
                raise Exception(
                    f"OCR processing stopped due to API error: {str(api_error)}"
                ) from api_error
            return (page_num, _vision_response_to_page_data(response))
        finally:
            if page_image is not None:
                page_image.close()
    finally:
        doc.close()


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


def extract_text_with_locations(pdf_bytes, progress_callback=None, save_callback=None):
    """
    Extract text with location data per page. Renders at 300 DPI, sends Vision-friendly JPEGs.
    Uses a thread pool for concurrent Vision API calls (see OCR_MAX_WORKERS); sequential if workers=1.

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

    _log_mem("before_vision_client")
    vclient = get_vision_client()
    _log_mem("after_vision_client")
    workers = _effective_ocr_worker_count(total_pages)
    result: dict = {}

    if progress_callback:
        progress_callback(
            0,
            total_pages,
            f"Starting OCR: {total_pages} page(s), {workers} concurrent Vision request(s) max…",
        )

    stream = save_callback is not None
    empties: list = [False] * total_pages if stream else []

    if workers < 2:
        page_datas: list = []
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        try:
            for page_index in range(total_pages):
                page_num = page_index + 1
                page_image = None
                try:
                    t_render = time.perf_counter()
                    page_image = render_pdf_page_to_image(doc, page_index)
                    if prof:
                        prof.add("pdf_render", time.perf_counter() - t_render, "pdf_render_pages")
                    if progress_callback:
                        progress_callback(
                            page_num,
                            total_pages,
                            f"Processing page {page_num} of {total_pages}…",
                        )
                    try:
                        t_ocr = time.perf_counter()
                        response = ocr_pil_with_client(vclient, page_image)
                        if prof:
                            prof.add("ocr_api", time.perf_counter() - t_ocr, "ocr_api_pages")
                    except VisionConfigurationError:
                        raise
                    except Exception as api_error:
                        err = f"OCR API error on page {page_num}: {str(api_error)}"
                        print(f"[OCR Processing Error] {err}", file=sys.stderr, flush=True)
                        raise Exception(
                            f"OCR processing stopped due to API error: {str(api_error)}"
                        ) from api_error
                    page_data = _vision_response_to_page_data(response)
                finally:
                    if page_image is not None:
                        page_image.close()
                if stream:
                    # Stream this page to the sink and drop it immediately; only a bool
                    # per page is retained (for the consecutive-empty rule).
                    empties[page_index] = is_page_empty(page_data)
                    _invoke_save_callback(save_callback, page_num, page_data)
                    del page_data
                else:
                    page_datas.append(page_data)
        finally:
            doc.close()
        if stream:
            _raise_if_consecutive_empty(empties)
        else:
            _assemble_pages_with_empty_detection(
                page_datas, total_pages, result, progress_callback, save_callback
            )
    else:
        page_datas = [] if stream else [None] * total_pages
        completed = 0
        _log_mem("before_create_threadpool")
        with ThreadPoolExecutor(max_workers=workers) as ex:
            _log_mem("after_create_threadpool")
            _log_mem(f"before_submit_ocr_tasks workers={workers} total_pages={total_pages}")
            futures = []
            for i in range(total_pages):
                futures.append(ex.submit(_parallel_ocr_one_page, pdf_bytes, i, vclient))
                if i % 25 == 0:
                    _log_mem(f"submit_loop_i={i}")
            _log_mem("after_submit_ocr_tasks")
            for fut in as_completed(futures):
                page_num, pdata = fut.result()
                completed += 1
                if stream:
                    # Hand each finished page straight to the sink and release it so the
                    # full OCR result is never held in memory at once.
                    empties[page_num - 1] = is_page_empty(pdata)
                    _invoke_save_callback(save_callback, page_num, pdata)
                    del pdata
                else:
                    page_datas[page_num - 1] = pdata
                if progress_callback:
                    progress_callback(
                        completed,
                        total_pages,
                        f"OCR finished {completed} of {total_pages} page(s) (parallel)…",
                    )
        if stream:
            _raise_if_consecutive_empty(empties)
        else:
            _assemble_pages_with_empty_detection(
                page_datas, total_pages, result, None, save_callback
            )
    
    if progress_callback:
        progress_callback(total_pages, total_pages, "OCR processing complete!")
    
    return result


def supabase_access_token_to_user_id(access_token: str) -> str | None:
    """Resolve Supabase Auth user id from a browser JWT (Bearer)."""
    user = _auth_user_from_access_token(access_token)
    return user.get("id") if user else None


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
    if request.endpoint is None:
        return None
    if request.endpoint == "static":
        return None
    if request.path == "/process" and request.method == "OPTIONS":
        _ensure_cred_session_id()
        return None
    if request.path.startswith("/api/files/") or request.path.startswith("/api/me/"):
        _ensure_cred_session_id()
        return None
    exempt = {
        "setup_credentials",
        "api_credentials",
        "api_auth_status",
        "landing",
        "login_page",
        "signup_page",
        "dashboard2_page",
        "dashboard_legacy_redirect",
        "process_supabase_preflight",
        "process_supabase_preflight_upload",
    }
    if request.endpoint in exempt:
        _ensure_cred_session_id()
        return None
    _ensure_cred_session_id()
    if has_ocr_credentials():
        return None
    if (
        request.path.startswith("/api/")
        or request.path in ("/pdf", "/pdf-json", "/process")
        or request.is_json
    ):
        return jsonify(
            {"error": "Google OCR service account key required.", "needs_credentials": True}
        ), 401
    if request.method in ("GET", "HEAD"):
        return redirect(url_for("setup_credentials"))
    return jsonify(
        {"error": "Google OCR service account key required.", "needs_credentials": True}
    ), 401


@app.route("/setup-credentials", methods=["GET"])
def setup_credentials():
    _ensure_cred_session_id()
    return render_template("setup_credentials.html")


@app.route("/api/auth/status", methods=["GET"])
def api_auth_status():
    _ensure_cred_session_id()
    return jsonify({"has_credentials": has_ocr_credentials()})


@app.route("/api/credentials", methods=["POST", "DELETE"])
def api_credentials():
    _ensure_cred_session_id()
    if request.method == "DELETE":
        path = _session_credential_path()
        if path and os.path.isfile(path):
            try:
                os.remove(path)
            except OSError:
                pass
        session.pop("has_uploaded_credentials", None)
        session.modified = True
        return jsonify({"success": True, "message": "Session credentials removed."})

    raw = None
    if request.files.get("file"):
        raw = request.files["file"].read()
    if raw is None:
        body = request.get_json(silent=True) or {}
        if isinstance(body.get("json"), str):
            raw = body["json"].encode("utf-8")
    if not raw:
        return jsonify({"error": "Provide a JSON key file in the 'file' field, or JSON body with a 'json' string."}), 400
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return jsonify({"error": "Invalid JSON."}), 400
    if not _is_service_account_key(data):
        return jsonify(
            {"error": "Invalid Google service account key (expected type service_account with private_key and client_email)."}
        ), 400
    sess_dir = os.path.join(SESSION_CREDS_ROOT, session["cred_session_id"])
    os.makedirs(sess_dir, exist_ok=True)
    dest = os.path.join(sess_dir, "google-ocr-key.json")
    with open(dest, "w", encoding="utf-8") as out:
        json.dump(data, out, ensure_ascii=False, indent=2)
    session.modified = True
    return jsonify({"success": True, "message": "Credentials saved for this browser session."})


@app.route("/", methods=["GET"])
def landing():
    return render_template("landing.html", **supabase_browser_config)


@app.route("/login", methods=["GET"])
def login_page():
    return render_template("login.html", active_page="login", **supabase_browser_config)


@app.route("/signup", methods=["GET"])
def signup_page():
    return render_template("signup.html", active_page="signup", **supabase_browser_config)


@app.route("/dashboard", methods=["GET"])
def dashboard_legacy_redirect():
    return redirect(url_for("dashboard2_page"))


@app.route("/dashboard2", methods=["GET"])
def dashboard2_page():
    return render_template("dashboard2.html", **supabase_browser_config)


@app.route("/api/me/pages", methods=["GET"])
def api_me_pages():
    """Return profiles.pages_remaining and when the monthly quota window resets."""
    token = _bearer_token_from_request()
    if not token:
        return jsonify({"error": "Missing Authorization bearer token."}), 401
    user_id = supabase_access_token_to_user_id(token)
    if not user_id:
        return jsonify({"error": "Invalid or expired token."}), 401
    cur, err, anchor = _profile_pages_remaining_state(user_id)
    if cur is None or anchor is None:
        return jsonify({"error": err or "No profile.", "pages_remaining": None}), 404
    next_reset = anchor + timedelta(days=PROFILE_PAGES_RESET_INTERVAL_DAYS)
    next_iso = next_reset.isoformat().replace("+00:00", "Z")
    return jsonify(
        {
            "pages_remaining": cur,
            "next_reset_at": next_iso,
            "reset_interval_days": PROFILE_PAGES_RESET_INTERVAL_DAYS,
        }
    )


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
    try:
        with profile.time_step("storage_download", file_path=file_path):
            try:
                raw = download_from_gbucket(file_path)
            except Exception as e:
                print(f"[process] Storage download failed: {e}", file=sys.stderr, flush=True)
                return jsonify({"error": f"Could not download file from storage: {e!s}"}), 502

        if not raw:
            return jsonify({"error": "Downloaded file was empty."}), 400

        _log_mem("after_storage_download")

        with profile.time_step("pdf_parse_page_count", bytes=len(raw)):
            try:
                doc_kind, doc_pages = _document_kind_and_page_count(raw)
            except ValueError as e:
                return jsonify({"error": str(e)}), 400

        # Linearize PDFs ("fast web view") so the viewer can render page 1 from the first
        # ~few hundred KB instead of downloading the whole file. Client-uploaded originals
        # aren't linearized; re-upload the optimized copy in place. Best-effort.
        if doc_kind == "pdf":
            linearized = _linearize_pdf_bytes(raw)
            if linearized is not raw and linearized != raw:
                try:
                    with profile.time_step("pdf_linearize_reupload", bytes=len(linearized)):
                        _storage_upload(SUPABASE_STORAGE_BUCKET, file_path, linearized, "application/pdf")
                    raw = linearized
                except Exception as e:
                    print(f"[process] linearized re-upload failed (using original): {e}", file=sys.stderr, flush=True)
            # Release the temporary linearized buffer so two full PDF copies aren't retained
            # into the memory-heavy OCR stage (raw now holds whichever copy we keep).
            del linearized

        page_charge = doc_pages if doc_kind == "pdf" else 1

        with profile.time_step("db_read_profile_quota"):
            cur_pages, pr_err = _get_profile_pages_remaining(user_id)
        if cur_pages is None:
            return jsonify(
                {
                    "error": pr_err or "Could not load your page quota from profiles.",
                    "error_type": "profile",
                }
            ), 400
        if cur_pages < page_charge:
            return jsonify(
                {
                    "error": f"Not enough credits remaining ({cur_pages} left; this file needs {page_charge}).",
                    "error_type": "insufficient_pages",
                    "pages_remaining": cur_pages,
                    "pages_required": page_charge,
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

        def mark_file_failed(message: str) -> None:
            try:
                supabase_client.table("files").update({"status": "failed"}).eq("id", str(inserted_id)).eq("user_id", user_id).execute()
            except Exception as status_e:
                print(f"[process] failed to mark file failed ({message}): {status_e}", file=sys.stderr, flush=True)

        with profile.time_step("db_consume_credits", pages=page_charge):
            ok_q, err_q, new_remaining = _consume_profile_pages(user_id, page_charge)
        if not ok_q:
            mark_file_failed(err_q or "Could not update credit balance.")
            return jsonify(
                {
                    "error": err_q or "Could not update credit balance.",
                    "error_type": "quota_update_failed",
                    "pages_remaining": cur_pages,
                    "file_id": str(inserted_id),
                }
            ), 409

        inserted_id_str = str(inserted_id)
        ocr_result = None
        try:
            if use_inline_json:
                # Single page: keep the page data in memory so it can be stored inline on
                # the row (1 page is tiny). No streaming needed.
                with profile.time_step("ocr_pipeline", pages=doc_pages, inline_json=True):
                    ocr_result = run_ocr_on_file_bytes(
                        raw, file_name or os.path.basename(file_path), save_callback=None
                    )
            else:
                # Multi-page: stream each page to per-page storage as its OCR completes so
                # the whole OCR result is never held in memory at once. Uploads still run in
                # parallel via a dedicated executor; OCR (the slow stage) paces the backlog,
                # keeping only a handful of pages in flight regardless of document size.
                upload_ex = ThreadPoolExecutor(max_workers=_import_max_workers(doc_pages))
                upload_futures: list = []

                def _save_original_page(page_num, page_data):
                    upload_futures.append(
                        upload_ex.submit(
                            _upload_page_json, user_id, inserted_id_str, "original", page_num, page_data
                        )
                    )

                try:
                    with profile.time_step("ocr_pipeline", pages=doc_pages, inline_json=False):
                        run_ocr_on_file_bytes(
                            raw,
                            file_name or os.path.basename(file_path),
                            save_callback=_save_original_page,
                        )
                    with profile.time_step("json_upload_parallel", pages=doc_pages):
                        upload_ex.shutdown(wait=True)
                        for f in upload_futures:
                            f.result()
                finally:
                    # Ensure worker threads are always released, even on OCR/upload failure.
                    upload_ex.shutdown(wait=True)
        except VisionConfigurationError as e:
            mark_file_failed(e.user_message)
            return jsonify_vision_configuration_error(e)
        except EmptyPagesError as e:
            mark_file_failed(str(e))
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
            return jsonify(
                {
                    "error": str(e),
                    "error_type": "empty_pages",
                    "start_page": e.start_page,
                    "end_page": e.end_page,
                    "file_id": str(inserted_id),
                }
            ), 500
        except Exception as e:
            print(f"[process] OCR failed: {e}", file=sys.stderr, flush=True)
            mark_file_failed(str(e))
            return jsonify({"error": str(e), "error_type": "ocr_error", "file_id": str(inserted_id)}), 500

        # The source PDF is no longer needed; release it before serializing DB updates.
        raw = None

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
                mark_file_failed(str(e))
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
                "pages_remaining": new_remaining,
                "pages_charged": page_charge,
                "credits_used": page_charge,
                "json_storage": json_storage,
                "profile_job_id": profile_job_id,
            }
        )
    finally:
        if not profile_finished:
            profile.finish(status="incomplete")
        _set_active_upload_profile(None)


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
    cur_pages, pr_err = _get_profile_pages_remaining(user_id)
    if cur_pages is None:
        return jsonify(
            {
                "error": pr_err or "Could not load your page quota from profiles.",
                "error_type": "profile",
            }
        ), 400

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
            "pages_remaining": cur_pages,
            "error_type": None if can_process else "insufficient_pages",
            "error": None
            if can_process
            else f"Not enough credits remaining ({cur_pages} left; this file needs {credits_required}).",
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
    cur_pages, pr_err = _get_profile_pages_remaining(user_id)
    if cur_pages is None:
        return jsonify(
            {
                "error": pr_err or "Could not load your page quota from profiles.",
                "error_type": "profile",
            }
        ), 400

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
            "pages_remaining": cur_pages,
            "error_type": None if can_process else "insufficient_pages",
            "error": None
            if can_process
            else f"Not enough credits remaining ({cur_pages} left; this file needs {credits_required}).",
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


def _profile_pages_remaining_state(
    user_id: str,
) -> tuple[int | None, str | None, datetime | None]:
    """
    Read profiles.pages_remaining for auth user id.
    If last_reset is more than PROFILE_PAGES_RESET_INTERVAL_DAYS ago, sets pages_remaining
    to PROFILE_PAGES_MONTHLY_ALLOWANCE and last_reset to now (UTC).
    If last_reset is null, sets last_reset to now only (keeps existing pages_remaining).
    Returns (pages_remaining, error, last_reset_anchor_utc). Anchor is the cycle start in UTC;
    the next automatic reset is anchor + PROFILE_PAGES_RESET_INTERVAL_DAYS.
    (None, msg, None) if missing profile or error.
    """
    if not supabase_client:
        return None, "Supabase not configured", None
    try:
        res = (
            supabase_client.table("profiles")
            .select("pages_remaining, last_reset")
            .eq("id", user_id)
            .limit(1)
            .execute()
        )
        rows = getattr(res, "data", None) or []
        if not rows:
            return (
                None,
                "No profile row found for this user (create a profiles row with the same id as auth.users).",
                None,
            )
        row = rows[0]
        pr = row.get("pages_remaining")
        last_reset = _parse_profile_timestamp(row.get("last_reset"))
        now = datetime.now(timezone.utc)
        if last_reset is None:
            try:
                supabase_client.table("profiles").update({"last_reset": now.isoformat()}).eq("id", user_id).execute()
            except Exception as e:
                print(f"[profiles] set last_reset failed: {e}", file=sys.stderr, flush=True)
                return None, str(e), None
            if pr is None:
                return 0, None, now
            return int(pr), None, now
        if (now - last_reset) > timedelta(days=PROFILE_PAGES_RESET_INTERVAL_DAYS):
            try:
                supabase_client.table("profiles").update(
                    {
                        "pages_remaining": PROFILE_PAGES_MONTHLY_ALLOWANCE,
                        "last_reset": now.isoformat(),
                    }
                ).eq("id", user_id).execute()
            except Exception as e:
                print(f"[profiles] monthly quota reset failed: {e}", file=sys.stderr, flush=True)
                return None, str(e), None
            return PROFILE_PAGES_MONTHLY_ALLOWANCE, None, now
        if pr is None:
            return 0, None, last_reset
        return int(pr), None, last_reset
    except Exception as e:
        print(f"[profiles] read pages_remaining: {e}", file=sys.stderr, flush=True)
        return None, str(e), None


def _get_profile_pages_remaining(user_id: str) -> tuple[int | None, str | None]:
    pr, err, _anchor = _profile_pages_remaining_state(user_id)
    return pr, err


def _consume_profile_pages(user_id: str, delta: int) -> tuple[bool, str | None, int | None]:
    """
    Subtract delta from profiles.pages_remaining.
    Returns (success, error_message, new_remaining on success else current or None).
    """
    if delta <= 0:
        cur, err = _get_profile_pages_remaining(user_id)
        return (True, err, cur) if cur is not None else (False, err or "No profile.", None)
    cur, err = _get_profile_pages_remaining(user_id)
    if cur is None:
        return False, err or "No profile row found for this user.", None
    if cur < delta:
        return False, f"Not enough credits remaining ({cur} left, this file needs {delta}).", cur
    new_v = cur - delta
    try:
        res = (
            supabase_client.table("profiles")
            .update({"pages_remaining": new_v})
            .eq("id", user_id)
            .eq("pages_remaining", cur)
            .execute()
        )
        rows = getattr(res, "data", None) or []
        if not rows:
            latest, latest_err = _get_profile_pages_remaining(user_id)
            if latest is not None and latest < delta:
                return False, f"Not enough credits remaining ({latest} left, this file needs {delta}).", latest
            return False, latest_err or "Credit balance changed while starting this job. Try again.", latest
    except Exception as e:
        print(f"[profiles] update pages_remaining: {e}", file=sys.stderr, flush=True)
        return False, str(e), cur
    return True, None, new_v


_QPDF_BIN = shutil.which("qpdf")


def _pdf_is_linearized(raw: bytes) -> bool:
    """A linearized ("fast web view") PDF declares /Linearized in its first object."""
    return b"/Linearized" in raw[:2048]


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


if __name__ == "__main__":
    debug = _env_truthy("FLASK_DEBUG")
    if _is_production() and debug:
        raise RuntimeError("Do not enable FLASK_DEBUG when FLASK_ENV=production.")
    port = int((os.environ.get("PORT") or "5000").strip())
    host = (os.environ.get("HOST") or "127.0.0.1").strip()
    app.run(debug=debug, host=host, port=port)
