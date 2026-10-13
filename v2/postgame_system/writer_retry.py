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


def attempt_postgame_writer(store, postgame, generator, *, clock=None, output_root=DEFAULT_OUTPUT_ROOT):
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
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT * FROM postgame_writer_attempts WHERE postgame_version_id=?',(version_id,)).fetchone()
        if row and (row['status']=='succeeded' or row['attempt_count']>=3 or
                    (row['lease_until'] and row['lease_until']>at.isoformat()) or
                    (row['next_attempt_at'] and row['next_attempt_at']>at.isoformat())):
            db.commit()
            return {'editorial_status':'exhausted' if row['attempt_count']>=3 else 'waiting'}
        count=(row['attempt_count'] if row else 0)+1
        db.execute('''INSERT INTO postgame_writer_attempts VALUES(?,?,'running',?,NULL,?,NULL,NULL,?)
          ON CONFLICT(postgame_version_id) DO UPDATE SET status='running',attempt_count=excluded.attempt_count,
          next_attempt_at=NULL,lease_until=excluded.lease_until,error=NULL,updated_at=excluded.updated_at''',
          (version_id,postgame['game_id'],count,(at+timedelta(minutes=3)).isoformat(),at.isoformat()))
        db.commit()
    try:
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
          ('succeeded' if success else 'exhausted' if count>=3 else 'failed',
           None if success or count>=3 else (completed+timedelta(minutes=5*2**(count-1))).isoformat(),
           error,artifact.get('artifact_sha256') if artifact else None,completed.isoformat(),version_id))
    return {'editorial_status':status,'editorial_attempt':count,**({'editorial_error_type':error} if error else {})}
