"""Durable saved-version delivery; retries never regenerate reports."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import uuid
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from v2.pregame_system.prediction_versions import _instant, game_identity

STAGES = {"EARLY_PREVIEW", "UPDATED_PREVIEW", "FINAL_PREGAME"}
DEFAULT_PORTFOLIO_EDITORIAL_DIR = Path(__file__).parent / 'output' / 'editorial-overrides'


def saved_publication(database, game_id, version_id):
    """Read a published row without initializing its database."""
    if '::editorial::' in version_id:
        base_id,expected=version_id.split('::editorial::',1)
        base=saved_publication(database,game_id,base_id)
        from v2.pregame_system.storage import content_hash
        artifact=json.loads((DEFAULT_PORTFOLIO_EDITORIAL_DIR/(base_id+'.json')).read_text(encoding='utf-8'))
        editorial=artifact.get('editorial')or{}
        if (artifact.get('purpose')!='factual_correction' or artifact.get('base_prediction_version_id')!=base_id
                or artifact.get('base_prediction_payload_sha256')!=base['payload_sha256']
                or content_hash(editorial)!=artifact.get('editorial_sha256')):
            raise ValueError('public_release_editorial_correction_invalid')
        if artifact['editorial_sha256']!=expected:raise ValueError('public_release_article_selection_changed')
        return {**base,'prediction_payload_sha256':base['payload_sha256'],
            'payload_sha256':content_hash({'base':base['payload_sha256'],'editorial':expected}),
            'editorial_sha256':expected}
    if '::postgame::' in version_id:
        base_id,post_key=version_id.split('::postgame::',1)
        post_key,_,requested_view=post_key.partition('::view::')
        post_id,_,requested_article=post_key.partition('::article::')
        base=saved_publication(database,game_id,base_id)
        with closing(sqlite3.connect(Path(database).resolve().as_uri()+'?mode=ro',uri=True))as db:
            db.row_factory=sqlite3.Row
            row=db.execute('SELECT payload_json,payload_sha256 FROM postgame_versions WHERE game_id=? AND version_id=?',(game_id,post_id)).fetchone()
        if not row or hashlib.sha256(row['payload_json'].encode()).hexdigest()!=row['payload_sha256']:
            raise ValueError('public_release_postgame_missing_or_corrupt')
        post=json.loads(row['payload_json'])
        if post.get('base_prediction_version_id')!=base_id or post.get('base_prediction_payload_sha256')!=base['payload_sha256']:
            raise ValueError('public_release_postgame_base_mismatch')
        from v2.postgame_system.editorial import load_postgame_editorial,build_public_postgame_payload,build_public_postgame_entry,build_postgame_context,_publication_editorial
        artifact=load_postgame_editorial(post)
        if not artifact:raise ValueError('public_release_postgame_article_pending')
        class ReadStore:
            path=Path(database)
            def connect(self):
                connection=sqlite3.connect(Path(database).resolve().as_uri()+'?mode=ro',uri=True)
                connection.row_factory=sqlite3.Row
                return connection
        public=build_public_postgame_payload(ReadStore(),post)
        entry=build_public_postgame_entry(ReadStore(),post)
        from v2.pregame_system.storage import content_hash
        display_sha=content_hash({'postgame':public,'entry':entry})
        if requested_article and requested_article!=artifact['artifact_sha256']:
            raise ValueError('public_release_article_selection_changed')
        if requested_view and requested_view!=display_sha:
            raise ValueError('public_release_article_selection_changed')
        if artifact.get('article_contract_version')=='postgame-fan-v2' or (artifact.get('title_policy')or{}).get('required'):
            from v2.postgame_system.article_contract import validate_article
            issues=validate_article(_publication_editorial(artifact),build_postgame_context(ReadStore(),game_id))
            if issues:raise ValueError('public_release_postgame_article_contract:'+json.dumps(issues,ensure_ascii=False))
        digest=hashlib.sha256((row['payload_sha256']+artifact['artifact_sha256']+display_sha).encode()).hexdigest()
        return {**base,'payload_sha256':digest,'prediction_payload_sha256':base['payload_sha256'],
            'postgame':post,'public_postgame':public,'public_postgame_entry':entry,
            'artifact_sha256':artifact['artifact_sha256'],'display_sha256':display_sha}
    if not isinstance(game_id, str) or not re.fullmatch(r'(espn|nba):\d+', game_id):
        raise ValueError('public_release_game_id_invalid')
    with closing(sqlite3.connect(Path(database).resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT payload_json,payload_sha256 FROM prediction_versions WHERE game_id=? AND version_id=?", (game_id, version_id)).fetchone()
        state = db.execute("SELECT registered_game_json,policy_config_json FROM game_prediction_state WHERE game_id=?", (game_id,)).fetchone()
    if not row or hashlib.sha256(row['payload_json'].encode()).hexdigest() != row['payload_sha256']:
        raise ValueError('public_release_version_missing_or_corrupt')
    version = json.loads(row['payload_json'])
    if not state or not state['registered_game_json'] or not json.loads(state['policy_config_json']).get('report_enabled', True):
        raise ValueError('public_release_game_not_enabled')
    if version.get('stage') not in STAGES or version.get('game_identity') != game_identity(json.loads(state['registered_game_json'])) or version.get('game_id') != game_id:
        raise ValueError('public_release_identity_or_stage_invalid')
    tip = _instant(version['game_identity']['scheduled_tipoff'], 'tipoff')
    if any(_instant(version[field], field) >= tip for field in ('input_cutoff_at', 'generated_at', 'published_at')):
        raise ValueError('public_release_not_legal_pregame')
    return {'version': version, 'payload_sha256': row['payload_sha256']}


class PublicationQueue:
    def __init__(self, path, *, clock=None, max_attempts=12, lease_seconds=120):
        self.path = Path(path).resolve()
        self.root = self.path.parent
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.max_attempts, self.lease_seconds = max_attempts, lease_seconds
        if not 1 <= max_attempts <= 48 or lease_seconds < 3:
            raise ValueError('invalid_publication_limits')
        self.root.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS publication_jobs(
                version_id TEXT PRIMARY KEY,game_id TEXT NOT NULL,source_database TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL,status TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,next_attempt_at TEXT NOT NULL,owner_token TEXT,lease_until TEXT,
                last_error TEXT,result_json TEXT,finished_at TEXT)''')

    def connect(self):
        db = sqlite3.connect(self.path, timeout=1)
        db.row_factory = sqlite3.Row
        return db

    def enqueue(self, database, game_id, version_id):
        source = saved_publication(database, game_id, version_id)
        path, stamp = str(Path(database).resolve()), self.clock().isoformat()
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO publication_jobs(version_id,game_id,source_database,payload_sha256,status,created_at,next_attempt_at) VALUES(?,?,?,?,'pending',?,?)", (version_id, game_id, path, source['payload_sha256'], stamp, stamp))
            row = db.execute('SELECT * FROM publication_jobs WHERE version_id=?', (version_id,)).fetchone()
            if (row['game_id'], row['source_database'], row['payload_sha256']) != (game_id, path, source['payload_sha256']):
                raise ValueError('publication_job_identity_conflict')
        return dict(row)

    def discover(self, database, *, game_ids=None, stages=None):
        with closing(sqlite3.connect(Path(database).resolve().as_uri() + '?mode=ro', uri=True)) as db:
            rows = db.execute('SELECT v.game_id,v.version_id,v.payload_json FROM game_prediction_state s JOIN prediction_versions v ON v.version_id=COALESCE(s.locked_version_id,s.current_version_id)').fetchall()
        result = {'eligible': 0, 'errors': []}
        for gid, vid, raw in rows:
            if game_ids is not None and gid not in game_ids: continue
            if json.loads(raw).get('stage') not in (STAGES if stages is None else set(stages)): continue
            try:
                self.enqueue(database, gid, vid)
                result['eligible'] += 1
            except ValueError as error:
                result['errors'].append({'version_id': vid, 'error': str(error)})
        return result

    def discover_postgames(self,database,*,game_ids=None):
        result={'eligible':0,'errors':[]}
        with closing(sqlite3.connect(Path(database).resolve().as_uri()+'?mode=ro',uri=True))as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='postgame_versions'").fetchone():return result
            rows=db.execute('''SELECT p.game_id,p.version_id,p.base_prediction_version_id FROM postgame_versions p
                WHERE p.version_id=(SELECT n.version_id FROM postgame_versions n WHERE n.game_id=p.game_id ORDER BY n.published_at DESC,n.rowid DESC LIMIT 1)''').fetchall()
        for gid,vid,base in rows:
            if game_ids is not None and gid not in game_ids:continue
            try:
                key=base+'::postgame::'+vid
                source=saved_publication(database,gid,key)
                self.enqueue(database,gid,key+'::view::'+source['display_sha256']);result['eligible']+=1
            except ValueError as exc:
                if str(exc)!='public_release_postgame_article_pending':result['errors'].append({'game_id':gid,'error':str(exc)})
        return result

    def claim(self, version_id=None, *, game_ids=None):
        at = self.clock()
        if game_ids is not None and not game_ids:
            return None
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("UPDATE publication_jobs SET status=CASE WHEN attempts>=? THEN 'failed' ELSE 'retry' END,owner_token=NULL,lease_until=NULL,last_error='publication_lease_expired' WHERE status='running' AND lease_until<=?", (self.max_attempts, at.isoformat()))
            scope = '' if game_ids is None else ' AND game_id IN (' + ','.join('?' for _ in game_ids) + ')'
            args = [self.max_attempts, at.isoformat(), version_id, version_id]
            if game_ids is not None: args.extend(sorted(game_ids))
            row = db.execute("SELECT * FROM publication_jobs WHERE status IN ('pending','retry') AND attempts<? AND next_attempt_at<=? AND (? IS NULL OR version_id=?)" + scope + " ORDER BY created_at,version_id LIMIT 1", args).fetchone()
            if row is None: return None
            db.execute("UPDATE publication_jobs SET status='running',attempts=attempts+1,owner_token=?,lease_until=? WHERE version_id=?", (str(uuid.uuid4()), (at + timedelta(seconds=self.lease_seconds)).isoformat(), row['version_id']))
            return dict(db.execute('SELECT * FROM publication_jobs WHERE version_id=?', (row['version_id'],)).fetchone())

    def heartbeat(self, job):
        at = self.clock()
        with self.connect() as db:
            return db.execute("UPDATE publication_jobs SET lease_until=? WHERE version_id=? AND status='running' AND owner_token=? AND lease_until>?", ((at + timedelta(seconds=self.lease_seconds)).isoformat(), job['version_id'], job['owner_token'], at.isoformat())).rowcount == 1

    @contextmanager
    def keep_alive(self, job):
        stopped = threading.Event()
        def renew():
            while not stopped.wait(min(30, self.lease_seconds / 3)):
                try:
                    if not self.heartbeat(job): return
                except sqlite3.Error: return
        thread = threading.Thread(target=renew, daemon=True, name='publication-lease')
        thread.start()
        try: yield
        finally:
            stopped.set()
            thread.join(timeout=2)

    def _finish(self, job, *, result=None, error=None):
        at = self.clock()
        if error:
            status = 'failed' if job['attempts'] >= self.max_attempts else 'retry'
            next_at = at + timedelta(seconds=min(900, 30 * 2 ** min(job['attempts'] - 1, 5)))
        else:
            status = 'superseded' if result.get('status') == 'superseded' else 'succeeded'
            next_at = at
        with self.connect() as db:
            changed = db.execute("UPDATE publication_jobs SET status=?,next_attempt_at=?,owner_token=NULL,lease_until=NULL,last_error=?,result_json=?,finished_at=? WHERE version_id=? AND status='running' AND owner_token=? AND lease_until>?", (status, next_at.isoformat(), str(error)[:300] if error else None, json.dumps(result, ensure_ascii=False) if result else None, at.isoformat() if status in {'succeeded','superseded','failed'} else None, job['version_id'], job['owner_token'], at.isoformat())).rowcount
        if not changed: raise ValueError('publication_claim_lost')
        if status=='succeeded' and result.get('commit'):
            from .publication_deployment import DeploymentChecks
            DeploymentChecks(self).enqueue(job,result)
        return {'status': status, 'version_id': job['version_id'], 'attempts': job['attempts'], 'result': result}

    def run(self, job, publisher):
        with self.keep_alive(job):
            try: result = publisher(job)
            except Exception as error:
                if str(error)=='public_release_article_selection_changed':
                    return self._finish(job,result={'status':'superseded','reason':'article_selection_changed'})
                return self._finish(job, error=error)
            if result.get('status') not in {'pushed','already_published','superseded'}:
                return self._finish(job, error='publication_result_not_success')
            return self._finish(job, result=result)

    def summary(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute('SELECT version_id,game_id,status,attempts,next_attempt_at,last_error FROM publication_jobs ORDER BY created_at')]
