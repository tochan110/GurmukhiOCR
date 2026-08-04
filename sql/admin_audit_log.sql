-- Super Admin audit log (run in Supabase SQL editor).
-- Service role inserts from Flask admin endpoints only.
-- Do not weaken existing RLS on other tables.

create table if not exists public.admin_audit_log (
    id uuid primary key default gen_random_uuid(),
    admin_email text not null,
    action text not null,
    target_user_id uuid null,
    created_at timestamptz not null default now()
);

create index if not exists admin_audit_log_created_at_idx
    on public.admin_audit_log (created_at desc);

create index if not exists admin_audit_log_admin_email_idx
    on public.admin_audit_log (admin_email);

alter table public.admin_audit_log enable row level security;

-- No policies for authenticated/anon: only service role (bypasses RLS) can read/write.
comment on table public.admin_audit_log is
    'Super-admin audit trail. Written by GurmukhiOCR Flask with the service role.';
