"""Evaluation-only transactional phase ledger and append-only receipts.

This is a workflow boundary, not an OS sandbox against the machine owner.
"""
import json, sqlite3, sys
import os,re
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'golden_set/v1'))
from asset_scanner import canonical_hash
from evaluation_integrity import GOLDEN_ID, DATASET_ID

HERE = Path(__file__).resolve().parent
DEFAULT_DB = HERE / 'runtime/control.sqlite'
PHASES = ('PREPARED', 'PREGAME_GENERATED', 'PREGAME_ANNOTATING', 'PREGAME_FROZEN',
          'POSTGAME_GENERATED', 'POSTGAME_ANNOTATING', 'POSTGAME_FROZEN')


def now():
    return datetime.now(timezone.utc).isoformat()


def exclusive(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x', encoding='utf-8') as file:
        json.dump(value, file, ensure_ascii=False, indent=2, allow_nan=False)


class Control:
    def __init__(self, path=DEFAULT_DB):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.tx() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY CHECK(id=1), phase TEXT NOT NULL,
                    golden TEXT NOT NULL, pre_revision TEXT, post_revision TEXT);
                CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY, event TEXT, timestamp TEXT,
                    revision TEXT, actor TEXT, details TEXT);
                CREATE TABLE IF NOT EXISTS attempts (revision TEXT, stage TEXT, sample TEXT, attempt INTEGER,
                    status TEXT NOT NULL, artifact TEXT, artifact_hash TEXT,
                    PRIMARY KEY(revision,stage,sample,attempt));
                CREATE TABLE IF NOT EXISTS profiles (revision TEXT PRIMARY KEY, profile_hash TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS freezes (stage TEXT, revision INTEGER, receipt TEXT NOT NULL,
                    PRIMARY KEY(stage,revision));
                CREATE TABLE IF NOT EXISTS workspaces (stage TEXT PRIMARY KEY, identity TEXT NOT NULL);
                CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit BEGIN SELECT RAISE(ABORT,'append only'); END;
                CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit BEGIN SELECT RAISE(ABORT,'append only'); END;
                CREATE TRIGGER IF NOT EXISTS freeze_no_update BEFORE UPDATE ON freezes BEGIN SELECT RAISE(ABORT,'immutable'); END;
                CREATE TRIGGER IF NOT EXISTS freeze_no_delete BEFORE DELETE ON freezes BEGIN SELECT RAISE(ABORT,'immutable'); END;
            ''')
            db.execute('INSERT OR IGNORE INTO state(id,phase,golden) VALUES(1,?,?)', ('PREPARED', GOLDEN_ID))

    @contextmanager
    def tx(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def state(self):
        with self.tx() as db:
            state = dict(db.execute('SELECT * FROM state').fetchone())
        if state['golden'] != GOLDEN_ID:
            raise ValueError('wrong Golden revision in control ledger')
        return state

    def audit(self, event, actor, details, db=None):
        # Only callers' bounded metadata enters the log; never credentials/raw errors.
        if not actor or not isinstance(details, dict):
            raise ValueError('audit actor and details required')
        encoded=json.dumps(details,ensure_ascii=False)
        for name,value in os.environ.items():
            if value and len(value)>=8 and re.search('KEY|TOKEN|SECRET|PASSWORD',name,re.I):
                encoded=encoded.replace(value,'[REDACTED]')
                actor=actor.replace(value,'[REDACTED]')
        details=json.loads(encoded)
        if db is None:
            with self.tx() as conn:
                self.audit(event, actor, details, conn)
            return
        db.execute('INSERT INTO audit(event,timestamp,revision,actor,details) VALUES(?,?,?,?,?)',
                   (event, now(), GOLDEN_ID, actor, json.dumps(details, ensure_ascii=False)))

    def assert_phase(self, expected):
        if self.state()['phase'] not in expected:
            raise ValueError('illegal phase: ' + self.state()['phase'])

    def postgame_gate(self, actor, override_reason=None):
        if self.state()['phase'] in PHASES[3:]:
            return
        if not override_reason or not override_reason.strip():
            raise ValueError('Postgame requires PREGAME_FROZEN')
        self.audit('override_used', actor, {'operation': 'postgame_workspace', 'reason': override_reason})

    def claim(self, revision, stage, sample, profile_hash, actor, retry=False):
        with self.tx() as db:
            old = db.execute('SELECT profile_hash FROM profiles WHERE revision=?', (revision,)).fetchone()
            if old and old[0] != profile_hash:
                raise ValueError('generation profile changed within revision')
            db.execute('INSERT OR IGNORE INTO profiles VALUES(?,?)', (revision, profile_hash))
            rows = db.execute('SELECT * FROM attempts WHERE revision=? AND stage=? AND sample=? ORDER BY attempt',
                              (revision, stage, sample)).fetchall()
            if rows and (not retry or any(r['status'] in ('RUNNING', 'ACCEPTED') for r in rows)):
                raise ValueError('output overwrite or unapproved retry')
            if retry and not rows:
                raise ValueError('retry requires a prior failed attempt')
            n = len(rows) + 1
            db.execute('INSERT INTO attempts VALUES(?,?,?,?,?,?,?)', (revision, stage, sample, n, 'RUNNING', None, None))
            self.audit('generation_started', actor, {'sample': sample, 'stage': stage, 'generation_revision': revision, 'attempt': n}, db)
            return n

    def finish(self, revision, stage, sample, attempt, status, path, digest, actor):
        with self.tx() as db:
            cur = db.execute('UPDATE attempts SET status=?,artifact=?,artifact_hash=? WHERE revision=? AND stage=? AND sample=? AND attempt=? AND status=?',
                             (status, str(path), digest, revision, stage, sample, attempt, 'RUNNING'))
            if cur.rowcount != 1:
                raise ValueError('attempt already finalized')
            self.audit('generation_completed', actor, {'sample': sample, 'stage': stage, 'status': status, 'attempt': attempt, 'generation_revision': revision}, db)

    def accepted(self, revision, stage, sample):
        with self.tx() as db:
            row = db.execute('SELECT * FROM attempts WHERE revision=? AND stage=? AND sample=? AND status=?',
                             (revision, stage, sample, 'ACCEPTED')).fetchone()
        if not row:
            raise ValueError('same-sample accepted Pregame output missing')
        return dict(row)

    def mark_generated(self, stage, revision, samples, actor):
        expected = 'PREPARED' if stage == 'pregame' else 'PREGAME_FROZEN'
        with self.tx() as db:
            state = db.execute('SELECT * FROM state').fetchone()
            if state['phase'] != expected:
                raise ValueError('illegal phase transition')
            rows = db.execute('SELECT sample FROM attempts WHERE revision=? AND stage=? AND status=?', (revision, stage, 'ACCEPTED')).fetchall()
            if len(samples) != 20 or {r[0] for r in rows} != set(samples):
                raise ValueError('exactly 20 accepted samples required')
            column = 'pre_revision' if stage == 'pregame' else 'post_revision'
            db.execute(f'UPDATE state SET phase=?,{column}=? WHERE id=1', (stage.upper() + '_GENERATED', revision))
            self.audit('generation_completed', actor, {'stage': stage, 'count': 20, 'generation_revision': revision}, db)

    def workspace(self, stage, identity=None):
        with self.tx() as db:
            if identity is not None:
                db.execute('INSERT OR REPLACE INTO workspaces VALUES(?,?)', (stage, json.dumps(identity)))
            row = db.execute('SELECT identity FROM workspaces WHERE stage=?', (stage,)).fetchone()
            return json.loads(row[0]) if row else None

    def start_annotations(self, stage, actor):
        with self.tx() as db:
            phase = db.execute('SELECT phase FROM state').fetchone()[0]
            if phase != stage.upper() + '_GENERATED':
                raise ValueError('cannot annotate before accepted generation')
            db.execute('UPDATE state SET phase=?', (stage.upper() + '_ANNOTATING',))
            self.audit('annotation_started', actor, {'stage': stage, 'workspace': self.workspace_identity(db, stage)}, db)

    @staticmethod
    def workspace_identity(db, stage):
        row = db.execute('SELECT identity FROM workspaces WHERE stage=?', (stage,)).fetchone()
        if not row:
            raise ValueError('workspace identity missing')
        return json.loads(row[0])

    def revise_annotations(self, stage, actor, reason):
        if not reason or not reason.strip():raise ValueError('explicit annotation revision reason required')
        with self.tx() as db:
            phase=db.execute('SELECT phase FROM state').fetchone()[0]
            if phase!=stage.upper()+'_FROZEN':raise ValueError('cannot revise after downstream stage has begun')
            db.execute('UPDATE state SET phase=?',(stage.upper()+'_ANNOTATING',))
            self.audit('override_used',actor,{'operation':'new_annotation_revision','stage':stage,'reason':reason},db)

    def freeze(self, stage, tasks, samples, export_path, actor, new_revision=False):
        """Called only with authenticated server export; pure fixtures in tests."""
        from export_annotations import normalize
        from asset_scanner import read_json, EVAL
        labels, _ = normalize(tasks, read_json(EVAL/'golden_set/v1/selection_manifest.json'), read_json(EVAL/'golden_set/v1/rubric.json'))
        if len(tasks) != 20 or {t['data']['sample_id'] for t in tasks} != set(samples) or len(labels) != 20:
            raise ValueError('20 real completed annotations required')
        if any(t['meta']['stage'] != stage for t in tasks):
            raise ValueError('annotation stage mismatch')
        with self.tx() as db:
            state = db.execute('SELECT * FROM state').fetchone()
            allowed = (stage.upper()+'_ANNOTATING',) + ((stage.upper()+'_FROZEN',) if new_revision else ())
            if state['phase'] not in allowed:
                raise ValueError('illegal freeze phase')
            revision = db.execute('SELECT COALESCE(MAX(revision),0)+1 FROM freezes WHERE stage=?', (stage,)).fetchone()[0]
            receipt = {'golden_set_revision': GOLDEN_ID, 'parent_dataset_revision': DATASET_ID,
                       'generation_revision': state['pre_revision' if stage=='pregame' else 'post_revision'],
                       'annotation_revision': revision, 'annotation_export_hash': canonical_hash(tasks),
                       'annotator_identities': sorted({a['annotator_a']['annotator_id'] for a in labels} |
                           {a['annotator_b']['annotator_id'] for a in labels if 'annotator_b' in a}),
                       'task_count': 20, 'completed_count': 20, 'timestamp': now(),
                       'workspace': self.workspace_identity(db, stage), 'stage': stage}
            exclusive(export_path, {'receipt': receipt, 'raw_export': tasks, 'labels': labels})
            db.execute('INSERT INTO freezes VALUES(?,?,?)', (stage, revision, json.dumps(receipt)))
            db.execute('UPDATE state SET phase=?', (stage.upper()+'_FROZEN',))
            self.audit('annotation_frozen', actor, receipt, db)
            return receipt
