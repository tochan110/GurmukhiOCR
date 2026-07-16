"""File ownership and user-to-user sharing access resolution."""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

FILE_SELECT = (
    "id,user_id,original_file_path,file_name,original_json,edited_json,"
    "original_json_path,editable_json_path,credits_used,status,job_metadata"
)

_VALID_PERMISSIONS = frozenset({"view", "edit"})


@dataclass(frozen=True)
class FileAccess:
    row: dict
    role: str  # owner | view | edit
    owner_user_id: str
    share_id: str | None = None

    @property
    def is_owner(self) -> bool:
        return self.role == "owner"

    @property
    def can_edit(self) -> bool:
        return self.role in ("owner", "edit")


def access_payload(access: FileAccess, *, owner_username: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "role": access.role,
        "can_edit": access.can_edit,
        "is_owner": access.is_owner,
    }
    if owner_username:
        out["owner_username"] = owner_username
    return out


def resolve_file_access(supabase_client, file_id: str, user_id: str) -> FileAccess | None:
    """Return file row + access role if user owns or has a shared_files grant."""
    if not supabase_client or not file_id or not user_id:
        return None
    try:
        res = (
            supabase_client.table("files")
            .select(FILE_SELECT)
            .eq("id", file_id)
            .limit(1)
            .execute()
        )
        rows = getattr(res, "data", None) or []
        if not rows:
            return None
        row = rows[0]
        owner_id = str(row.get("user_id") or "")
        if owner_id == str(user_id):
            return FileAccess(row=row, role="owner", owner_user_id=owner_id)
        share_res = (
            supabase_client.table("shared_files")
            .select("id,permission")
            .eq("file_id", file_id)
            .eq("shared_with_user_id", user_id)
            .limit(1)
            .execute()
        )
        share_rows = getattr(share_res, "data", None) or []
        if not share_rows:
            return None
        share = share_rows[0]
        perm = (share.get("permission") or "view").strip().lower()
        if perm not in _VALID_PERMISSIONS:
            perm = "view"
        return FileAccess(
            row=row,
            role=perm,
            owner_user_id=owner_id,
            share_id=str(share.get("id") or "") or None,
        )
    except Exception as e:
        print(f"[file_access] resolve failed: {e}", file=sys.stderr, flush=True)
        return None


def _admin_auth_headers() -> dict[str, str] | None:
    url = (os.environ.get("SUPABASE_URL") or "").strip()
    secret = (os.environ.get("SUPABASE_SECRET_KEY") or "").strip()
    if not url or not secret:
        return None
    return {
        "Authorization": f"Bearer {secret}",
        "apikey": secret,
    }


def lookup_auth_user_by_email(email: str) -> dict[str, Any] | None:
    """Find auth.users row by email (case-insensitive) via Supabase Admin API."""
    email_norm = (email or "").strip().lower()
    if not email_norm:
        return None
    headers = _admin_auth_headers()
    if not headers:
        return None
    base = (os.environ.get("SUPABASE_URL") or "").rstrip("/")
    page = 1
    per_page = 200
    while page <= 50:
        url = f"{base}/auth/v1/admin/users?page={page}&per_page={per_page}"
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as e:
            print(f"[file_access] list users failed: {e}", file=sys.stderr, flush=True)
            return None
        users = data.get("users") if isinstance(data, dict) else None
        if not isinstance(users, list):
            return None
        for user in users:
            if not isinstance(user, dict):
                continue
            if (user.get("email") or "").strip().lower() == email_norm:
                return user
        if len(users) < per_page:
            break
        page += 1
    return None


def auth_user_display(user_id: str) -> dict[str, str | None]:
    """Return email and optional display name for an auth user id."""
    headers = _admin_auth_headers()
    if not headers or not user_id:
        return {"email": None, "name": None}
    base = (os.environ.get("SUPABASE_URL") or "").rstrip("/")
    url = f"{base}/auth/v1/admin/users/{urllib.parse.quote(str(user_id), safe='')}"
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError):
        return {"email": None, "name": None}
    user = data if isinstance(data, dict) else {}
    if isinstance(user.get("user"), dict):
        user = user["user"]
    meta = user.get("user_metadata") if isinstance(user.get("user_metadata"), dict) else {}
    name = (
        meta.get("full_name")
        or meta.get("name")
        or user.get("full_name")
    )
    if name:
        name = str(name).strip() or None
    email = (user.get("email") or "").strip() or None
    return {"email": email, "name": name}
