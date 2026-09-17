"""SQLite storage owned by application workflows, never imported by algorithms."""
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

KINDS = {'vod_asr', 'vod_slides', 'live', 'sync', 'assignments'}


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.connect() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS entities (collection TEXT, key TEXT, body TEXT NOT NULL,
                    PRIMARY KEY(collection,key));
                CREATE TABLE IF NOT EXISTS executions (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, course_id TEXT NOT NULL,
                    lecture_id TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                    due REAL NOT NULL DEFAULT 0, error TEXT, artifact TEXT, options TEXT NOT NULL DEFAULT '{}',
                    created REAL NOT NULL, updated REAL NOT NULL,
                    UNIQUE(kind,course_id,lecture_id));
            ''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def put(self, collection, key, body):
        with self.connect() as db:
            db.execute('INSERT INTO entities VALUES (?,?,?) ON CONFLICT(collection,key) DO UPDATE SET body=excluded.body',
                       (collection, str(key), json.dumps(body, ensure_ascii=False)))

    def get(self, collection, key, default=None):
        with self.connect() as db:
            row = db.execute('SELECT body FROM entities WHERE collection=? AND key=?', (collection, str(key))).fetchone()
        return json.loads(row[0]) if row else default

    def list(self, collection):
        with self.connect() as db:
            return [json.loads(row[0]) for row in db.execute('SELECT body FROM entities WHERE collection=? ORDER BY key', (collection,))]

    def enqueue(self, kind, course_id, lecture_id='', *, options=None, force=False):
        if kind not in KINDS:
            raise ValueError('Unsupported execution kind')
        now = time.time()
        key = (kind, str(course_id), str(lecture_id))
        with self.connect() as db:
            db.execute('INSERT OR IGNORE INTO executions(id,kind,course_id,lecture_id,status,options,created,updated) VALUES (?,?,?,?,?,?,?,?)',
                       (uuid.uuid4().hex, *key, 'pending', json.dumps(options or {}), now, now))
            if not (options or {}).get('automatic', False):
                # An explicit request may promote pending automatic work while paused.
                db.execute("UPDATE executions SET options=?, due=0 WHERE kind=? AND course_id=? AND lecture_id=? AND status='pending'", (json.dumps(options or {}), *key))
            if force:
                db.execute("UPDATE executions SET status='pending', attempts=0, due=0, error=NULL, options=?, updated=? WHERE kind=? AND course_id=? AND lecture_id=? AND status IN ('failed','cancelled','succeeded','needs_login')",
                           (json.dumps(options or {}), now, *key))
            return db.execute('SELECT id FROM executions WHERE kind=? AND course_id=? AND lecture_id=?', key).fetchone()[0]

    def claim(self, kind, *, run_id=None, allow_automatic=True):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            sql = "SELECT * FROM executions WHERE kind=? AND status='pending' AND due<=?"
            params = [kind, time.time()]
            if run_id:
                sql += ' AND id=?'
                params.append(run_id)
            if not allow_automatic:
                sql += " AND COALESCE(json_extract(options, '$.automatic'),0)=0"
            row = db.execute(sql+' ORDER BY created LIMIT 1', params).fetchone()
            if not row:
                return None
            db.execute("UPDATE executions SET status='running', attempts=attempts+1, updated=? WHERE id=?", (time.time(), row['id']))
            result = dict(row)
            result['attempts'] += 1
            result['options'] = json.loads(result['options'])
            return result

    def finish(self, run_id, status, *, error=None, artifact=None, delay=0):
        with self.connect() as db:
            db.execute("UPDATE executions SET status=?, error=?, artifact=COALESCE(?,artifact), due=?, updated=? WHERE id=? AND status!='cancelled'",
                       (status, error, str(artifact) if artifact else None, time.time()+delay, time.time(), run_id))

    def defer(self, run_id, delay=60):
        with self.connect() as db:
            db.execute("UPDATE executions SET status='pending', attempts=MAX(0,attempts-1), due=? WHERE id=? AND status='running'", (time.time()+delay, run_id))

    def cancel(self, run_id):
        with self.connect() as db:
            cursor = db.execute("UPDATE executions SET status='cancelled', updated=? WHERE id=? AND status IN ('pending','running','needs_login')", (time.time(), run_id))
            return bool(cursor.rowcount)

    def recover(self):
        with self.connect() as db:
            db.execute("UPDATE executions SET status='pending', due=0 WHERE status='running'")

    def retry(self, run_id):
        with self.connect() as db:
            return bool(db.execute("UPDATE executions SET status='pending', attempts=0, due=0, error=NULL WHERE id=? AND status IN ('failed','cancelled','needs_login')", (run_id,)).rowcount)

    def executions(self):
        with self.connect() as db:
            return [{**dict(r), 'options': json.loads(r['options'])} for r in db.execute('SELECT * FROM executions ORDER BY created DESC')]

    def execution(self, run_id):
        with self.connect() as db:
            row = db.execute('SELECT * FROM executions WHERE id=?', (run_id,)).fetchone()
            return {**dict(row), 'options': json.loads(row['options'])} if row else None
