-- Optional uniqueness for Lemon Squeezy order idempotency.
-- Run in Supabase SQL editor if lemon_order_id is not already unique.

create unique index if not exists payments_lemon_order_id_uidx
    on public.payments (lemon_order_id)
    where lemon_order_id is not null;
