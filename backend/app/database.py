import sqlite3
import re
from contextlib import contextmanager
from pathlib import Path
from collections.abc import Iterator


@contextmanager
def connection(path: Path) -> Iterator[sqlite3.Connection]:
    db = sqlite3.connect(path, timeout=5)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    try:
        with db:
            yield db
    finally:
        db.close()


def initialize_database(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with connection(path) as db:
        db.execute("PRAGMA journal_mode = WAL")
        # SQLite requires table rebuilds to widen CHECK constraints. Keep foreign
        # key names intact and validate them before committing the migration.
        db.execute('PRAGMA foreign_keys = OFF')
        # Serialize migrations, and roll back DDL and version writes together.
        db.execute("BEGIN IMMEDIATE")
        db.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)")
        db.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (1)")

        if db.execute("SELECT 1 FROM schema_version WHERE version = 2").fetchone() is None:
            db.execute("""CREATE TABLE tasks (
                id TEXT PRIMARY KEY,
                description TEXT NOT NULL CHECK(length(trim(description)) BETWEEN 1 AND 5000),
                status TEXT NOT NULL CHECK(status IN ('pending','planning','awaiting_approval','running','completed','failed','cancelled')),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                plan TEXT CHECK(plan IS NULL OR (json_valid(plan) AND json_type(plan) = 'object')),
                final_result TEXT,
                error TEXT
            )""")
            db.execute("""CREATE TABLE task_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL REFERENCES tasks(id),
                event_type TEXT NOT NULL CHECK(event_type IN ('task_created','status_changed')),
                status TEXT NOT NULL CHECK(status IN ('pending','planning','awaiting_approval','running','completed','failed','cancelled')),
                message TEXT NOT NULL,
                created_at TEXT NOT NULL
            )""")
            db.execute("CREATE INDEX tasks_recent ON tasks(created_at DESC, id DESC)")
            db.execute("CREATE INDEX history_task_order ON task_history(task_id, created_at, id)")
            db.execute("INSERT INTO schema_version (version) VALUES (2)")

        if db.execute("SELECT 1 FROM schema_version WHERE version = 3").fetchone() is None:
            db.execute("""CREATE TABLE planning_attempts (
                task_id TEXT PRIMARY KEY REFERENCES tasks(id),
                token TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                outcome TEXT NOT NULL CHECK(outcome IN ('active','failed','succeeded'))
            )""")
            db.execute("CREATE INDEX planning_expiry ON planning_attempts(outcome, expires_at)")
            db.execute("INSERT INTO schema_version VALUES (3)")

        if db.execute("SELECT 1 FROM schema_version WHERE version = 4").fetchone() is None:
            db.execute("""CREATE TABLE execution_runs (
                task_id TEXT PRIMARY KEY REFERENCES tasks(id),
                id TEXT NOT NULL UNIQUE,
                phase TEXT NOT NULL CHECK(phase IN ('planning','awaiting_approval','executing','verifying','completed','failed','cancelled')),
                plan_json TEXT CHECK(plan_json IS NULL OR json_valid(plan_json)),
                plan_digest TEXT,
                approval_required INTEGER NOT NULL DEFAULT 0 CHECK(approval_required IN (0,1)),
                workspace_id TEXT NOT NULL,
                owner TEXT NOT NULL,
                expires_at TEXT,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )""")
            db.execute("""CREATE TABLE execution_steps (
                task_id TEXT NOT NULL REFERENCES execution_runs(task_id),
                number INTEGER NOT NULL,
                dependencies TEXT NOT NULL CHECK(json_valid(dependencies)),
                tool_json TEXT NOT NULL CHECK(json_valid(tool_json)),
                status TEXT NOT NULL CHECK(status IN ('pending','executing','executed','verified','failed')),
                attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts BETWEEN 0 AND 3),
                result TEXT CHECK(result IS NULL OR json_valid(result)),
                verification TEXT CHECK(verification IS NULL OR json_valid(verification)),
                error TEXT,
                PRIMARY KEY(task_id, number)
            )""")
            db.execute("""CREATE TABLE execution_approvals (
                task_id TEXT PRIMARY KEY REFERENCES execution_runs(task_id),
                plan_digest TEXT NOT NULL,
                decision TEXT NOT NULL CHECK(decision IN ('approve','reject')),
                created_at TEXT NOT NULL
            )""")
            db.execute("CREATE INDEX execution_expiry ON execution_runs(phase, expires_at)")
            db.execute("INSERT INTO schema_version VALUES (4)")

        if db.execute("SELECT 1 FROM schema_version WHERE version = 5").fetchone() is None:
            db.execute("ALTER TABLE tasks ADD COLUMN title TEXT CHECK(title IS NULL OR length(trim(title)) BETWEEN 1 AND 120)")
            db.execute("ALTER TABLE tasks ADD COLUMN archived INTEGER NOT NULL DEFAULT 0 CHECK(archived IN (0,1))")
            db.execute("INSERT INTO schema_version VALUES (5)")

        if db.execute("SELECT 1 FROM schema_version WHERE version=6").fetchone() is None:
            db.execute("CREATE TABLE chat_runs(task_id TEXT PRIMARY KEY REFERENCES tasks(id), token TEXT NOT NULL, plan_json TEXT, agents_json TEXT NOT NULL CHECK(json_valid(agents_json)))")
            db.execute("INSERT INTO schema_version VALUES(6)")

        if db.execute('SELECT 1 FROM schema_version WHERE version=7').fetchone() is None:
            db.execute('''CREATE TABLE python_jobs (
                task_id TEXT PRIMARY KEY REFERENCES execution_runs(task_id),
                job_id TEXT NOT NULL UNIQUE, owner TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('claimed','submitted','verifying','completed','failed','uncertain')),
                evidence TEXT CHECK(evidence IS NULL OR json_valid(evidence)),
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL)''')
            db.execute('INSERT INTO schema_version VALUES(7)')

        if db.execute('SELECT 1 FROM schema_version WHERE version=8').fetchone() is None:
            # Existing tasks remain independent conversation roots.
            db.execute('ALTER TABLE tasks ADD COLUMN conversation_id TEXT REFERENCES tasks(id)')
            db.execute('CREATE INDEX conversation_turns ON tasks(conversation_id,created_at,id)')
            db.execute('INSERT INTO schema_version VALUES(8)')

        if db.execute('SELECT 1 FROM schema_version WHERE version=9').fetchone() is None:
            for table in ('tasks', 'task_history', 'execution_runs'):
                sql = db.execute('SELECT sql FROM sqlite_master WHERE type=? AND name=?', ('table', table)).fetchone()[0]
                indexes = [r[0] for r in db.execute('SELECT sql FROM sqlite_master WHERE type=? AND tbl_name=? AND sql IS NOT NULL', ('index', table))]
                sql = re.sub(r'CREATE TABLE\s+"?' + table + r'"?', 'CREATE TABLE ' + table + '_new', sql, count=1)
                sql = sql.replace("'awaiting_approval'", "'awaiting_user_input','awaiting_approval'", 1)
                db.execute(sql)
                db.execute(f'INSERT INTO {table}_new SELECT * FROM {table}')
                db.execute(f'DROP TABLE {table}')
                db.execute(f'ALTER TABLE {table}_new RENAME TO {table}')
                for index in indexes: db.execute(index)
            db.execute('''CREATE TABLE execution_clarifications (
                task_id TEXT NOT NULL REFERENCES tasks(id),
                answer TEXT NOT NULL, created_at TEXT NOT NULL)''')
            pending = db.execute('''SELECT t.id FROM tasks t LEFT JOIN chat_runs c ON c.task_id=t.id
                WHERE t.status IN ('completed','awaiting_approval') AND
                (json_extract(t.plan,'$.clarification_needed')=1 OR json_extract(c.plan_json,'$.clarification_needed')=1)''').fetchall()
            for row in pending:
                db.execute("UPDATE tasks SET status='awaiting_user_input' WHERE id=?", (row['id'],))
                db.execute("""INSERT INTO task_history(task_id,event_type,status,message,created_at)
                    VALUES (?,'status_changed','awaiting_user_input','Clarification is still required. No execution authorized.',strftime('%Y-%m-%dT%H:%M:%f+00:00','now'))""", (row['id'],))
            if db.execute('PRAGMA foreign_key_check').fetchone():
                raise sqlite3.IntegrityError('Migration found invalid references')
            db.execute('INSERT INTO schema_version VALUES(9)')


        if db.execute('SELECT 1 FROM schema_version WHERE version=10').fetchone() is None:
            for table in ('tasks', 'task_history'):
                sql = db.execute('SELECT sql FROM sqlite_master WHERE type=? AND name=?', ('table', table)).fetchone()[0]
                indexes = [r[0] for r in db.execute('SELECT sql FROM sqlite_master WHERE type=? AND tbl_name=? AND sql IS NOT NULL', ('index', table))]
                sql = re.sub(r'CREATE TABLE\s+"?' + table + r'"?', 'CREATE TABLE ' + table + '_new', sql, count=1)
                sql = sql.replace("'cancelled'", "'cancelled','blocked'", 1)
                db.execute(sql)
                db.execute(f'INSERT INTO {table}_new SELECT * FROM {table}')
                db.execute(f'DROP TABLE {table}')
                db.execute(f'ALTER TABLE {table}_new RENAME TO {table}')
                for index in indexes: db.execute(index)
            db.execute('ALTER TABLE tasks ADD COLUMN execution_required INTEGER NOT NULL DEFAULT 0 CHECK(execution_required IN (0,1))')
            db.execute('CREATE TABLE chat_clarifications(task_id TEXT NOT NULL REFERENCES tasks(id), answer TEXT NOT NULL, created_at TEXT NOT NULL)')
            from app.task_intent import requires_execution
            for row in db.execute('SELECT id,description FROM tasks').fetchall():
                if requires_execution(row['description']):
                    db.execute('UPDATE tasks SET execution_required=1 WHERE id=? OR conversation_id=?', (row['id'], row['id']))
            if db.execute('PRAGMA foreign_key_check').fetchone():
                raise sqlite3.IntegrityError('Migration found invalid references')
            db.execute('INSERT INTO schema_version VALUES(10)')


def check_database(path: Path) -> None:
    with connection(path) as db:
        row = db.execute("SELECT version FROM schema_version WHERE version = 3").fetchone()
        db.execute("SELECT id FROM tasks LIMIT 1")
        db.execute("SELECT id FROM task_history LIMIT 1")
        if row is None:
            raise sqlite3.DatabaseError("Database schema is not initialized")
