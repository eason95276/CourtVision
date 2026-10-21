"""Persistent, bounded retries for separately published Postgame articles."""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from .editorial import DEFAULT_OUTPUT_ROOT, load_postgame_editorial


def initialize_writer_attempts(store):
    with store.connect() as db:
        db.execute('''CREATE TABLE IF NOT EXISTS postgame_writer_attempts(
          postgame_version_id TEXT PRIMARY KEY, game_id TEXT NOT NULL, status TEXT NOT NULL,
          attempt_count INTEGER NOT NULL, next_attempt_at TEXT, lease_until TEXT, error TEXT,
          artifact_sha256 TEXT, updated_at TEXT NOT NULL)''')
        db.execute('''CREATE TABLE IF NOT EXISTS postgame_writer_retry_grants(
          postgame_version_id TEXT PRIMARY KEY,additional_attempts INTEGER NOT NULL CHECK(additional_attempts=1),
          not_before TEXT NOT NULL,expires_at TEXT NOT NULL,reason TEXT NOT NULL,
          prior_attempt_json TEXT NOT NULL,granted_at TEXT NOT NULL)''')


def grant_reviewed_writer_retry(store,version_id,*,reason,not_before,expires_at,clock=None):
    """One explicit, audited extra attempt; never erase/reset the original three."""
    import json
    from v2.pregame_system.cost_ledger import instant
    at=(clock or(lambda:datetime.now(timezone.utc)))()
    start,end=instant(not_before),instant(expires_at)
    if not str(reason).strip() or end<=start or end<=at:
        raise ValueError('writer_retry_grant_invalid')
    initialize_writer_attempts(store)
    with store.connect()as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT * FROM postgame_writer_attempts WHERE postgame_version_id=?',(version_id,)).fetchone()
        if not row or row['status']!='exhausted' or row['attempt_count']!=3 or row['lease_until']:
            raise ValueError('writer_retry_grant_requires_reviewed_exhausted_attempts')
        if db.execute('SELECT 1 FROM postgame_writer_retry_grants WHERE postgame_version_id=?',(version_id,)).fetchone():
            raise ValueError('writer_retry_grant_already_exists')
        db.execute('INSERT INTO postgame_writer_retry_grants VALUES(?,1,?,?,?,?,?)',
            (version_id,start.isoformat(),end.isoformat(),str(reason).strip(),json.dumps(dict(row)),at.isoformat()))
        db.commit()


def writer_attempt_limit(store,version_id,at):
    from v2.pregame_system.cost_ledger import instant
    with store.connect()as db:
        grant=db.execute('SELECT * FROM postgame_writer_retry_grants WHERE postgame_version_id=?',(version_id,)).fetchone()
    return 3+(1 if grant and instant(grant['not_before'])<=instant(at)<instant(grant['expires_at'])else 0)


def attempt_postgame_writer(store, postgame, generator, *, clock=None, output_root=DEFAULT_OUTPUT_ROOT,
                            prefer_off_peak=True, holidays=(), evening_hour=20):
    clock=clock or (lambda:datetime.now(timezone.utc))
    at=clock();version_id=postgame['version_id']
    initialize_writer_attempts(store)
    try:
        artifact=load_postgame_editorial(postgame,output_root=output_root)
    except ValueError as exc:
        return {'editorial_status':'failed','editorial_error_type':str(exc)}
    if artifact:
        with store.connect() as db:
            db.execute('''INSERT INTO postgame_writer_attempts VALUES(?,?,'succeeded',0,NULL,NULL,NULL,?,?)
              ON CONFLICT(postgame_version_id) DO UPDATE SET status='succeeded',artifact_sha256=excluded.artifact_sha256,
              lease_until=NULL,error=NULL,updated_at=excluded.updated_at''',
              (version_id,postgame['game_id'],artifact['artifact_sha256'],at.isoformat()))
        return {'editorial_status':'already_exists'}
    from v2.pregame_system.cost_ledger import peak,next_off_peak,production_scope,component
    due=at
    waiting_status=None
    if evening_hour is not None:
        from v2.pregame_system.publication_timing import next_evening
        due=next_evening(at,evening_hour)
        if due>at:waiting_status='waiting_evening'
    if prefer_off_peak and peak(due,holidays):
        due=next_off_peak(due,due+timedelta(days=1),holidays)
        waiting_status=waiting_status or 'waiting_off_peak'
    if waiting_status:
        due=due.isoformat()
        with store.connect() as db:
            db.execute('''INSERT INTO postgame_writer_attempts VALUES(?,?,'waiting',0,?,NULL,NULL,NULL,?)
              ON CONFLICT(postgame_version_id) DO UPDATE SET next_attempt_at=excluded.next_attempt_at,
              updated_at=excluded.updated_at WHERE postgame_writer_attempts.status!='succeeded'
              AND (postgame_writer_attempts.lease_until IS NULL OR postgame_writer_attempts.lease_until<=excluded.updated_at)''',
              (version_id,postgame['game_id'],due,at.isoformat()))
        return {'editorial_status':waiting_status,'next_attempt_at':due}
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT * FROM postgame_writer_attempts WHERE postgame_version_id=?',(version_id,)).fetchone()
        # Read the separately recorded grant on this transaction's connection.
        grant=db.execute('SELECT * FROM postgame_writer_retry_grants WHERE postgame_version_id=?',(version_id,)).fetchone()
        from v2.pregame_system.cost_ledger import instant
        limit=3+(1 if grant and instant(grant['not_before'])<=instant(at)<instant(grant['expires_at'])else 0)
        if row and (row['status']=='succeeded' or row['attempt_count']>=limit or
                    (row['lease_until'] and row['lease_until']>at.isoformat()) or
                    (row['next_attempt_at'] and row['next_attempt_at']>at.isoformat())):
            db.commit()
            return {'editorial_status':'exhausted' if row['attempt_count']>=limit else 'waiting'}
        count=(row['attempt_count'] if row else 0)+1
        db.execute('''INSERT INTO postgame_writer_attempts VALUES(?,?,'running',?,NULL,?,NULL,NULL,?)
          ON CONFLICT(postgame_version_id) DO UPDATE SET status='running',attempt_count=excluded.attempt_count,
          next_attempt_at=NULL,lease_until=excluded.lease_until,error=NULL,updated_at=excluded.updated_at''',
          (version_id,postgame['game_id'],count,(at+timedelta(minutes=6)).isoformat(),at.isoformat()))
        db.commit()
    try:
        with production_scope(store.path,postgame['game_id'],'POSTGAME',version_id+':'+str(count),holidays=holidays,retry=count-1):
            with component('Postgame Editorial'):
                result=generator(store,postgame['game_id'],postgame_version_id=version_id)
        status=getattr(result,'status','failed');success=status in {'success','already_exists'}
        artifact=load_postgame_editorial(postgame,output_root=output_root) if success else None
        if success and artifact is None:
            raise ValueError('postgame_writer_success_without_verified_artifact')
        error=None if success else getattr(result,'error_type',None) or 'writer_failed'
    except Exception as exc:
        success=False;status='failed';artifact=None;error=type(exc).__name__+':'+str(exc)[:240]
    completed=clock()
    with store.connect() as db:
        db.execute('''UPDATE postgame_writer_attempts SET status=?,next_attempt_at=?,lease_until=NULL,
          error=?,artifact_sha256=?,updated_at=? WHERE postgame_version_id=?''',
          ('succeeded' if success else 'exhausted' if count>=limit else 'failed',
           None if success or count>=limit else (completed+timedelta(minutes=5*2**(count-1))).isoformat(),
           error,artifact.get('artifact_sha256') if artifact else None,completed.isoformat(),version_id))
    return {'editorial_status':status,'editorial_attempt':count,**({'editorial_error_type':error} if error else {})}
