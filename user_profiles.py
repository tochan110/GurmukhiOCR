"""Profile usernames for collaboration (email stays private)."""

from __future__ import annotations

import re
import sys
from typing import Any

from file_access import auth_user_display

USERNAME_RE = re.compile(r"^[a-z0-9_.]{3,30}$")


def normalize_username_input(raw: str) -> str:
    s = (raw or "").strip()
    if s.startswith("@"):
        s = s[1:].strip()
    return s.lower()


def validate_username_format(username: str) -> str | None:
    if not username:
        return "Username is required."
    if len(username) < 3 or len(username) > 30:
        return "Username must be 3–30 characters."
    if not USERNAME_RE.match(username):
        return "Username may only contain letters, numbers, underscores, and periods."
    return None


def get_profile_row(supabase_client, user_id: str) -> dict | None:
    if not supabase_client or not user_id:
        return None
    try:
        res = (
            supabase_client.table("profiles")
            .select("id,username")
            .eq("id", str(user_id))
            .limit(1)
            .execute()
        )
        rows = getattr(res, "data", None) or []
        return rows[0] if rows else None
    except Exception as e:
        print(f"[user_profiles] get profile failed: {e}", file=sys.stderr, flush=True)
        return None


def is_username_taken(
    supabase_client, username: str, *, exclude_user_id: str | None = None
) -> bool:
    if not supabase_client or not username:
        return False
    try:
        res = (
            supabase_client.table("profiles")
            .select("id")
            .eq("username", username)
            .limit(1)
            .execute()
        )
        rows = getattr(res, "data", None) or []
        if not rows:
            return False
        if exclude_user_id and str(rows[0].get("id") or "") == str(exclude_user_id):
            return False
        return True
    except Exception as e:
        print(f"[user_profiles] username lookup failed: {e}", file=sys.stderr, flush=True)
        return True


def lookup_user_id_by_username(supabase_client, username: str) -> str | None:
    normalized = normalize_username_input(username)
    if not normalized or not supabase_client:
        return None
    try:
        res = (
            supabase_client.table("profiles")
            .select("id")
            .eq("username", normalized)
            .limit(1)
            .execute()
        )
        rows = getattr(res, "data", None) or []
        if not rows:
            return None
        uid = rows[0].get("id")
        return str(uid) if uid else None
    except Exception as e:
        print(f"[user_profiles] resolve username failed: {e}", file=sys.stderr, flush=True)
        return None


def _base_username_from_email(email: str, user_id: str) -> str:
    local = (email or "").split("@")[0].lower()
    cleaned = re.sub(r"[^a-z0-9_.]", "", local).strip(".")
    if len(cleaned) < 3:
        fallback = re.sub(r"[^a-z0-9_.]", "", str(user_id).replace("-", ""))[:20]
        cleaned = (cleaned + fallback) if cleaned else fallback
    if len(cleaned) < 3:
        cleaned = "user"
    return cleaned[:30]


def ensure_profile_username(
    supabase_client, user_id: str, email: str | None = None
) -> str | None:
    """Set profiles.username when empty; return username or None."""
    if not supabase_client or not user_id:
        return None
    row = get_profile_row(supabase_client, user_id)
    if not row:
        return None
    existing = (row.get("username") or "").strip().lower()
    if existing:
        return existing
    if not email:
        email = auth_user_display(str(user_id)).get("email")
    base = _base_username_from_email(email or "", str(user_id))
    candidate = base
    n = 2
    while is_username_taken(supabase_client, candidate, exclude_user_id=str(user_id)):
        suffix = str(n)
        candidate = f"{base[: max(1, 30 - len(suffix))]}{suffix}"
        n += 1
        if n > 9999:
            return None
    try:
        supabase_client.table("profiles").update({"username": candidate}).eq(
            "id", str(user_id)
        ).execute()
    except Exception as e:
        print(f"[user_profiles] ensure username failed: {e}", file=sys.stderr, flush=True)
        return None
    return candidate


def update_profile_username(
    supabase_client, user_id: str, raw_username: str
) -> tuple[str | None, str | None]:
    normalized = normalize_username_input(raw_username)
    err = validate_username_format(normalized)
    if err:
        return None, err
    if not get_profile_row(supabase_client, user_id):
        return None, "No profile found for this account."
    if is_username_taken(supabase_client, normalized, exclude_user_id=str(user_id)):
        return None, "That username is already taken."
    try:
        supabase_client.table("profiles").update({"username": normalized}).eq(
            "id", str(user_id)
        ).execute()
    except Exception as e:
        print(f"[user_profiles] update username failed: {e}", file=sys.stderr, flush=True)
        return None, str(e)
    return normalized, None


def profile_user_display(
    supabase_client, user_id: str, *, ensure_if_missing: bool = False
) -> dict[str, Any]:
    uid = str(user_id or "").strip()
    if not uid:
        return {"user_id": None, "username": None}
    row = get_profile_row(supabase_client, user_id) if supabase_client else None
    username = (row.get("username") or "").strip().lower() if row else None
    if not username and ensure_if_missing and supabase_client:
        username = ensure_profile_username(supabase_client, uid)
    return {"user_id": uid, "username": username or None}
