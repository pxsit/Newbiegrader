-- Newbiegrader baseline schema for plain PostgreSQL 18 (no Supabase).
-- Applied by a superuser (or the database owner) against an empty database, and
-- safe to re-run. Later changes are appended as new sections; never edit above.
-- Passwords and DSNs do not belong here: roles are LOGIN without a password and
-- authenticate by peer over the Unix socket (pg_ident.conf maps OS users).

-- ---------------------------------------------------------------------------
-- Roles
--   newbiegrader_owner   NOLOGIN; owns every object, so no login role can alter
--                        schema or widen its own privileges
--   newbiegrader_api     LOGIN; the web API
--   newbiegrader_worker  LOGIN; judge workers: only submissions and judge_jobs
--                        plus the queue functions they call
-- ---------------------------------------------------------------------------
do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'newbiegrader_owner') then
    create role newbiegrader_owner nologin;
  end if;
  if not exists (select 1 from pg_roles where rolname = 'newbiegrader_api') then
    create role newbiegrader_api login;
  end if;
  if not exists (select 1 from pg_roles where rolname = 'newbiegrader_worker') then
    create role newbiegrader_worker login;
  end if;
end
$$;

revoke all on schema public from public;
grant usage on schema public to newbiegrader_api, newbiegrader_worker;
grant usage, create on schema public to newbiegrader_owner;
do $$
begin
  execute format('revoke all on database %I from public', current_database());
  execute format('grant connect on database %I to newbiegrader_api, newbiegrader_worker, newbiegrader_owner',
    current_database());
end
$$;

set role newbiegrader_owner;

-- ---------------------------------------------------------------------------
-- Tables
-- ---------------------------------------------------------------------------
create table if not exists public.submissions (
  id uuid primary key default gen_random_uuid(),
  created_at timestamptz not null default now(),
  problem text not null,
  language text not null,
  filename text,
  name text not null default '',
  user_email text not null default '',
  verdict text not null,                 -- AC / REJECTED / CE
  accepted boolean not null default false,
  passed int not null default 0,
  total int not null default 0,
  score int not null default 0,
  counts jsonb not null default '{}',
  results jsonb not null default '[]',   -- per-test verdicts + diffs
  code text not null default '',
  detail text not null default '',       -- compile error / misc
  tests_hash text not null default '',   -- test set that scored this row
  grader_version text not null default '',
  kind text not null default 'submit',
  contest_id text,
  job_id uuid
);
create index if not exists submissions_problem_time on public.submissions (problem, created_at desc);
create index if not exists submissions_user_time on public.submissions (user_email, created_at desc);
create index if not exists submissions_created on public.submissions (created_at desc);
create index if not exists submissions_contest_time on public.submissions (contest_id, created_at desc);
create index if not exists submissions_history_filter on public.submissions (user_email, problem, created_at desc);
create index if not exists submissions_leaderboard_cover on public.submissions (contest_id, user_email, problem);
create index if not exists submissions_stats_cover on public.submissions (contest_id, problem);
create index if not exists submissions_contest_created on public.submissions (contest_id, created_at desc);

create table if not exists public.profiles (
  user_email text primary key,
  display_name text not null default '',
  default_language text not null default ''
);

create table if not exists public.reports (
  id uuid primary key default gen_random_uuid(),
  created_at timestamptz not null default now(),
  problem text not null,
  kind text not null default 'other',
  message text not null default '',
  user_email text not null default '',
  name text not null default '',
  status text not null default 'open'
);
create index if not exists reports_problem_time on public.reports (problem, created_at desc);

create table if not exists public.users (
  email text primary key,
  name text not null default '',
  password_hash text not null default '',
  created_at timestamptz not null default now(),
  email_verified_at timestamptz,
  email_auth_subject uuid,
  google_subject text,
  auth_version bigint not null default 0
);
create unique index if not exists users_email_auth_subject_unique
  on public.users(email_auth_subject) where email_auth_subject is not null;
create unique index if not exists users_google_subject_unique
  on public.users(google_subject) where google_subject is not null;

create table if not exists public.judge_jobs (
  id uuid primary key default gen_random_uuid(),
  created_at timestamptz not null default now(),
  started_at timestamptz,
  finished_at timestamptz,
  status text not null default 'queued',   -- queued | running | done | error
  kind text not null default 'submit',     -- submit | rejudge
  user_email text not null default '',
  name text not null default '',
  problem text not null,
  language text not null,
  filename text,
  contest_id text,
  source text not null default '',
  target_id uuid,                          -- rejudge: submission row to overwrite
  result jsonb,
  error text not null default '',
  tests_hash text not null default '',
  grader_version text not null default '',
  reveal text not null default 'samples',  -- all | samples | none (feedback policy)
  payload jsonb not null default '{}',
  track_active boolean not null default true,
  worker_id text,
  attempt integer not null default 0,
  lease_until timestamptz,
  submission_id uuid
);
create index if not exists judge_jobs_status_time on public.judge_jobs (status, created_at);
create index if not exists judge_jobs_lease on public.judge_jobs (lease_until) where status = 'running';
create index if not exists judge_jobs_active_user on public.judge_jobs (user_email)
  where status in ('queued', 'running') and track_active;
create index if not exists judge_jobs_claim_cover on public.judge_jobs (status, kind, created_at, id);

create table if not exists public.judge_quota_events (
  user_email text not null,
  kind text not null,
  admitted_at timestamptz not null default now()
);
create index if not exists judge_quota_window on public.judge_quota_events (user_email, kind, admitted_at);

create table if not exists public.contests (
  id text primary key,
  created_at timestamptz not null default now(),
  title text not null default '',
  starts_at timestamptz not null,
  ends_at timestamptz not null,
  problems jsonb not null default '[]',
  freeze_minutes int not null default 0,
  show_samples boolean not null default true,
  created_by text not null default '',
  hide_from_tasks boolean not null default true
);

create table if not exists public.contest_registrations (
  contest_id text not null,
  user_email text not null,
  name text not null default '',
  registered_at timestamptz not null default now(),
  primary key (contest_id, user_email)
);

create table if not exists public.contest_announcements (
  id uuid not null primary key,
  contest_id text not null,
  body text not null default '',
  created_by text not null default '',
  created_at timestamptz not null default now()
);
create index if not exists contest_announcements_contest_time
  on public.contest_announcements (contest_id, created_at);

-- Problem metadata and public statements/samples. Hidden test files and PDFs
-- live on backend disk (GRADER_FILE_STORAGE=local); hidden_tests holds only the
-- manifest (name/sha256/sizes), never contents.
create table if not exists public.problems (
  slug text primary key,
  no int unique not null,
  title text not null default '',
  time_limit double precision not null default 1.0,
  memory_limit int not null default 256,
  checker text not null default 'token',
  tolerance double precision not null default 1e-6,
  difficulty text not null default '',
  tags jsonb not null default '[]',
  samples jsonb not null default '[]',
  samples_data jsonb not null default '[]',
  statement_md text not null default '',
  tests_hash text not null default '',
  tests_count int not null default 0,
  hidden_tests jsonb not null default '[]',
  hidden boolean not null default false,
  is_published boolean not null default true,
  statement_pdf_path text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists problems_no_idx on public.problems (no);
create index if not exists problems_published_idx on public.problems (is_published, hidden);

create table if not exists public.admins (
  email text primary key check (email = lower(btrim(email)) and email <> ''),
  created_at timestamptz not null default now(),
  created_by text not null default ''
);

create table if not exists public.auth_rate_buckets (
  bucket text primary key,
  window_start timestamptz not null,
  attempts integer not null check (attempts > 0),
  expires_at timestamptz not null
);
create index if not exists auth_rate_buckets_expiry on public.auth_rate_buckets(expires_at);

-- Short-lived rows. Backups may skip them; the one-time Supabase cutover carries
-- them over so signed-in users stay signed in (docs/POSTGRES_CUTOVER.md).
create table if not exists public.web_sessions (
  token_hash text primary key check (token_hash ~ '^[0-9a-f]{64}$'),
  user_email text not null references public.users(email) on delete cascade,
  auth_version bigint not null check (auth_version > 0),
  created_at timestamptz not null default clock_timestamp(),
  expires_at timestamptz not null,
  remember_until timestamptz,
  activated boolean not null,
  handoff_until timestamptz,
  provider text not null check (provider in ('google', 'password')),
  picture text not null default '' check (length(picture) <= 2048),
  revoked_at timestamptz
);
create index if not exists web_sessions_expiry on public.web_sessions(expires_at);
create index if not exists web_sessions_user on public.web_sessions(user_email);

-- ---------------------------------------------------------------------------
-- Views. security_invoker so callers need table privileges.
-- Scores earned inside a contest stay out of the all-time views.
-- ---------------------------------------------------------------------------
create or replace view public.leaderboard_scores with (security_invoker = true) as
select user_email, problem,
       max(score) as best,
       bool_or(accepted) as accepted,
       max(created_at) as last_at,
       (array_agg(name order by created_at desc)
        filter (where coalesce(name, '') <> ''))[1] as name
from public.submissions
where user_email <> '' and contest_id is null
group by user_email, problem;

create or replace view public.problem_stats with (security_invoker = true) as
select problem,
       count(*) as subs,
       count(*) filter (where accepted) as accepted,
       max(score) as best,
       sum(score) as total_score,
       count(distinct user_email) filter (where accepted) as solvers
from public.submissions
where contest_id is null
group by problem;

-- ---------------------------------------------------------------------------
-- Functions. All security invoker with a pinned search_path: the caller's own
-- table privileges apply, and EXECUTE is granted per role below.
-- ---------------------------------------------------------------------------
create or replace function public.judge_queue_admit(
  p_job jsonb, p_queue_max integer, p_track_active boolean,
  p_quota_limit integer, p_quota_window integer
) returns jsonb language plpgsql security invoker set search_path = pg_catalog, public as $$
declare v_job public.judge_jobs; v_email text := coalesce(p_job->>'user_email', '');
  v_kind text := coalesce(p_job->>'kind', 'submit'); v_quota_kind text;
  v_first timestamptz; v_count integer;
begin
  -- Serialize admission across API replicas: queue and account checks plus insert
  -- form one transaction. A check in Python cannot provide this guarantee.
  perform pg_advisory_xact_lock(64723487231);
  if p_track_active and v_email <> '' and exists (
    select 1 from public.judge_jobs where user_email=v_email and track_active
      and status in ('queued','running')
  ) then
    return jsonb_build_object('error','you already have a submission judging, please wait','status_code',409);
  end if;
  if (select count(*) from public.judge_jobs where status='queued') >= p_queue_max then
    return jsonb_build_object('error','judge queue is full — try again in a moment','status_code',503,'retry_after',30);
  end if;
  v_quota_kind := case when v_kind='playground' then 'run' else 'submit' end;
  if p_quota_limit > 0 and p_quota_window > 0 and v_email <> '' then
    delete from public.judge_quota_events where admitted_at < now() - interval '7 days';
    select count(*), min(admitted_at) into v_count, v_first from public.judge_quota_events
      where user_email=v_email and kind=v_quota_kind
        and admitted_at > now() - make_interval(secs => p_quota_window);
    if v_count >= p_quota_limit then
      return jsonb_build_object('error','quota exceeded','status_code',429,
        'retry_after',greatest(1,ceil(extract(epoch from v_first + make_interval(secs => p_quota_window)-now()))::integer));
    end if;
    insert into public.judge_quota_events(user_email,kind) values(v_email,v_quota_kind);
  end if;
  insert into public.judge_jobs (id,kind,user_email,name,problem,language,filename,contest_id,
      source,target_id,reveal,tests_hash,grader_version,payload,track_active)
    values(coalesce((p_job->>'id')::uuid,gen_random_uuid()),v_kind,v_email,
      coalesce(p_job->>'name',''),coalesce(p_job->>'problem',''),coalesce(p_job->>'language',''),
      p_job->>'filename',p_job->>'contest_id',coalesce(p_job->>'source',''),
      (p_job->>'target_id')::uuid,coalesce(p_job->>'reveal','samples'),
      coalesce(p_job->>'tests_hash',''),coalesce(p_job->>'grader_version',''),
      coalesce(p_job->'payload','{}'::jsonb),p_track_active)
    returning * into v_job;
  return jsonb_build_object('job',to_jsonb(v_job));
end $$;

create or replace function public.judge_queue_claim(p_worker text,p_lease_seconds integer)
returns jsonb language plpgsql security invoker set search_path = pg_catalog, public as $$
declare v_job public.judge_jobs;
begin
  update public.judge_jobs set status='error',error='stale job dropped',finished_at=now()
    where status='queued' and created_at < now() - interval '24 hours';
  select * into v_job from public.judge_jobs
    where status='queued' or (status='running' and lease_until <= now())
    order by (kind='rejudge'), created_at, id for update skip locked limit 1;
  if not found then return null; end if;
  update public.judge_jobs set status='running',worker_id=p_worker,attempt=attempt+1,
      started_at=now(),lease_until=now()+make_interval(secs=>greatest(1,p_lease_seconds)),
      error='',finished_at=null
    where id=v_job.id returning * into v_job;
  return to_jsonb(v_job);
end $$;

create or replace function public.judge_queue_heartbeat(
  p_id uuid,p_worker text,p_attempt integer,p_lease_seconds integer
) returns boolean language plpgsql security invoker set search_path = pg_catalog, public as $$
begin
  update public.judge_jobs set lease_until=now()+make_interval(secs=>greatest(1,p_lease_seconds))
    where id=p_id and status='running' and worker_id=p_worker and attempt=p_attempt and lease_until>now();
  return found;
end $$;

create or replace function public.judge_queue_finish(
  p_id uuid,p_worker text,p_attempt integer,p_result jsonb,p_submission jsonb,p_error text
) returns jsonb language plpgsql security invoker set search_path = pg_catalog, public as $$
declare v_job public.judge_jobs; v_id uuid; v_sub public.submissions;
begin
  select * into v_job from public.judge_jobs where id=p_id for update;
  if not found or p_worker is null or p_attempt is null
      or v_job.worker_id is distinct from p_worker or v_job.attempt is distinct from p_attempt then
    return jsonb_build_object('applied',false);
  end if;
  if v_job.status in ('done','error') then
    return jsonb_build_object('applied',true,'submission_id',v_job.submission_id);
  end if;
  if v_job.status <> 'running' or v_job.lease_until is null or v_job.lease_until <= now() then
    return jsonb_build_object('applied',false);
  end if;
  -- History and terminal status commit together under the attempt fence.
  if p_submission is not null and coalesce(p_error,'')='' then
    v_sub := jsonb_populate_record(null::public.submissions,p_submission);
    if v_job.target_id is not null then
      update public.submissions set verdict=v_sub.verdict,accepted=v_sub.accepted,
        passed=v_sub.passed,total=v_sub.total,score=v_sub.score,counts=v_sub.counts,
        results=v_sub.results,detail=v_sub.detail,tests_hash=v_sub.tests_hash,
        grader_version=v_sub.grader_version,kind=v_sub.kind,job_id=p_id
        where id=v_job.target_id returning id into v_id;
    else
      insert into public.submissions(problem,language,filename,name,user_email,verdict,accepted,
        passed,total,score,counts,results,code,detail,tests_hash,grader_version,kind,contest_id,job_id)
      values(v_sub.problem,v_sub.language,v_sub.filename,coalesce(v_sub.name,''),coalesce(v_sub.user_email,''),
        v_sub.verdict,coalesce(v_sub.accepted,false),coalesce(v_sub.passed,0),coalesce(v_sub.total,0),
        coalesce(v_sub.score,0),coalesce(v_sub.counts,'{}'::jsonb),coalesce(v_sub.results,'[]'::jsonb),
        coalesce(v_sub.code,''),coalesce(v_sub.detail,''),coalesce(v_sub.tests_hash,''),
        coalesce(v_sub.grader_version,''),coalesce(v_sub.kind,'submit'),v_sub.contest_id,p_id)
      returning id into v_id;
    end if;
  end if;
  update public.judge_jobs set status=case when coalesce(p_error,'')='' then 'done' else 'error' end,
    result=case when p_result is null then null else p_result || jsonb_build_object('id',v_id) end,
    error=coalesce(p_error,''),finished_at=now(),lease_until=null,submission_id=v_id,
    -- The submission row owns the validated source. Terminal jobs need only
    -- the result; retaining source/payload would duplicate user code and
    -- playground input indefinitely in the queue table.
    source='',payload='{}'::jsonb where id=p_id;
  return jsonb_build_object('applied',true,'submission_id',v_id);
end $$;

create or replace function public.judge_queue_diagnostics() returns jsonb
language sql security invoker set search_path = pg_catalog, public as $$
  select jsonb_build_object('queued',count(*) filter(where status='queued'),
    'running',count(*) filter(where status='running')) from public.judge_jobs;
$$;

-- One transaction for every replica. Hash keys are supplied by the trusted
-- backend; no email addresses or OTPs are stored in this abuse-control table.
-- Global send ceiling is deliberately small for the initial Gmail test sender.
create or replace function public.auth_email_budget(
  p_email_key text, p_network_key text, p_action text
) returns jsonb language plpgsql security invoker set search_path = pg_catalog, public as $$
declare
  limits jsonb;
  item jsonb;
  current_time_ timestamptz := clock_timestamp();
  start_ timestamptz;
  seconds_ integer;
  hits_ integer;
  retry_ integer := 0;
begin
  if p_email_key !~ '^[0-9a-f]{64}$' or p_network_key !~ '^[0-9a-f]{64}$'
     or p_action not in ('request', 'verify') then
    raise exception 'invalid auth budget';
  end if;
  perform pg_advisory_xact_lock(hashtext('newbiegrader-email-budget'));
  current_time_ := clock_timestamp();
  if p_action = 'request' then
    limits := jsonb_build_array(
      jsonb_build_array('send:global', 100, 86400),
      jsonb_build_array('send:network:' || p_network_key, 50, 3600),
      jsonb_build_array('send:hour:' || p_email_key, 5, 3600),
      jsonb_build_array('send:minute:' || p_email_key, 1, 60));
  else
    limits := jsonb_build_array(
      jsonb_build_array('verify:global', 1000, 3600),
      jsonb_build_array('verify:network:' || p_network_key, 100, 900),
      jsonb_build_array('verify:email:' || p_email_key, 8, 900));
  end if;
  for item in select value from jsonb_array_elements(limits) loop
    seconds_ := (item->>2)::integer;
    start_ := current_time_;
    select attempts into hits_ from public.auth_rate_buckets
      where bucket = item->>0 and expires_at > current_time_;
    if coalesce(hits_, 0) >= (item->>1)::integer then
      select expires_at into start_ from public.auth_rate_buckets where bucket = item->>0;
      retry_ := greatest(retry_, ceil(extract(epoch from (start_ - current_time_)))::integer);
    end if;
  end loop;
  if retry_ > 0 then
    return jsonb_build_object('allowed', false, 'retry_after', retry_);
  end if;
  delete from public.auth_rate_buckets where expires_at <= current_time_;
  for item in select value from jsonb_array_elements(limits) loop
    seconds_ := (item->>2)::integer;
    start_ := current_time_;
    insert into public.auth_rate_buckets as b(bucket, window_start, attempts, expires_at)
      values (item->>0, start_, 1, start_ + make_interval(secs => seconds_))
    on conflict (bucket) do update set
      attempts = case when b.expires_at > current_time_ then b.attempts + 1 else 1 end,
      window_start = case when b.expires_at > current_time_ then b.window_start else excluded.window_start end,
      expires_at = case when b.expires_at > current_time_ then b.expires_at else excluded.expires_at end;
  end loop;
  return jsonb_build_object('allowed', true);
end $$;

-- Call only AFTER a successful server-to-server /verify response. The password
-- is chosen after email proof, never recovered from a pending signup/session.
-- The row lock serializes credential replacement; every replacement revokes
-- all previous session/remember versions without rewriting any history owner.
create or replace function public.auth_email_complete(
  p_email text, p_subject uuid, p_password_hash text, p_name text
) returns jsonb language plpgsql security invoker set search_path = pg_catalog, public as $$
declare
  account_ public.users%rowtype;
begin
  if p_email is null or p_email <> lower(btrim(p_email)) or length(p_email) > 254
     or p_subject is null or p_password_hash not like 'pbkdf2$%' then
    raise exception 'invalid verified identity';
  end if;
  perform pg_advisory_xact_lock(hashtext('identity:' || p_email));
  select * into account_ from public.users where email = p_email for update;
  if found and account_.email_auth_subject is not null and account_.email_auth_subject <> p_subject then
    raise exception 'identity subject mismatch';
  end if;
  insert into public.users as u(email, name, password_hash, email_verified_at, email_auth_subject, auth_version)
    values (p_email, left(p_name, 50), p_password_hash, clock_timestamp(), p_subject, 1)
  on conflict (email) do update set name = excluded.name, password_hash = excluded.password_hash,
    email_verified_at = excluded.email_verified_at, email_auth_subject = excluded.email_auth_subject,
    auth_version = u.auth_version + 1
  returning * into account_;
  return jsonb_build_object('email', account_.email, 'name', account_.name,
                           'auth_version', account_.auth_version);
end $$;

-- Google subjects are bound explicitly. A verified password account is NOT
-- silently linked because an OAuth response happens to have the same email.
-- A first trusted Google claim clears any unverified legacy password.
create or replace function public.auth_google_identity(
  p_email text, p_subject text, p_name text
) returns jsonb language plpgsql security invoker set search_path = pg_catalog, public as $$
declare
  account_ public.users%rowtype;
begin
  if p_email is null or p_email <> lower(btrim(p_email)) or p_subject is null
     or length(p_subject) not between 1 and 255 then
    raise exception 'invalid google identity';
  end if;
  perform pg_advisory_xact_lock(hashtext('identity:' || p_email));
  select * into account_ from public.users where email = p_email for update;
  if found and account_.email_verified_at is not null then
    if account_.google_subject is distinct from p_subject then
      return jsonb_build_object('link_required', true);
    end if;
  else
    insert into public.users as u(email, name, password_hash, email_verified_at, google_subject, auth_version)
      values (p_email, left(p_name, 50), '', clock_timestamp(), p_subject, 1)
    on conflict (email) do update set name = excluded.name, password_hash = '',
      email_verified_at = excluded.email_verified_at, google_subject = excluded.google_subject,
      auth_version = u.auth_version + 1
    returning * into account_;
  end if;
  return jsonb_build_object('email', account_.email, 'name', account_.name,
                           'auth_version', account_.auth_version);
end $$;

create or replace function public.auth_session_start(
  p_token_hash text, p_email text, p_auth_version bigint,
  p_remember boolean, p_pending boolean, p_provider text, p_picture text,
  p_previous text[]
) returns jsonb language plpgsql security invoker set search_path = '' as $$
declare
  account_ public.users%rowtype;
  current_ timestamptz := clock_timestamp();
  remember_ timestamptz;
begin
  if p_token_hash is null or p_token_hash !~ '^[0-9a-f]{64}$'
     or p_remember is null or p_pending is null
     or p_provider is null or p_provider not in ('google','password')
     or p_picture is null or length(p_picture) > 2048
     or p_previous is null or cardinality(p_previous) > 2
     or exists (select 1 from unnest(p_previous) x where x is null or x !~ '^[0-9a-f]{64}$')
     or p_token_hash = any(p_previous) then
    raise exception 'invalid session parameters';
  end if;
  -- Serialize credential replacement with creation, not all user requests.
  select * into account_ from public.users where email = p_email for share;
  if not found or account_.email_verified_at is null
     or account_.auth_version is distinct from p_auth_version then
    return jsonb_build_object('valid', false);
  end if;
  if p_remember then remember_ := current_ + interval '365 days'; end if;
  update public.web_sessions set revoked_at = current_, expires_at = current_
    where token_hash = any(p_previous) and revoked_at is null;
  -- Indexed, bounded cleanup on login. No periodic job or write on each poll.
  delete from public.web_sessions where token_hash in (
    select token_hash from public.web_sessions where expires_at <= current_
      order by expires_at limit 100 for update skip locked);
  insert into public.web_sessions(token_hash,user_email,auth_version,expires_at,
      remember_until,activated,handoff_until,provider,picture)
    values (p_token_hash,p_email,p_auth_version,
      case when p_pending then current_ + interval '5 minutes'
           else greatest(current_ + interval '60 days', remember_) end,
      remember_,not p_pending,
      case when p_pending then current_ + interval '5 minutes' else null end,
      p_provider,p_picture);
  return jsonb_build_object('valid', true);
end $$;

create or replace function public.auth_session_identity(
  p_token_hash text, p_mode text, p_previous text[]
) returns jsonb language plpgsql security invoker set search_path = '' as $$
declare
  session_ record;
  current_ timestamptz := clock_timestamp();
  claimed_ text;
begin
  if p_token_hash is null or p_token_hash !~ '^[0-9a-f]{64}$'
     or p_mode is null or p_mode not in ('session','remember','finish')
     or p_previous is null or cardinality(p_previous) > 2
     or exists (select 1 from unnest(p_previous) x where x is null or x !~ '^[0-9a-f]{64}$') then
    raise exception 'invalid session parameters';
  end if;
  select s.*,u.name as account_name,u.auth_version as live_version,u.email_verified_at
    into session_ from public.web_sessions s join public.users u on u.email=s.user_email
    where s.token_hash=p_token_hash;
  if not found or session_.revoked_at is not null or session_.expires_at <= current_
     or session_.email_verified_at is null or session_.auth_version <> session_.live_version then
    return jsonb_build_object('valid', false);
  end if;
  if p_mode='finish' then
    if session_.activated or session_.handoff_until is null or session_.handoff_until <= current_ then
      return jsonb_build_object('valid', false);
    end if;
    -- Atomic consumption survives API restarts and concurrent replicas.
    update public.web_sessions set activated=true,handoff_until=null,
        remember_until=case when remember_until is not null then current_ + interval '365 days' end,
        expires_at=case when remember_until is not null then current_ + interval '365 days'
                        else current_ + interval '60 days' end
      where token_hash=p_token_hash and not activated and revoked_at is null
        and handoff_until > clock_timestamp() and expires_at > clock_timestamp()
      returning token_hash into claimed_;
    if not found then return jsonb_build_object('valid', false); end if;
    update public.web_sessions set revoked_at=current_,expires_at=current_
      where token_hash=any(p_previous) and token_hash<>p_token_hash and revoked_at is null;
  else
    if not session_.activated or (p_mode='remember' and
        (session_.remember_until is null or session_.remember_until <= current_)) then
      return jsonb_build_object('valid', false);
    end if;
    -- Cookie signatures still enforce the existing rolling 30-day age. A
    -- 60-day row retention, renewed only below 45 days, cannot expire a valid
    -- 30-day cookie and avoids a DB UPDATE on every authenticated request.
    if session_.expires_at < current_ + interval '45 days' then
      update public.web_sessions set expires_at=current_ + interval '60 days'
        where token_hash=p_token_hash and revoked_at is null
          and expires_at < current_ + interval '45 days';
    end if;
  end if;
  return jsonb_build_object('valid',true,'user',jsonb_build_object(
    'email',session_.user_email,'name',coalesce(nullif(session_.account_name,''),session_.user_email),
    'auth_version',session_.live_version,'provider',session_.provider,'picture',session_.picture));
end $$;

create or replace function public.auth_session_revoke(p_token_hashes text[])
returns jsonb language plpgsql security invoker set search_path = '' as $$
declare current_ timestamptz := clock_timestamp();
begin
  if p_token_hashes is null or cardinality(p_token_hashes)>2
     or exists (select 1 from unnest(p_token_hashes) x where x is null or x !~ '^[0-9a-f]{64}$') then
    raise exception 'invalid session parameters';
  end if;
  update public.web_sessions set revoked_at=current_,expires_at=current_
    where token_hash=any(p_token_hashes) and revoked_at is null;
  return jsonb_build_object('ok',true);
end $$;

-- ---------------------------------------------------------------------------
-- Privileges. Nothing is granted to PUBLIC.
-- ---------------------------------------------------------------------------
revoke all on all tables in schema public from public;
revoke execute on all functions in schema public from public;

-- API: every table. web_sessions, auth_rate_buckets and judge_quota_events are
-- reached through invoker functions that run with these grants.
grant select, insert, update, delete on
  public.submissions, public.profiles, public.reports, public.users,
  public.judge_jobs, public.judge_quota_events, public.contests,
  public.contest_registrations, public.contest_announcements, public.problems,
  public.admins, public.auth_rate_buckets, public.web_sessions
  to newbiegrader_api;
grant select on public.leaderboard_scores, public.problem_stats to newbiegrader_api;
grant execute on function
  public.judge_queue_admit(jsonb,integer,boolean,integer,integer),
  public.judge_queue_diagnostics(),
  public.auth_email_budget(text,text,text),
  public.auth_email_complete(text,uuid,text,text),
  public.auth_google_identity(text,text,text),
  public.auth_session_start(text,text,bigint,boolean,boolean,text,text,text[]),
  public.auth_session_identity(text,text,text[]),
  public.auth_session_revoke(text[])
  to newbiegrader_api;

-- Worker: only the two tables a judge job touches, and the four queue calls.
grant select, insert, update on public.submissions, public.judge_jobs to newbiegrader_worker;
grant execute on function
  public.judge_queue_claim(text,integer),
  public.judge_queue_heartbeat(uuid,text,integer,integer),
  public.judge_queue_finish(uuid,text,integer,jsonb,jsonb,text),
  public.judge_queue_diagnostics()
  to newbiegrader_worker;

reset role;
