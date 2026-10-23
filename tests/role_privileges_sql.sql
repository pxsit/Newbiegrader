-- Role privilege contract for db/schema.sql. Run as a superuser against a
-- database where the schema was applied; raises on the first violation.
-- psql -v ON_ERROR_STOP=1 -d <db> -f tests/role_privileges_sql.sql
\set ON_ERROR_STOP on

do $$
declare
  t text;
  tables text[] := array['submissions','profiles','reports','users','judge_jobs',
    'judge_quota_events','contests','contest_registrations','contest_announcements',
    'problems','admins','auth_rate_buckets','web_sessions'];
  worker_tables text[] := array['submissions','judge_jobs'];
  worker_fns text[] := array[
    'public.judge_queue_claim(text,integer)',
    'public.judge_queue_heartbeat(uuid,text,integer,integer)',
    'public.judge_queue_finish(uuid,text,integer,jsonb,jsonb,text)',
    'public.judge_queue_diagnostics()'];
  f text;
  r record;
begin
  -- login roles cannot escalate or own anything
  for r in select rolname, rolcanlogin, rolsuper, rolcreaterole, rolcreatedb, rolbypassrls
           from pg_roles where rolname like 'newbiegrader\_%' loop
    assert not (r.rolsuper or r.rolcreaterole or r.rolcreatedb or r.rolbypassrls),
      r.rolname || ' has elevated attributes';
    assert r.rolcanlogin = (r.rolname <> 'newbiegrader_owner'),
      r.rolname || ' login flag wrong';
  end loop;

  for t in select tablename from pg_tables where schemaname = 'public' loop
    assert (select tableowner from pg_tables where schemaname='public' and tablename=t)
      = 'newbiegrader_owner', t || ' not owned by newbiegrader_owner';
    assert not has_table_privilege('public', 'public.' || t, 'select'),
      t || ' readable by PUBLIC';
  end loop;

  foreach t in array tables loop
    assert has_table_privilege('newbiegrader_api', 'public.' || t, 'select, insert, update, delete'),
      'api lacks DML on ' || t;
    assert not has_table_privilege('newbiegrader_api', 'public.' || t, 'truncate, references, trigger'),
      'api has DDL-ish privileges on ' || t;
    if t = any(worker_tables) then
      assert has_table_privilege('newbiegrader_worker', 'public.' || t, 'select, insert, update'),
        'worker lacks DML on ' || t;
      assert not has_table_privilege('newbiegrader_worker', 'public.' || t, 'delete, truncate'),
        'worker can delete from ' || t;
    else
      assert not has_table_privilege('newbiegrader_worker', 'public.' || t, 'select')
         and not has_table_privilege('newbiegrader_worker', 'public.' || t, 'insert')
         and not has_table_privilege('newbiegrader_worker', 'public.' || t, 'update')
         and not has_table_privilege('newbiegrader_worker', 'public.' || t, 'delete'),
        'worker can touch ' || t;
    end if;
  end loop;

  -- the worker can execute exactly the four queue functions it calls
  for r in select p.oid::regprocedure::text as sig, p.proowner::regrole::text as owner
           from pg_proc p join pg_namespace n on n.oid = p.pronamespace
           where n.nspname = 'public' loop
    assert r.owner = 'newbiegrader_owner', r.sig || ' not owned by newbiegrader_owner';
    assert not has_function_privilege('public', r.sig, 'execute'), r.sig || ' executable by PUBLIC';
    assert has_function_privilege('newbiegrader_worker', r.sig, 'execute')
      = (r.sig = any (select w::regprocedure::text from unnest(worker_fns) as w(w))),
      'worker execute privilege wrong on ' || r.sig;
  end loop;

  -- no function is security definer, so privileges never widen at call time
  assert not exists (select 1 from pg_proc p join pg_namespace n on n.oid = p.pronamespace
                     where n.nspname = 'public' and p.prosecdef), 'security definer function present';

  -- login roles cannot create objects
  assert not has_schema_privilege('newbiegrader_api', 'public', 'create'), 'api can create in public';
  assert not has_schema_privilege('newbiegrader_worker', 'public', 'create'), 'worker can create in public';
end
$$;

-- behavioural check: the worker really is refused outside its two tables
set role newbiegrader_worker;
do $$
begin
  begin
    perform 1 from public.users;
    raise exception 'worker read users';
  exception when insufficient_privilege then null;
  end;
  begin
    perform 1 from public.web_sessions;
    raise exception 'worker read web_sessions';
  exception when insufficient_privilege then null;
  end;
  begin
    perform public.judge_queue_admit('{}'::jsonb, 1, false, 0, 0);
    raise exception 'worker executed admit';
  exception when insufficient_privilege then null;
  end;
  perform 1 from public.judge_jobs;
  perform public.judge_queue_diagnostics();
end
$$;
reset role;
