"""Lemon Squeezy payment helpers (additive; does not touch Stripe fulfillment)."""

from __future__ import annotations

import hashlib
import hmac
import json
import sys
from typing import Any, Callable


LEMON_API_BASE = "https://api.lemonsqueezy.com/v1"


def verify_lemon_signature(raw_body: bytes, signature_header: str | None, secret: str) -> bool:
    """Verify X-Signature HMAC-SHA256 hex digest of the raw webhook body."""
    if not secret or not signature_header or not raw_body:
        return False
    digest = hmac.new(
        secret.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(digest, signature_header.strip())


def create_lemon_checkout_url(
    *,
    api_key: str,
    store_id: str,
    variant_id: str,
    user_id: str,
    redirect_url: str,
    email: str | None = None,
) -> str:
    """Create a Lemon Squeezy checkout and return its hosted URL."""
    import requests

    attributes: dict[str, Any] = {
        "checkout_data": {
            "custom": {
                "user_id": str(user_id),
            }
        },
        "product_options": {
            "redirect_url": redirect_url,
        },
        "checkout_options": {
            "embed": False,
        },
    }
    if email:
        attributes["checkout_data"]["email"] = email

    payload = {
        "data": {
            "type": "checkouts",
            "attributes": attributes,
            "relationships": {
                "store": {
                    "data": {
                        "type": "stores",
                        "id": str(store_id),
                    }
                },
                "variant": {
                    "data": {
                        "type": "variants",
                        "id": str(variant_id),
                    }
                },
            },
        }
    }
    resp = requests.post(
        f"{LEMON_API_BASE}/checkouts",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/vnd.api+json",
            "Content-Type": "application/vnd.api+json",
        },
        json=payload,
        timeout=30,
    )
    if resp.status_code >= 400:
        raise RuntimeError(
            f"Lemon checkout create failed ({resp.status_code}): {resp.text[:500]}"
        )
    body = resp.json()
    url = (
        (((body.get("data") or {}).get("attributes") or {}).get("url"))
        if isinstance(body, dict)
        else None
    )
    if not url:
        raise RuntimeError("Lemon checkout response missing attributes.url")
    return str(url)


def _nested_get(data: dict, *path, default=None):
    cur: Any = data
    for key in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
    return default if cur is None else cur


def extract_lemon_order_payload(event: dict) -> dict[str, Any] | None:
    """Normalize a Lemon webhook JSON body into fulfillment fields, or None if not a paid order."""
    if not isinstance(event, dict):
        return None
    event_name = str(_nested_get(event, "meta", "event_name") or "").strip()
    # order_created fires when an order is placed; status should be paid for one-time products.
    if event_name not in ("order_created", "order_paid"):
        return None

    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    attrs = data.get("attributes") if isinstance(data.get("attributes"), dict) else {}
    status = str(attrs.get("status") or "").strip().lower()
    if status and status not in ("paid", "completed"):
        # Some events arrive before settlement; ignore non-paid.
        if event_name != "order_paid":
            return None

    order_id = str(data.get("id") or "").strip()
    if not order_id:
        return None

    first_item = attrs.get("first_order_item") if isinstance(attrs.get("first_order_item"), dict) else {}
    variant_id = str(first_item.get("variant_id") or "").strip()
    if not variant_id:
        return None

    custom = _nested_get(event, "meta", "custom_data") or {}
    if not isinstance(custom, dict):
        custom = {}
    user_id = str(custom.get("user_id") or "").strip()

    identifier = str(attrs.get("identifier") or "").strip() or None
    try:
        total_cents = int(attrs.get("total")) if attrs.get("total") is not None else None
    except (TypeError, ValueError):
        total_cents = None
    currency = str(attrs.get("currency") or "USD").strip().lower() or "usd"

    return {
        "lemon_order_id": order_id,
        "lemon_payment_id": identifier,
        "lemon_variant_id": variant_id,
        "user_id": user_id,
        "amount_paid_cents": total_cents,
        "currency": currency,
        "event_name": event_name,
        "status": status or "paid",
    }


def fulfill_lemon_order(
    *,
    supabase_client,
    order: dict[str, Any],
    variant_id_to_credits: dict[str, int],
    add_paid_credits: Callable[[str, int], tuple[bool, str | None, dict | None]],
    is_unique_violation: Callable[[BaseException], bool],
) -> tuple[bool, str, int]:
    """Insert Lemon payment + grant credits once. Returns (ok, message, http_status)."""
    if not supabase_client:
        return False, "Supabase is not configured.", 500

    lemon_order_id = str(order.get("lemon_order_id") or "").strip()
    if not lemon_order_id:
        return False, "Missing lemon_order_id.", 400

    try:
        existing = (
            supabase_client.table("payments")
            .select("id,lemon_order_id,status,credits_granted")
            .eq("lemon_order_id", lemon_order_id)
            .limit(1)
            .execute()
        )
        rows = getattr(existing, "data", None) or []
        if rows:
            print(
                f"[lemon] duplicate webhook ignored order_id={lemon_order_id}",
                file=sys.stderr,
                flush=True,
            )
            return True, "already_processed", 200
    except Exception as e:
        print(f"[lemon] payment lookup failed: {e}", file=sys.stderr, flush=True)
        return False, "Could not look up payment.", 500

    user_id = str(order.get("user_id") or "").strip()
    if not user_id:
        return False, "Order missing custom_data.user_id.", 400

    variant_id = str(order.get("lemon_variant_id") or "").strip()
    if not variant_id or variant_id not in variant_id_to_credits:
        return False, f"Unknown or missing lemon_variant_id: {variant_id!r}", 400
    credits_granted = int(variant_id_to_credits[variant_id])

    payment_row = {
        "user_id": user_id,
        "provider": "lemon",
        "lemon_order_id": lemon_order_id,
        "lemon_payment_id": order.get("lemon_payment_id"),
        "lemon_variant_id": variant_id,
        "credits_granted": credits_granted,
        "amount_paid_cents": order.get("amount_paid_cents"),
        "currency": order.get("currency") or "usd",
        "status": "completed",
    }

    try:
        supabase_client.table("payments").insert(payment_row).execute()
        print(
            f"[lemon] payment inserted order_id={lemon_order_id} "
            f"user_id={user_id} credits={credits_granted}",
            file=sys.stderr,
            flush=True,
        )
    except Exception as e:
        # Race: another webhook inserted first.
        try:
            again = (
                supabase_client.table("payments")
                .select("id")
                .eq("lemon_order_id", lemon_order_id)
                .limit(1)
                .execute()
            )
            if getattr(again, "data", None) or is_unique_violation(e):
                print(
                    f"[lemon] duplicate webhook ignored order_id={lemon_order_id}",
                    file=sys.stderr,
                    flush=True,
                )
                return True, "already_processed", 200
        except Exception:
            pass
        err_text = str(e)
        print(f"[lemon] payment insert failed: {err_text}", file=sys.stderr, flush=True)
        # Surface the DB reason so Lemon's Resend log is actionable.
        return False, f"Could not record payment: {err_text}", 500

    ok, err, _balance = add_paid_credits(user_id, credits_granted)
    if not ok:
        print(
            f"[lemon] credits grant failed order_id={lemon_order_id} "
            f"user_id={user_id} credits={credits_granted}: {err}",
            file=sys.stderr,
            flush=True,
        )
        try:
            supabase_client.table("payments").delete().eq(
                "lemon_order_id", lemon_order_id
            ).execute()
        except Exception as del_e:
            print(
                f"[lemon] compensate delete failed order_id={lemon_order_id}: {del_e}",
                file=sys.stderr,
                flush=True,
            )
        return False, err or "Could not grant credits.", 500

    print(
        f"[lemon] credits granted order_id={lemon_order_id} "
        f"user_id={user_id} credits={credits_granted}",
        file=sys.stderr,
        flush=True,
    )
    return True, "ok", 200
