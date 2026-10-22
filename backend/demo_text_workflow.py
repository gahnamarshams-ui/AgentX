"""Repeatable LIVE text-only demo. Creates three tasks; never approves or executes tools.

Run from backend: .venv/bin/python demo_text_workflow.py
Requires the isolated demo server and database described in DEMO_READINESS.md.
"""
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from urllib.request import Request, urlopen

BASE = 'http://127.0.0.1:8002'
ROOT = Path(__file__).resolve().parent
ROLES = ['Orchestrator', 'Planner', 'Safety', 'Executor', 'Verifier']
SCENARIOS = [
    ('A', 'Calculate 125 + 275. Reply with only the numeric answer.'),
    ('B', 'Show a Python function add(a, b) that returns their sum. Show code only; do not execute it.'),
    ('C', 'Inspect my local workspace files and report what they contain. If you cannot access them, explain that limitation without inventing file contents.'),
]


def request(path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    with urlopen(Request(BASE + path, data=data, headers={'Content-Type': 'application/json'}), timeout=135) as response:
        return json.load(response)


def main():
    assert request('/api/v1/health')['status'] == 'ok'
    report = {'backend': BASE, 'mode': 'live OpenAI text only; no tool execution', 'scenarios': []}
    for label, prompt in SCENARIOS:
        task = request('/api/tasks', {'description': prompt})
        path = '/api/tasks/' + task['id']
        result = request(path + '/respond', {})
        saved, activity, history = request(path), request(path + '/activity'), request(path + '/history')
        assert saved == result and saved['status'] == 'completed' and saved['response_mode'] == 'chat'
        assert [a['role'] for a in activity['agents']] == ROLES
        assert all(a['status'] == 'succeeded' for a in activity['agents'])
        assert activity['plan_digest'] is None
        digest = hashlib.sha256(saved['final_result'].encode()).hexdigest()
        assert 'answer_sha256:' + digest in activity['agents'][-1]['checks']
        assert 'factual_correctness_not_verified' in activity['agents'][-1]['checks']
        verification = activity['agents'][-1]['text_verification']
        assert verification['code_executed'] is False and verification['factual_correctness_verified'] is False
        assert verification['answer_sha256'] == digest
        checks = {c['name']: c['status'] for c in verification['checks']}
        assert checks['semantic_correctness'] == checks['factual_correctness'] == 'not_checked'
        assert checks['python_syntax'] == ('passed' if label == 'B' else 'not_checked')
        if label == 'B': assert checks['requested_function'] == 'passed'
        messages = [e['message'] for e in history['events']]
        assert len(messages) == len(set(messages))
        for role in ROLES:
            assert sum(m.startswith(f'[{role}] running:') for m in messages) == 1
            assert sum(m.startswith(f'[{role}] succeeded:') for m in messages) == 1
        if label == 'A':
            assert saved['final_result'].strip() == str(125 + 275)
        elif label == 'B':
            assert re.search(r'def add\(a, b\)', saved['final_result'])
            assert 'return a + b' in saved['final_result']
        # Scenario C needs human review of the actual answer; no keyword heuristic proves honesty.
        with sqlite3.connect(f'file:{ROOT / "data/hackathon-demo.sqlite3"}?mode=ro', uri=True) as db:
            for table in ['execution_runs', 'execution_steps', 'execution_approvals', 'python_jobs']:
                assert db.execute(f'SELECT count(*) FROM {table} WHERE task_id=?', (task['id'],)).fetchone()[0] == 0
            row = db.execute('SELECT plan_json,agents_json FROM chat_runs WHERE task_id=?', (task['id'],)).fetchone()
            assert row and json.loads(row[0]) and json.loads(row[1]) == activity['agents']
        report['scenarios'].append({'scenario': label, 'task': saved, 'internal_plan': json.loads(row[0]), 'activity': activity, 'history': history})
        print(f"{label}: {task['id']} — persisted five roles; zero execution/approval/job records", flush=True)
    (ROOT / 'data/hackathon-demo-evidence.json').write_text(json.dumps(report, indent=2) + '\n')
    print('Saved data/hackathon-demo-evidence.json. Review Scenario C answer; no code was executed.')


if __name__ == '__main__':
    main()
