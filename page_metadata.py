"""Page-level collaboration metadata (independent of OCR JSON)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from user_profiles import profile_user_display

_METADATA_COLUMNS = (
    "file_id,page_number,completed,notes,assigned_to_user_id,updated_by,updated_at"
)


def default_page_metadata(file_id: str, page_number: int) -> dict[str, Any]:
    return {
        "file_id": str(file_id),
        "page_number": page_number,
        "completed": False,
        "notes": "",
        "assigned_to_user_id": None,
        "updated_by": None,
        "updated_at": None,
    }


def _row_to_metadata(row: dict | None, file_id: str, page_number: int) -> dict[str, Any]:
    out = default_page_metadata(file_id, page_number)
    if not row:
        return out
    if row.get("completed") is not None:
        out["completed"] = bool(row.get("completed"))
    notes = row.get("notes")
    out["notes"] = "" if notes is None else str(notes)
    assignee = row.get("assigned_to_user_id")
    out["assigned_to_user_id"] = str(assignee) if assignee else None
    updater = row.get("updated_by")
    out["updated_by"] = str(updater) if updater else None
    out["updated_at"] = row.get("updated_at")
    return out


def fetch_page_metadata_row(supabase_client, file_id: str, page_number: int) -> dict | None:
    if not supabase_client:
        return None
    try:
        res = (
            supabase_client.table("page_metadata")
            .select(_METADATA_COLUMNS)
            .eq("file_id", str(file_id))
            .eq("page_number", page_number)
            .limit(1)
            .execute()
        )
        rows = getattr(res, "data", None) or []
        return rows[0] if rows else None
    except Exception:
        return None


def metadata_public_payload(
    meta: dict[str, Any],
    supabase_client=None,
    *,
    enrich_users: bool = False,
) -> dict[str, Any]:
    out = dict(meta)
    if enrich_users:
        assignee_id = out.get("assigned_to_user_id")
        if assignee_id:
            disp = profile_user_display(supabase_client, str(assignee_id), ensure_if_missing=True)
            out["assigned_to"] = {"username": disp.get("username")}
        else:
            out["assigned_to"] = None
        updater_id = out.get("updated_by")
        if updater_id:
            disp = profile_user_display(supabase_client, str(updater_id), ensure_if_missing=True)
            out["updated_by_user"] = {"username": disp.get("username")}
        else:
            out["updated_by_user"] = None
    return out


def normalize_metadata_patch(body: dict | None) -> dict[str, Any]:
    if not isinstance(body, dict):
        return {}
    out: dict[str, Any] = {}
    if "completed" in body:
        out["completed"] = bool(body["completed"])
    if "notes" in body:
        notes = body["notes"]
        out["notes"] = "" if notes is None else str(notes)
    if "assigned_to_user_id" in body:
        val = body["assigned_to_user_id"]
        out["assigned_to_user_id"] = None if val in (None, "") else str(val)
    return out


def get_page_metadata(supabase_client, file_id: str, page_number: int) -> dict[str, Any]:
    row = fetch_page_metadata_row(supabase_client, file_id, page_number)
    meta = _row_to_metadata(row, file_id, page_number)
    return metadata_public_payload(meta, supabase_client, enrich_users=True)


def upsert_page_metadata(
    supabase_client,
    file_id: str,
    page_number: int,
    user_id: str,
    patch: dict[str, Any],
) -> dict[str, Any]:
    if not supabase_client:
        raise RuntimeError("Supabase is not configured.")
    normalized = normalize_metadata_patch(patch)
    if not normalized:
        raise ValueError("No metadata fields to update.")
    existing = _row_to_metadata(fetch_page_metadata_row(supabase_client, file_id, page_number), file_id, page_number)
    merged = {
        "file_id": str(file_id),
        "page_number": page_number,
        "completed": normalized.get("completed", existing["completed"]),
        "notes": normalized.get("notes", existing["notes"]),
        "assigned_to_user_id": normalized.get("assigned_to_user_id", existing["assigned_to_user_id"]),
        "updated_by": str(user_id),
        "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    if merged["assigned_to_user_id"] is not None:
        merged["assigned_to_user_id"] = str(merged["assigned_to_user_id"])
    res = (
        supabase_client.table("page_metadata")
        .upsert(merged, on_conflict="file_id,page_number")
        .execute()
    )
    rows = getattr(res, "data", None) or []
    saved = rows[0] if rows else merged
    meta = _row_to_metadata(saved, file_id, page_number)
    return metadata_public_payload(meta, supabase_client, enrich_users=True)


def list_metadata_collaborators(supabase_client, file_id: str, owner_user_id: str) -> list[dict[str, Any]]:
    """Users who may be assigned pages: owner plus everyone the file is shared with."""
    seen: set[str] = set()
    collaborators: list[dict[str, Any]] = []

    def add_user(uid: str) -> None:
        uid = str(uid or "").strip()
        if not uid or uid in seen:
            return
        seen.add(uid)
        disp = profile_user_display(supabase_client, uid, ensure_if_missing=True)
        collaborators.append(
            {
                "user_id": uid,
                "username": disp.get("username"),
            }
        )

    add_user(owner_user_id)
    if not supabase_client:
        return collaborators
    try:
        res = (
            supabase_client.table("shared_files")
            .select("shared_with_user_id")
            .eq("file_id", str(file_id))
            .execute()
        )
        for row in getattr(res, "data", None) or []:
            add_user(row.get("shared_with_user_id"))
    except Exception:
        pass
    return collaborators
