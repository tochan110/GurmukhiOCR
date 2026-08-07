"""Super Admin dashboard: read-only overview, user inspection, audit logging."""

from __future__ import annotations

import sys
import concurrent.futures
from datetime import datetime, timedelta, timezone
from typing import Any

from flask import Blueprint, jsonify, render_template, request

admin_bp = Blueprint("admin", __name__)

# Populated by register_admin(app, ctx) so this module stays importable for tests.
_CTX: dict[str, Any] = {}


def register_admin(app, ctx: dict[str, Any]) -> None:
    """Attach admin routes. ctx must include helpers/clients from app.py."""
    _CTX.clear()
    _CTX.update(ctx)
    app.register_blueprint(admin_bp)


def _c(key: str):
    return _CTX[key]


def _is_superadmin_email(email: str | None) -> bool:
    if not email:
        return False
    allow = _c("SUPERADMINS")
    return email.strip().lower() in {e.strip().lower() for e in allow if e}


def require_superadmin():
    """Return (admin_user_dict, None) or (None, (response, status))."""
    token = _c("_bearer_token_from_request")()
    if not token:
        return None, (jsonify({"error": "Missing Authorization bearer token."}), 401)
    user = _c("_auth_user_from_access_token")(token)
    if not user or not user.get("id"):
        return None, (jsonify({"error": "Invalid or expired token."}), 401)
    email = (user.get("email") or "").strip()
    if not _is_superadmin_email(email):
        return None, (jsonify({"error": "Forbidden.", "error_type": "not_superadmin"}), 403)
    user["email"] = email
    return user, None


def write_audit(admin_email: str, action: str, target_user_id: str | None = None) -> None:
    client = _c("supabase_client")
    if not client:
        return
    try:
        row = {
            "admin_email": admin_email,
            "action": action,
            "target_user_id": str(target_user_id) if target_user_id else None,
        }
        client.table("admin_audit_log").insert(row).execute()
    except Exception as e:
        print(f"[admin] audit log failed action={action}: {e}", file=sys.stderr, flush=True)


def _parse_ts(val) -> datetime | None:
    if val is None:
        return None
    if isinstance(val, datetime):
        dt = val
    else:
        s = str(val).strip()
        if not s:
            return None
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _fmt_bytes(n: int | None) -> str:
    if n is None:
        return "—"
    n = int(n)
    if n < 1024:
        return f"{n} B"
    if n < 1024**2:
        return f"{n / 1024:.1f} KB"
    if n < 1024**3:
        return f"{n / (1024**2):.1f} MB"
    return f"{n / (1024**3):.2f} GB"


def _storage_object_size(path: str | None) -> int | None:
    path = (path or "").strip()
    if not path or path.startswith("/") or ".." in path:
        return None
    try:
        url = _c("_storage_object_url")(_c("SUPABASE_STORAGE_BUCKET"), path)
        resp = _c("_storage_http_session")().head(
            url,
            headers=_c("_storage_auth_headers")(),
            timeout=20,
        )
        if resp.status_code >= 400:
            return None
        cl = resp.headers.get("Content-Length")
        return int(cl) if cl is not None else None
    except Exception:
        return None


def _sum_storage_bytes(paths: list[str], *, max_objects: int = 250) -> int:
    uniq = []
    seen = set()
    for p in paths:
        p = (p or "").strip()
        if not p or p in seen:
            continue
        seen.add(p)
        uniq.append(p)
        if len(uniq) >= max_objects:
            break
    total = 0
    if not uniq:
        return 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        for size in pool.map(_storage_object_size, uniq):
            if size:
                total += size
    return total


def _list_auth_users() -> list[dict]:
    client = _c("supabase_client")
    if not client:
        return []
    users: list[dict] = []
    page = 1
    per_page = 200
    while page <= 50:
        try:
            res = client.auth.admin.list_users(page=page, per_page=per_page)
        except TypeError:
            # Older supabase-py signatures
            try:
                res = client.auth.admin.list_users()
            except Exception as e:
                print(f"[admin] list_users failed: {e}", file=sys.stderr, flush=True)
                break
        except Exception as e:
            print(f"[admin] list_users failed: {e}", file=sys.stderr, flush=True)
            break

        batch = []
        if hasattr(res, "users"):
            batch = list(res.users or [])
        elif isinstance(res, list):
            batch = res
        elif isinstance(res, dict):
            batch = res.get("users") or []

        if not batch:
            break

        for u in batch:
            if isinstance(u, dict):
                users.append(u)
            else:
                users.append(
                    {
                        "id": getattr(u, "id", None),
                        "email": getattr(u, "email", None),
                        "created_at": getattr(u, "created_at", None),
                        "last_sign_in_at": getattr(u, "last_sign_in_at", None),
                        "user_metadata": getattr(u, "user_metadata", None) or {},
                    }
                )
        if len(batch) < per_page:
            break
        page += 1
    return users


def _auth_user_by_id(user_id: str) -> dict | None:
    client = _c("supabase_client")
    if not client or not user_id:
        return None
    try:
        res = client.auth.admin.get_user_by_id(str(user_id))
        user = getattr(res, "user", None) or res
        if user is None:
            return None
        if isinstance(user, dict):
            return user
        return {
            "id": getattr(user, "id", None),
            "email": getattr(user, "email", None),
            "created_at": getattr(user, "created_at", None),
            "last_sign_in_at": getattr(user, "last_sign_in_at", None),
            "user_metadata": getattr(user, "user_metadata", None) or {},
        }
    except Exception as e:
        print(f"[admin] get_user_by_id failed: {e}", file=sys.stderr, flush=True)
        return None


def _profiles_by_id() -> dict[str, dict]:
    client = _c("supabase_client")
    if not client:
        return {}
    try:
        res = (
            client.table("profiles")
            .select(
                "id,username,free_pages_remaining,paid_pages_remaining,"
                "monthly_free_credit_allowance,last_reset,created_at"
            )
            .execute()
        )
        rows = getattr(res, "data", None) or []
        out = {}
        for row in rows:
            uid = str(row.get("id") or "")
            if uid:
                out[uid] = row
        return out
    except Exception as e:
        # created_at may not exist on older schemas — retry without it.
        print(f"[admin] profiles select failed ({e}); retrying minimal columns", file=sys.stderr, flush=True)
        try:
            res = (
                client.table("profiles")
                .select(
                    "id,username,free_pages_remaining,paid_pages_remaining,"
                    "monthly_free_credit_allowance,last_reset"
                )
                .execute()
            )
            rows = getattr(res, "data", None) or []
            out = {}
            for row in rows:
                uid = str(row.get("id") or "")
                if uid:
                    out[uid] = row
            return out
        except Exception as e2:
            print(f"[admin] profiles select failed: {e2}", file=sys.stderr, flush=True)
            return {}


def _display_name(auth_user: dict | None, profile: dict | None) -> str:
    meta = (auth_user or {}).get("user_metadata") or {}
    if not isinstance(meta, dict):
        meta = {}
    for key in ("full_name", "name", "display_name"):
        val = (meta.get(key) or "").strip() if isinstance(meta.get(key), str) else ""
        if val:
            return val
    username = ((profile or {}).get("username") or "").strip()
    if username:
        return username
    email = ((auth_user or {}).get("email") or "").strip()
    if email and "@" in email:
        return email.split("@", 1)[0]
    return "—"


def _plan_label(profile: dict | None, *, has_payment: bool = False) -> str:
    paid = 0
    if profile:
        try:
            paid = int(profile.get("paid_pages_remaining") or 0)
        except (TypeError, ValueError):
            paid = 0
    if paid > 0 or has_payment:
        return "Paid"
    return "Free"


def _file_kind(file_name: str | None, path: str | None = None) -> str:
    name = (file_name or path or "").lower()
    if name.endswith(".pdf"):
        return "pdf"
    if any(name.endswith(ext) for ext in (".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".gif", ".bmp")):
        return "image"
    return "other"


def _users_payload(*, search: str | None = None) -> list[dict]:
    auth_users = _list_auth_users()
    profiles = _profiles_by_id()
    client = _c("supabase_client")

    file_stats: dict[str, dict] = {}
    if client:
        try:
            res = (
                client.table("files")
                .select("id,user_id,credits_used,file_name,original_file_path,created_at,status")
                .execute()
            )
            for row in getattr(res, "data", None) or []:
                uid = str(row.get("user_id") or "")
                if not uid:
                    continue
                bucket = file_stats.setdefault(
                    uid,
                    {"files": 0, "pages": 0, "paths": [], "images": 0, "pdfs": 0},
                )
                bucket["files"] += 1
                try:
                    bucket["pages"] += max(0, int(row.get("credits_used") or 0))
                except (TypeError, ValueError):
                    pass
                path = row.get("original_file_path")
                if path:
                    bucket["paths"].append(path)
                kind = _file_kind(row.get("file_name"), path)
                if kind == "pdf":
                    bucket["pdfs"] += 1
                elif kind == "image":
                    bucket["images"] += 1
        except Exception as e:
            print(f"[admin] files aggregate failed: {e}", file=sys.stderr, flush=True)

    paid_user_ids: set[str] = set()
    if client:
        try:
            res = client.table("payments").select("user_id,status").execute()
            for row in getattr(res, "data", None) or []:
                if str(row.get("status") or "").lower() in ("paid", "complete", "completed", "succeeded"):
                    uid = str(row.get("user_id") or "")
                    if uid:
                        paid_user_ids.add(uid)
        except Exception:
            pass

    q = (search or "").strip().lower()
    rows_out: list[dict] = []
    for au in auth_users:
        uid = str(au.get("id") or "")
        if not uid:
            continue
        profile = profiles.get(uid)
        email = (au.get("email") or "").strip()
        name = _display_name(au, profile)
        username = ((profile or {}).get("username") or "").strip()
        if q:
            hay = f"{email} {username} {name}".lower()
            if q not in hay:
                continue
        stats = file_stats.get(uid) or {"files": 0, "pages": 0, "paths": [], "images": 0, "pdfs": 0}
        # Skip per-user storage HEADs on the list endpoint (too slow); detail page computes size.
        rows_out.append(
            {
                "id": uid,
                "name": name,
                "email": email or "—",
                "username": username or None,
                "plan": _plan_label(profile, has_payment=uid in paid_user_ids),
                "files": int(stats["files"]),
                "pages": int(stats["pages"]),
                "images": int(stats.get("images") or 0),
                "pdfs": int(stats.get("pdfs") or 0),
                "storage_bytes": None,
                "storage_label": "—",
                "last_login": au.get("last_sign_in_at"),
                "created_at": au.get("created_at") or (profile or {}).get("created_at"),
                "paid_pages_remaining": int((profile or {}).get("paid_pages_remaining") or 0)
                if profile
                else 0,
                "free_pages_remaining": int((profile or {}).get("free_pages_remaining") or 0)
                if profile
                else 0,
            }
        )

    rows_out.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
    return rows_out


def _stats_payload() -> dict:
    client = _c("supabase_client")
    auth_users = _list_auth_users()
    users_count = len(auth_users)
    now = datetime.now(timezone.utc)
    cutoff_30 = now - timedelta(days=30)
    cutoff_7 = now - timedelta(days=7)
    active = 0
    new_users_7d = 0
    for au in auth_users:
        ts_login = _parse_ts(au.get("last_sign_in_at"))
        if ts_login and ts_login >= cutoff_30:
            active += 1
        ts_created = _parse_ts(au.get("created_at"))
        if ts_created and ts_created >= cutoff_7:
            new_users_7d += 1

    files_count = 0
    pages_total = 0
    new_files_7d = 0
    paths: list[str] = []
    if client:
        try:
            res = client.table("files").select("id,credits_used,original_file_path,created_at").execute()
            rows = getattr(res, "data", None) or []
            files_count = len(rows)
            for row in rows:
                try:
                    pages_total += max(0, int(row.get("credits_used") or 0))
                except (TypeError, ValueError):
                    pass
                p = row.get("original_file_path")
                if p:
                    paths.append(p)
                created = _parse_ts(row.get("created_at"))
                if created and created >= cutoff_7:
                    new_files_7d += 1
        except Exception as e:
            print(f"[admin] stats files query failed: {e}", file=sys.stderr, flush=True)

    storage = _sum_storage_bytes(paths, max_objects=400)
    return {
        "users": users_count,
        "files": files_count,
        "pages": pages_total,
        "storage": storage,
        "storage_label": _fmt_bytes(storage),
        "active_users": active,
        "new_users_7d": new_users_7d,
        "new_files_7d": new_files_7d,
    }


def _user_detail(user_id: str) -> dict | None:
    auth_user = _auth_user_by_id(user_id)
    if not auth_user:
        return None
    profiles = _profiles_by_id()
    profile = profiles.get(str(user_id))
    client = _c("supabase_client")

    has_payment = False
    recent_transactions: list[dict] = []
    if client:
        try:
            res = (
                client.table("payments")
                .select(
                    "id,status,credits_granted,amount_paid_cents,currency,"
                    "stripe_price_id,stripe_checkout_session_id,created_at"
                )
                .eq("user_id", str(user_id))
                .order("created_at", desc=True)
                .limit(50)
                .execute()
            )
            for row in getattr(res, "data", None) or []:
                status = str(row.get("status") or "").lower()
                if status in ("paid", "complete", "completed", "succeeded"):
                    has_payment = True
                cents = row.get("amount_paid_cents")
                try:
                    cents_i = int(cents) if cents is not None else None
                except (TypeError, ValueError):
                    cents_i = None
                amount_label = "—"
                if cents_i is not None:
                    cur = (row.get("currency") or "usd").upper()
                    amount_label = f"${cents_i / 100:.2f} {cur}"
                try:
                    credits_g = int(row.get("credits_granted") or 0)
                except (TypeError, ValueError):
                    credits_g = 0
                recent_transactions.append(
                    {
                        "id": row.get("id"),
                        "status": row.get("status") or "—",
                        "credits_granted": credits_g,
                        "amount_paid_cents": cents_i,
                        "amount_label": amount_label,
                        "currency": row.get("currency") or "usd",
                        "stripe_price_id": row.get("stripe_price_id"),
                        "created_at": row.get("created_at"),
                    }
                )
        except Exception as e:
            print(f"[admin] user payments failed: {e}", file=sys.stderr, flush=True)
            # Retry without created_at / amount columns if schema differs.
            try:
                res = (
                    client.table("payments")
                    .select("id,status,credits_granted,stripe_price_id")
                    .eq("user_id", str(user_id))
                    .limit(50)
                    .execute()
                )
                for row in getattr(res, "data", None) or []:
                    status = str(row.get("status") or "").lower()
                    if status in ("paid", "complete", "completed", "succeeded"):
                        has_payment = True
                    try:
                        credits_g = int(row.get("credits_granted") or 0)
                    except (TypeError, ValueError):
                        credits_g = 0
                    recent_transactions.append(
                        {
                            "id": row.get("id"),
                            "status": row.get("status") or "—",
                            "credits_granted": credits_g,
                            "amount_paid_cents": None,
                            "amount_label": "—",
                            "currency": "usd",
                            "stripe_price_id": row.get("stripe_price_id"),
                            "created_at": None,
                        }
                    )
            except Exception as e2:
                print(f"[admin] user payments retry failed: {e2}", file=sys.stderr, flush=True)

    files = []
    images = 0
    pdfs = 0
    pages = 0
    paths = []
    if client:
        try:
            res = (
                client.table("files")
                .select(
                    "id,file_name,original_file_path,credits_used,status,created_at,job_metadata"
                )
                .eq("user_id", str(user_id))
                .order("created_at", desc=True)
                .limit(100)
                .execute()
            )
            for row in getattr(res, "data", None) or []:
                try:
                    page_n = max(0, int(row.get("credits_used") or 0))
                except (TypeError, ValueError):
                    page_n = 0
                pages += page_n
                kind = _file_kind(row.get("file_name"), row.get("original_file_path"))
                if kind == "pdf":
                    pdfs += 1
                elif kind == "image":
                    images += 1
                path = row.get("original_file_path")
                if path:
                    paths.append(path)
                size = _storage_object_size(path) if path else None
                job = row.get("job_metadata")
                if isinstance(job, str):
                    try:
                        import json

                        job = json.loads(job)
                    except Exception:
                        job = None
                files.append(
                    {
                        "id": row.get("id"),
                        "filename": row.get("file_name") or "—",
                        "uploaded": row.get("created_at"),
                        "pages": page_n,
                        "size_bytes": size,
                        "size_label": _fmt_bytes(size),
                        "ocr_status": (row.get("status") or "—"),
                        "kind": kind,
                        "job_stage": (job or {}).get("stage") if isinstance(job, dict) else None,
                    }
                )
        except Exception as e:
            print(f"[admin] user files failed: {e}", file=sys.stderr, flush=True)

    storage = _sum_storage_bytes(paths, max_objects=200)
    name = _display_name(auth_user, profile)
    return {
        "profile": {
            "id": str(user_id),
            "name": name,
            "email": (auth_user.get("email") or "").strip() or "—",
            "username": ((profile or {}).get("username") or None),
            "created_at": auth_user.get("created_at") or (profile or {}).get("created_at"),
            "last_login": auth_user.get("last_sign_in_at"),
            "plan": _plan_label(profile, has_payment=has_payment),
            "free_pages_remaining": int((profile or {}).get("free_pages_remaining") or 0)
            if profile
            else 0,
            "paid_pages_remaining": int((profile or {}).get("paid_pages_remaining") or 0)
            if profile
            else 0,
        },
        "usage": {
            "files": len(files),
            "images": images,
            "pdfs": pdfs,
            "pages": pages,
            "storage_bytes": storage,
            "storage_label": _fmt_bytes(storage),
        },
        "recent_files": files[:50],
        "recent_transactions": recent_transactions[:50],
        "recent_ocr_jobs": [
            {
                "file_id": f.get("id"),
                "filename": f.get("filename"),
                "status": f.get("ocr_status"),
                "stage": f.get("job_stage"),
                "uploaded": f.get("uploaded"),
                "pages": f.get("pages"),
            }
            for f in files[:25]
        ],
    }


def _payment_status_ok(status: str | None) -> bool:
    return str(status or "").lower() in ("paid", "complete", "completed", "succeeded")


def _fmt_money_cents(cents: int | None, currency: str = "USD") -> str:
    if cents is None:
        return "—"
    cur = (currency or "USD").upper()
    return f"${int(cents) / 100:,.2f} {cur}"


def _list_payments() -> list[dict]:
    client = _c("supabase_client")
    if not client:
        return []
    try:
        res = (
            client.table("payments")
            .select(
                "id,user_id,status,credits_granted,amount_paid_cents,currency,"
                "stripe_price_id,created_at"
            )
            .order("created_at", desc=True)
            .limit(5000)
            .execute()
        )
        return list(getattr(res, "data", None) or [])
    except Exception as e:
        print(f"[admin] payments list failed: {e}", file=sys.stderr, flush=True)
        try:
            res = (
                client.table("payments")
                .select("id,user_id,status,credits_granted,amount_paid_cents,currency,stripe_price_id")
                .limit(5000)
                .execute()
            )
            return list(getattr(res, "data", None) or [])
        except Exception as e2:
            print(f"[admin] payments list retry failed: {e2}", file=sys.stderr, flush=True)
            return []


def _finance_payload() -> dict:
    payments = _list_payments()
    completed = [p for p in payments if _payment_status_ok(p.get("status"))]
    total_cents = 0
    currency = "USD"
    paying_users: set[str] = set()
    for p in completed:
        try:
            cents = int(p.get("amount_paid_cents") or 0)
        except (TypeError, ValueError):
            cents = 0
        total_cents += max(0, cents)
        if p.get("currency"):
            currency = str(p.get("currency")).upper()
        uid = str(p.get("user_id") or "")
        if uid:
            paying_users.add(uid)

    auth_users = _list_auth_users()
    users_count = len(auth_users) or 0
    arpu_cents = int(round(total_cents / users_count)) if users_count else 0
    arppu_cents = (
        int(round(total_cents / len(paying_users))) if paying_users else 0
    )

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=30)
    month_cents = 0
    month_tx = 0
    for p in completed:
        ts = _parse_ts(p.get("created_at"))
        if ts is None or ts < cutoff:
            continue
        month_tx += 1
        try:
            month_cents += max(0, int(p.get("amount_paid_cents") or 0))
        except (TypeError, ValueError):
            pass

    recent = []
    for p in completed[:100]:
        try:
            cents_i = int(p.get("amount_paid_cents")) if p.get("amount_paid_cents") is not None else None
        except (TypeError, ValueError):
            cents_i = None
        try:
            credits_g = int(p.get("credits_granted") or 0)
        except (TypeError, ValueError):
            credits_g = 0
        recent.append(
            {
                "id": p.get("id"),
                "user_id": p.get("user_id"),
                "status": p.get("status") or "—",
                "credits_granted": credits_g,
                "amount_paid_cents": cents_i,
                "amount_label": _fmt_money_cents(cents_i, p.get("currency") or currency),
                "created_at": p.get("created_at"),
                "stripe_price_id": p.get("stripe_price_id"),
            }
        )

    return {
        "total_transactions": len(completed),
        "total_amount_cents": total_cents,
        "total_amount_label": _fmt_money_cents(total_cents, currency),
        "paying_users": len(paying_users),
        "total_users": users_count,
        "avg_rev_per_user_cents": arpu_cents,
        "avg_rev_per_user_label": _fmt_money_cents(arpu_cents, currency),
        "avg_rev_per_paying_user_cents": arppu_cents,
        "avg_rev_per_paying_user_label": _fmt_money_cents(arppu_cents, currency),
        "revenue_30d_cents": month_cents,
        "revenue_30d_label": _fmt_money_cents(month_cents, currency),
        "revenue_30d_transactions": month_tx,
        "currency": currency,
        "recent_transactions": recent,
    }


@admin_bp.route("/admin", methods=["GET"])
def admin_dashboard_page():
    seo = _c("_seo_context")(
        title="Super Admin — GurmukhiOCR",
        description="Internal super admin dashboard.",
        path="/admin",
        robots="noindex, nofollow",
    )
    return render_template(
        "admin.html",
        admin_active="overview",
        **_c("supabase_browser_config"),
        **seo,
    )


@admin_bp.route("/admin/finance", methods=["GET"])
def admin_finance_page():
    seo = _c("_seo_context")(
        title="Admin Finance — GurmukhiOCR",
        description="Internal super admin finance overview.",
        path="/admin/finance",
        robots="noindex, nofollow",
    )
    return render_template(
        "admin_finance.html",
        admin_active="finance",
        **_c("supabase_browser_config"),
        **seo,
    )


@admin_bp.route("/admin/users/<user_id>", methods=["GET"])
def admin_user_page(user_id: str):
    seo = _c("_seo_context")(
        title="Admin user — GurmukhiOCR",
        description="Internal super admin user inspection.",
        path=f"/admin/users/{user_id}",
        robots="noindex, nofollow",
    )
    return render_template(
        "admin_user.html",
        user_id=user_id,
        admin_active="overview",
        **_c("supabase_browser_config"),
        **seo,
    )


@admin_bp.route("/api/admin/me", methods=["GET"])
def api_admin_me():
    admin, err = require_superadmin()
    if err:
        return err
    return jsonify({"ok": True, "email": admin.get("email"), "id": admin.get("id")})


@admin_bp.route("/api/admin/stats", methods=["GET"])
def api_admin_stats():
    admin, err = require_superadmin()
    if err:
        return err
    if not _c("supabase_client"):
        return jsonify({"error": "Supabase not configured."}), 503
    write_audit(admin["email"], "Viewed dashboard")
    return jsonify(_stats_payload())


@admin_bp.route("/api/admin/finance", methods=["GET"])
def api_admin_finance():
    admin, err = require_superadmin()
    if err:
        return err
    if not _c("supabase_client"):
        return jsonify({"error": "Supabase not configured."}), 503
    write_audit(admin["email"], "Viewed finance")
    return jsonify(_finance_payload())


@admin_bp.route("/api/admin/users", methods=["GET"])
def api_admin_users():
    admin, err = require_superadmin()
    if err:
        return err
    if not _c("supabase_client"):
        return jsonify({"error": "Supabase not configured."}), 503
    search = (request.args.get("search") or "").strip()
    users = _users_payload(search=search or None)
    return jsonify({"users": users})


@admin_bp.route("/api/admin/users/<user_id>", methods=["GET"])
def api_admin_user_detail(user_id: str):
    admin, err = require_superadmin()
    if err:
        return err
    if not _c("supabase_client"):
        return jsonify({"error": "Supabase not configured."}), 503
    detail = _user_detail(user_id)
    if not detail:
        return jsonify({"error": "User not found."}), 404
    write_audit(admin["email"], "Viewed user", target_user_id=user_id)
    return jsonify(detail)


@admin_bp.route("/api/admin/users/<user_id>/files", methods=["GET"])
def api_admin_user_files(user_id: str):
    """File list shaped for dashboard2 read-only view-as mode."""
    admin, err = require_superadmin()
    if err:
        return err
    client = _c("supabase_client")
    if not client:
        return jsonify({"error": "Supabase not configured."}), 503
    try:
        res = (
            client.table("files")
            .select("*")
            .eq("user_id", str(user_id))
            .order("created_at", desc=True)
            .execute()
        )
        rows = getattr(res, "data", None) or []
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"files": rows})


@admin_bp.route("/api/admin/impersonate/<user_id>/start", methods=["POST"])
def api_admin_impersonate_start(user_id: str):
    admin, err = require_superadmin()
    if err:
        return err
    if not _auth_user_by_id(user_id):
        return jsonify({"error": "User not found."}), 404
    write_audit(admin["email"], "Started impersonation", target_user_id=user_id)
    return jsonify(
        {
            "ok": True,
            "view_as_user_id": str(user_id),
            "dashboard_url": f"/dashboard2?admin_view={user_id}",
        }
    )


@admin_bp.route("/api/admin/impersonate/<user_id>/exit", methods=["POST"])
def api_admin_impersonate_exit(user_id: str):
    admin, err = require_superadmin()
    if err:
        return err
    write_audit(admin["email"], "Exited impersonation", target_user_id=user_id)
    return jsonify({"ok": True})
