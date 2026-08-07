-- Allow Lemon (and other non-Stripe) payment rows.
-- Stripe columns must be nullable so Lemon inserts can omit them.
-- Run in Supabase SQL editor, then Resend the Lemon webhook.

alter table public.payments
    alter column stripe_checkout_session_id drop not null;

alter table public.payments
    alter column stripe_payment_intent_id drop not null;

alter table public.payments
    alter column stripe_price_id drop not null;

-- Optional: if provider is required, ensure Lemon rows can set it.
-- alter table public.payments alter column provider set default 'stripe';

-- Idempotency for Lemon orders (safe if already created).
create unique index if not exists payments_lemon_order_id_uidx
    on public.payments (lemon_order_id)
    where lemon_order_id is not null;
