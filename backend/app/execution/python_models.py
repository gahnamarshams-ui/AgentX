"""Data only: never import or evaluate candidate source in the backend."""
import hashlib
import json
from datetime import datetime
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode()


def fingerprint(value) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def strict_json(value):
    def pairs(items):
        result = {}
        for key, item in items:
            if key in result: raise ValueError('Duplicate JSON key')
            result[key] = item
        return result
    def invalid(value): raise ValueError('Non-finite JSON number')
    return json.loads(value, object_pairs_hook=pairs, parse_constant=invalid)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class PythonCase(StrictModel):
    args: list[JsonValue] = Field(max_length=20)
    kwargs: dict[str, JsonValue] = Field(max_length=20)
    expected: JsonValue


class PythonRuntime(StrictModel):
    policy: Literal['agentx-python-v1']
    deadline_policy: Literal['native-runtime-lease-v1'] = 'native-runtime-lease-v1'
    identity: str = Field(min_length=1, max_length=200)
    image: str = Field(pattern=r'^sha256:[a-f0-9]{64}$')
    bootstrap_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    seccomp_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    network: Literal['none']
    host_mounts: Literal[False]
    inherit_environment: Literal[False]
    user: Literal['65532:65532']
    read_only: Literal[True]
    cpus: Literal[1]
    memory_bytes: Literal[268435456]
    memory_swap_bytes: Literal[268435456]
    pids: Literal[16]
    tmpfs_bytes: Literal[16777216]
    output_bytes: Literal[65536]
    case_timeout_seconds: Literal[5]
    job_timeout_seconds: Literal[30]


class PythonCall(StrictModel):
    name: Literal['run_python_function']
    source: str = Field(min_length=1, max_length=8000)
    function: str = Field(pattern=r'^[A-Za-z][A-Za-z0-9_]{0,79}$')
    cases: list[PythonCase] = Field(min_length=1, max_length=10)
    runtime: PythonRuntime
    source_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    tests_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')

    @model_validator(mode='after')
    def exact_bytes(self):
        if (hashlib.sha256(self.source.encode()).hexdigest() != self.source_sha256
                or fingerprint([c.model_dump() for c in self.cases]) != self.tests_sha256
                or len(canonical(self.model_dump())) > 60000 or '\x00' in self.source):
            raise ValueError('Source/tests must match their approved hashes and size limits')
        return self


class CaseEvidence(StrictModel):
    container_id: str = Field(pattern=r'^[a-f0-9]{64}$')
    index: int = Field(ge=0, le=9)
    exit_code: int | None
    stdout: str = Field(max_length=65536)
    stderr: str = Field(max_length=65536)
    timed_out: bool
    oom: bool
    overflow: bool
    duration_ms: int = Field(ge=0, le=60000)
    inspected_config_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    stopped: bool
    removed: bool
    deadline_receipt: dict[str, JsonValue] | None = None


class PythonEvidence(StrictModel):
    kind: Literal['python'] = 'python'
    job_id: str = Field(pattern=r'^[a-f0-9]{32}$')
    plan_digest: str = Field(pattern=r'^[a-f0-9]{64}$')
    source_sha256: str
    tests_sha256: str
    runtime: PythonRuntime
    started_at: str
    finished_at: str
    cases: list[CaseEvidence] = Field(max_length=10)
    cleanup_confirmed: bool
    error: str | None = Field(max_length=500)
    checks: list[str] = Field(default_factory=list)


def verify_python(call: PythonCall, evidence: PythonEvidence, trusted: PythonEvidence,
                  job_id: str, digest: str, config_hash: str, deadlines=None) -> tuple[bool, list[str]]:
    """Compare an independently fetched supervisor receipt, then actual JSON results.

    stdout is untrusted; a printed PASS or candidate-generated assertion is never evidence.
    This establishes only the approved observable cases, not arbitrary semantic correctness.
    """
    try:
        PythonCall.model_validate(call.model_dump())
        PythonEvidence.model_validate(evidence.model_dump())
        if (canonical(evidence.model_dump()) != canonical(trusted.model_dump())
                or evidence.job_id != job_id or evidence.plan_digest != digest
                or evidence.runtime != call.runtime or evidence.source_sha256 != call.source_sha256
                or evidence.tests_sha256 != call.tests_sha256 or evidence.error
                or not evidence.cleanup_confirmed or len(evidence.cases) != len(call.cases)
                or len({c.container_id for c in evidence.cases}) != len(call.cases)):
            return False, ['sandbox_receipt_mismatch_or_incomplete']
        total = 0
        output_total = 0
        if deadlines is None or len(deadlines) != len(call.cases):
            return False, ['independent_native_cutoff_receipts_required']
        if (len({r['lease_id'] for r in deadlines}) != len(deadlines)
                or len({r['boot_id'] for r in deadlines}) != 1
                or len({r['job_expires_at'] for r in deadlines}) != 1
                or deadlines[0]['job_expires_at'] > deadlines[0]['armed_at'] + call.runtime.job_timeout_seconds
                or [r['armed_at'] for r in deadlines] != sorted(r['armed_at'] for r in deadlines)):
            return False, ['native_job_deadline_mismatch']
        elapsed = (datetime.fromisoformat(evidence.finished_at) - datetime.fromisoformat(evidence.started_at)).total_seconds()
        if not 0 <= elapsed <= call.runtime.job_timeout_seconds:
            return False, ['sandbox_deadline_exceeded']
        for index, (case, actual) in enumerate(zip(call.cases, evidence.cases)):
            from app.execution.deadlines import LeaseTarget, checked_receipt
            target = LeaseTarget(job_id=job_id, plan_digest=digest, container_id=actual.container_id,
                runtime_identity=call.runtime.identity, config_sha256=config_hash, case_index=index)
            if (canonical(actual.deadline_receipt) != canonical(deadlines[index])
                    or checked_receipt(deadlines[index], target, 'terminated', started=True) != deadlines[index]):
                return False, ['native_cutoff_receipt_mismatch']
            if deadlines[index].get('deadline_met') is not True or deadlines[index].get('timing') is None:
                return False, ['native_cessation_before_deadline_unproven']
            total += actual.duration_ms
            output_total += len(actual.stdout.encode()) + len(actual.stderr.encode())
            if (actual.index != index or actual.exit_code != 0 or actual.timed_out or actual.oom
                    or actual.overflow or not actual.stopped or not actual.removed
                    or actual.duration_ms > call.runtime.case_timeout_seconds * 1000
                    or actual.inspected_config_sha256 != config_hash
                    or len(actual.stdout.encode()) + len(actual.stderr.encode()) > call.runtime.output_bytes):
                return False, ['sandbox_case_failed']
            parsed = strict_json(actual.stdout)
            if (not isinstance(parsed, dict) or set(parsed) != {'value'}
                    or canonical(parsed['value']) != canonical(case.expected)):
                return False, ['approved_test_failed']
        if total > call.runtime.job_timeout_seconds * 1000 or output_total > call.runtime.output_bytes:
            return False, ['sandbox_deadline_exceeded']
    except (ValueError, TypeError, RecursionError, KeyError):
        return False, ['invalid_sandbox_evidence']
    return True, ['trusted_supervisor_receipt_matches', 'approved_json_cases_match',
                  'runtime_and_source_hashes_match', 'cleanup_confirmed', 'finite_tests_only']
