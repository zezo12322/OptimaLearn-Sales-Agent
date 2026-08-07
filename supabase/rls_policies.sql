-- Row-level security for the sales agent's tables, on Supabase.
--
-- Kept out of the Alembic migration on purpose. `is_admin()` is a function that
-- exists in this specific Supabase project, and the `authenticated` role is a
-- Supabase concept — baking either into the migration would tie the schema to
-- one hosting provider. The migration stays portable; the provider-specific part
-- lives here, where it is obvious and reviewable.
--
-- Apply AFTER `alembic upgrade head`:
--     psql "$SYNC_DATABASE_URL" -f supabase/rls_policies.sql
--
-- What this grants: a signed-in admin (a row in public.admins) may read and
-- write the sales tables, exactly as they already can for `bookings`. Nobody
-- else gets anything — anon and authenticated non-admins are denied by the
-- absence of any other policy, which is what RLS does once enabled.
--
-- The agent itself is unaffected: it connects as the role that owns these
-- tables, and an owner bypasses RLS (which is why these policies use ENABLE and
-- not FORCE — see the note below).

begin;

do $$
declare
  t text;
  sales_tables text[] := array[
    'sales_leads',
    'sales_lead_identities',
    'sales_conversations',
    'sales_messages',
    'sales_lead_events',
    'sales_documents',
    'sales_chunks',
    'sales_chunk_embeddings',
    'sales_outbound_messages',
    'sales_sequences',
    'sales_sequence_enrollments',
    'sales_upsell_recommendations'
  ];
begin
  foreach t in array sales_tables loop
    -- Skip anything the migration has not created yet, so a partial run is
    -- reported rather than silently leaving a table unprotected.
    if not exists (
      select 1 from pg_tables where schemaname = 'public' and tablename = t
    ) then
      raise warning 'table public.% does not exist — run alembic upgrade head first', t;
      continue;
    end if;

    -- ENABLE, deliberately not FORCE. The agent connects as the role that owns
    -- these tables, and an owner bypasses RLS unless it is FORCEd. Forcing it
    -- would subject the agent's own queries to is_admin(), which is false for a
    -- non-JWT connection — the agent would silently read and write nothing.
    execute format('alter table public.%I enable row level security', t);

    execute format('drop policy if exists %I on public.%I', t || '_admin_all', t);
    execute format(
      'create policy %I on public.%I for all to authenticated using (is_admin()) with check (is_admin())',
      t || '_admin_all', t
    );
  end loop;
end $$;

commit;

-- Verify: every sales table should report rowsecurity = true and exactly one policy.
--
--   select c.relname, c.relrowsecurity, count(p.polname) as policies
--   from pg_class c
--   left join pg_policies p
--     on p.schemaname = 'public' and p.tablename = c.relname
--   where c.relname like 'sales\_%'
--   group by 1, 2
--   order by 1;
