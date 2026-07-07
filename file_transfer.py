"""File ownership transfer with shared_files bookkeeping."""

from __future__ import annotations

import sys
from typing import Any


class TransferError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _share_row(supabase_client, file_id: str, user_id: str) -> dict | None:
    res = (
        supabase_client.table("shared_files")
        .select("id,permission")
        .eq("file_id", file_id)
        .eq("shared_with_user_id", user_id)
        .limit(1)
        .execute()
    )
    rows = getattr(res, "data", None) or []
    return rows[0] if rows else None


def _delete_share(supabase_client, file_id: str, user_id: str) -> dict | None:
    row = _share_row(supabase_client, file_id, user_id)
    if not row:
        return None
    supabase_client.table("shared_files").delete().eq("id", row["id"]).execute()
    return row


def _restore_share(supabase_client, file_id: str, user_id: str, row: dict) -> None:
    supabase_client.table("shared_files").insert(
        {
            "file_id": file_id,
            "shared_with_user_id": user_id,
            "permission": row.get("permission") or "view",
        }
    ).execute()


def _grant_edit_share(
    supabase_client, file_id: str, user_id: str, existing: dict | None
) -> None:
    if existing:
        supabase_client.table("shared_files").update({"permission": "edit"}).eq(
            "id", existing["id"]
        ).execute()
        return
    supabase_client.table("shared_files").insert(
        {
            "file_id": file_id,
            "shared_with_user_id": user_id,
            "permission": "edit",
        }
    ).execute()


def _update_owner(supabase_client, file_id: str, from_id: str, to_id: str) -> None:
    res = (
        supabase_client.table("files")
        .update({"user_id": to_id})
        .eq("id", file_id)
        .eq("user_id", from_id)
        .execute()
    )
    rows = getattr(res, "data", None) or []
    if not rows:
        raise TransferError(
            "Could not transfer ownership. The file may have changed.",
            409,
        )


def _rollback_transfer(
    supabase_client,
    *,
    file_id: str,
    previous_owner_id: str,
    new_owner_id: str,
    owner_updated: bool,
    deleted_recipient_share: dict | None,
) -> None:
    if owner_updated:
        try:
            _update_owner(supabase_client, file_id, new_owner_id, previous_owner_id)
        except Exception as rb:
            print(f"[file_transfer] rollback owner failed: {rb}", file=sys.stderr, flush=True)
    if deleted_recipient_share:
        try:
            _restore_share(supabase_client, file_id, new_owner_id, deleted_recipient_share)
        except Exception as rb:
            print(f"[file_transfer] rollback share failed: {rb}", file=sys.stderr, flush=True)


def transfer_file_ownership(
    supabase_client,
    *,
    file_id: str,
    current_owner_id: str,
    recipient_id: str,
) -> dict[str, Any]:
    """Transfer file ownership; previous owner receives edit access."""
    if not supabase_client:
        raise TransferError("Supabase is not configured.", 503)

    file_id = str(file_id)
    current_owner_id = str(current_owner_id)
    recipient_id = str(recipient_id)

    if current_owner_id == recipient_id:
        raise TransferError("You cannot transfer ownership to yourself.", 400)

    res = (
        supabase_client.table("files")
        .select("id,user_id")
        .eq("id", file_id)
        .limit(1)
        .execute()
    )
    rows = getattr(res, "data", None) or []
    if not rows:
        raise TransferError("File not found.", 404)
    if str(rows[0].get("user_id") or "") != current_owner_id:
        raise TransferError("File not found.", 404)

    recipient_share_before = _share_row(supabase_client, file_id, recipient_id)
    former_owner_share_before = _share_row(supabase_client, file_id, current_owner_id)

    deleted_recipient_share: dict | None = None
    owner_updated = False

    try:
        if recipient_share_before:
            deleted_recipient_share = _delete_share(
                supabase_client, file_id, recipient_id
            )

        _update_owner(supabase_client, file_id, current_owner_id, recipient_id)
        owner_updated = True

        _grant_edit_share(
            supabase_client, file_id, current_owner_id, former_owner_share_before
        )
    except TransferError:
        _rollback_transfer(
            supabase_client,
            file_id=file_id,
            previous_owner_id=current_owner_id,
            new_owner_id=recipient_id,
            owner_updated=owner_updated,
            deleted_recipient_share=deleted_recipient_share,
        )
        raise
    except Exception as e:
        _rollback_transfer(
            supabase_client,
            file_id=file_id,
            previous_owner_id=current_owner_id,
            new_owner_id=recipient_id,
            owner_updated=owner_updated,
            deleted_recipient_share=deleted_recipient_share,
        )
        print(f"[file_transfer] transfer failed: {e}", file=sys.stderr, flush=True)
        raise TransferError(str(e), 500) from e

    return {
        "success": True,
        "file_id": file_id,
        "new_owner_id": recipient_id,
        "previous_owner_id": current_owner_id,
    }
