"""Explicitly provisioned standalone service; not imported by app startup.

No auto-install, image build/pull, process spawning, VM start or runtime discovery.
The command-line service is intentionally not run by implementation tests.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import sqlite3
from http.server import BaseHTTPRequestHandler
from socketserver import ThreadingMixIn, UnixStreamServer
from app.execution.docker_supervisor import DockerAPI, DockerRuntime, Supervisor
from app.execution.models import ExecutionPlan
from app.execution.python_models import PythonCall, PythonRuntime, canonical
from app.execution.store import digest as plan_digest
from app.execution.deadlines import DeadlineClient
from app.tasks import timestamp


def approved_job(database, job_id, digest, call):
    # Independent read-only approval/ownership check: supervisor cannot accept a
    # source submission merely because someone can connect to its Unix socket.
    db = sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=1)
    db.row_factory = sqlite3.Row
    try:
        row = db.execute('''SELECT p.owner AS job_owner,p.state,r.*,t.status AS task_status
            FROM python_jobs p JOIN execution_runs r ON r.task_id=p.task_id
            JOIN tasks t ON t.id=p.task_id WHERE p.job_id=?''', (job_id,)).fetchone()
        if (not row or row['state'] != 'submitted' or row['phase'] != 'executing'
                or row['task_status'] != 'running' or row['job_owner'] != row['owner']
                or not row['expires_at'] or row['expires_at'] <= timestamp()
                or not row['approval_required'] or row['plan_digest'] != digest):
            raise ValueError('No current approved sandbox ownership')
        plan = ExecutionPlan.model_validate_json(row['plan_json'])
        if plan.workflow != 'python' or plan.steps[0].tool != call or plan_digest(plan, row['workspace_id']) != digest:
            raise ValueError('Approved source or configuration mismatch')
        step = db.execute('SELECT * FROM execution_steps WHERE task_id=? AND number=1', (row['task_id'],)).fetchone()
        if (not step or step['status'] != 'executing' or step['attempts'] != 1
                or json.loads(step['tool_json']) != call.model_dump() or json.loads(step['dependencies']) != []):
            raise ValueError('Approved step changed')
        if not db.execute("SELECT 1 FROM execution_approvals WHERE task_id=? AND plan_digest=? AND decision='approve'",
                          (row['task_id'], digest)).fetchone():
            raise ValueError('Approval missing')
    finally: db.close()


def handler_for(supervisor):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass  # No source, output, credentials or request data in logs.

        def do_GET(self):
            try:
                if self.path == '/health':
                    supervisor.docker.preflight()
                    with supervisor.db() as db:
                        ready = not db.execute("SELECT 1 FROM jobs WHERE state IN ('running','uncertain')").fetchone()
                    return self.reply(200, {'runtime': supervisor.runtime.model_dump(), 'ready': ready})
                if self.path.startswith('/jobs/'):
                    return self.reply(200, supervisor.receipt(self.path.removeprefix('/jobs/')))
                self.reply(404, {})
            except Exception: self.reply(503, {'error': 'Sandbox unavailable or uncertain'})

        def do_POST(self):
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if self.headers.get('Content-Type') != 'application/json' or not 0 <= size <= 65000:
                    return self.reply(400, {})
                body = json.loads(self.rfile.read(size)) if size else None
                if self.path.startswith('/stop/'):
                    return self.reply(200, {'cleanup_confirmed': supervisor.stop(self.path.removeprefix('/stop/'))})
                if self.path == '/jobs' and isinstance(body, dict) and set(body) == {'job_id', 'plan_digest', 'call'}:
                    result = supervisor.run(body['job_id'], body['plan_digest'], PythonCall.model_validate(body['call']))
                    return self.reply(200, result.model_dump())
                self.reply(400, {})
            except Exception: self.reply(503, {'error': 'Sandbox refused request; never replay source'})

        def reply(self, code, body):
            raw = canonical(body)
            try:
                self.send_response(code); self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw))); self.end_headers(); self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError): pass  # Supervisor keeps its deadline even if client leaves.
    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', required=True, type=Path)
    parser.add_argument('--docker-socket', required=True, type=Path)
    parser.add_argument('--socket', required=True, type=Path)
    parser.add_argument('--ledger', required=True, type=Path)
    parser.add_argument('--backend-database', required=True, type=Path)
    parser.add_argument('--deadline-socket', required=True, type=Path)
    args = parser.parse_args()
    # Provision all parents explicitly. Refuse symlink paths and existing sockets;
    # never unlink arbitrary paths, change system settings or start a VM.
    for path in [args.manifest, args.docker_socket, args.ledger, args.socket, args.backend_database, args.deadline_socket]:
        if not path.is_absolute() or path.is_symlink() or not path.parent.is_dir():
            raise RuntimeError('Use explicitly provisioned absolute paths without symlinks')
    if args.socket.exists(): raise RuntimeError('Supervisor socket already exists; inspect it manually')
    os.umask(0o077)
    lock = open(str(args.ledger) + '.lock', 'a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    runtime = PythonRuntime.model_validate_json(args.manifest.read_text())
    docker = DockerRuntime(runtime, DockerAPI(args.docker_socket), DeadlineClient(args.deadline_socket))
    docker.preflight()
    supervisor = Supervisor(args.ledger, runtime, docker,
                            lambda job, digest, call: approved_job(args.backend_database, job, digest, call))
    class Server(ThreadingMixIn, UnixStreamServer):
        daemon_threads = False
    with Server(str(args.socket), handler_for(supervisor)) as server:
        os.chmod(args.socket, 0o600)
        server.serve_forever()


if __name__ == '__main__': main()
