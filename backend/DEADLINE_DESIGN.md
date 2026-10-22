# Crash-independent deadlines: safety-only implementation

Live execution is still unconditionally blocked. Both execution feature flags
remain false by default. No runtime, service, image or timer was provisioned,
and no generated Python was executed. Tests use a simulated native backend.

## First remediation phase: journal fencing and evidence

Both `arm()` and `start()` require this instance to have reconciled. A private
journal authority row records a durable owner, generation and readiness barrier.
Reconciliation advances the generation before cleanup, invalidating all older
workers. No Python lock is relied on for cross-process ownership. Each mutation
claims a fresh operation token; arm/start/cleanup acknowledgements commit only
if the lease state, operation token and authority generation still match. Native
registration/start acknowledgements also bind a single immutable attempt ID.
Cancellation is persisted before native I/O. A late success or failure cannot
resurrect a cancelled lease or overwrite replacement recovery evidence.

The private deadline journal has an additive migration: generation, operation,
attempt, registration timestamp and timing-observation columns, plus the authority
barrier table. This is necessary to fence independent SQLite connections and
preserve intent across restarts. Existing rows, container IDs, unique constraints
and tombstones are retained; missing old evidence remains unknown. Application
database schemas and file-plan digest serialization are unchanged in this phase.

Version-2 receipts distinguish registration confirmation, optional cutoff request,
native-observed workload cessation, and termination confirmation. Observations
bind the job, digest, runtime, configuration, case, lease, attempt and boot domain.
`deadline_met` is unknown when cessation time is absent, false when cessation is
late, and true only when a matching trusted native observation places cessation
by the recorded deadline. A late confirmation alone establishes neither early
nor late cessation. Verifier rejects legacy, missing, inconsistent, incorrectly
bound or late timing evidence for a new Python success; historical records remain
readable. This is validation of a future trusted provider's assertions, not proof
that such a provider exists or that its clock/observations are trustworthy.

Deterministic tests use independent authority objects/connections and fake native
callbacks to interleave cancellation, expiry, duplicate starts, recovery and late
acknowledgements. They do not demonstrate real multi-process or native enforcement
safety. A future native provider must still fence a queued START against expiry
and cancellation even after the Python request has been issued. No SQLite change
can supply that runtime guarantee. Live preflight remains unconditionally blocked.

## Mechanism and required trusted component

The Python supervisor must not own a workload's final deadline. A separate runtime
lease authority must require a durable **platform-owned cutoff** before starting
an exact container. Its native backend must remain effective if both the Python
supervisor and the authority process crash, are killed or stop making progress.
A thread, Python Timer, supervisor heartbeat or relaunch-on-crash policy is not
sufficient by itself. This repository does not contain a verified native backend
for Docker Desktop's macOS VM. The default driver refuses every operation.

The required extra component is a separately reviewed runtime/VM-native deadline
broker: persistent cutoff registration, a suspend-aware monotonic clock and boot
identity, atomic lease-fenced start, exact-CID termination and durable cleanup
receipts. A platform service manager or hypervisor may provide the enforcement,
but no specific provider is assumed to exist or to be trustworthy here. If Docker
cannot terminate a target while its daemon is unreachable, the native component
needs a separately reviewed isolated-VM stop/destruction escalation. Without that
capability, a hard termination guarantee cannot be claimed. Never use a shared
personal-workload VM for such escalation.

## Registration, start and cancellation

`execution/deadlines.py` implements the data/state boundary. A lease binds the
job ID, approved plan digest, immutable container ID, runtime identity, inspected
configuration hash and case index. No source or expected output is sent to it.
The case deadline is at most five seconds and never exceeds the original
thirty-second job deadline. The native backend's clock supplies these values;
the Python supervisor cannot choose, refresh or extend them.

1. The separately operated authority must reconcile its journal and owned runtime
   inventory before accepting work. Unavailable enforcement means no start.
2. Container creation produces only a stopped object. Before start, lease intent
   is committed to the authority journal, then the native cutoff is armed and
   its exact binding acknowledged. A crash between these commits leaves a
   cleanup-only record.
3. The authority independently checks current approval/ownership and actual
   container configuration. It atomically consumes the armed lease before
   issuing native start. The native backend must fence queued starts against
   expiry, cancellation, boot changes and previously consumed tickets. An
   acknowledgement lost after start is uncertain, never permission to retry.
4. The Docker adapter no longer issues Docker START itself or uses a local Timer
   as its deadline. Its local receive deadline is only a responsiveness check;
   platform enforcement must not depend on the receive loop or `tick()`.
5. Completion/cancellation terminates the exact CID and independently confirms
   absence **before** disarming its native cutoff. Unconfirmed termination or
   timer cleanup quarantines new work. Job-specific stop never kills another job.

The native component must also make cleanup idempotent and reject a delayed
START after a cancellation tombstone. Container names are never identities.
Restart policies remain disabled; neither source nor a container is restarted.

## Crash windows and recovery

| Failure | Required behavior |
| --- | --- |
| Create completed, registration absent | Reconcile this authority's managed inventory; remove orphan CID without starting it. |
| Lease journal written, native registration acknowledgement lost | Quarantine; terminate exact CID and reconcile native tickets. Never rearm to extend time. |
| Supervisor/backend dies after start | Native cutoff still expires; no Python callback is required. |
| Authority process dies | Platform cutoff persists. On authority restart, terminate/reconcile journaled work, never resume it. |
| Boot changes or clock rolls backward | Reject start; clean recorded CIDs. Never reinterpret an old monotonic deadline in a new boot. |
| Start acknowledgement lost | Termination/absence reconciliation only; no start replay. |
| Docker unavailable, cleanup unconfirmed | Persist quarantine. Native enforcement/escalation remains responsible for termination; no new work or success claim. |

Backend recovery continues to stop recorded jobs through the supervisor rather
than resubmit source. Supervisor cleanup now also requires native job cleanup;
an empty Docker list alone cannot prove the native timer state is safe. Receipts
record lease/boot/timing/start/termination bindings. Verifier retrieves them from
the separate authority channel and compares them with the supervisor's evidence,
approved digest and exact container/configuration. Printed PASS is irrelevant.
The deadline policy is included in Python plan serialization; old Python plans
need fresh approval. File-plan digest serialization is unchanged.

## Trust and residual failure modes

Trusted: macOS/hypervisor, native VM clock and timer manager, exact-container
control/VM escalation, pinned Docker/runtime/image, authority journal and
approval callback, protected Unix channels, and the backend's saved approval.
Candidate code and its stdout/stderr are untrusted. The authority must run
separately and receive no candidate code. Its native driver and approval callback
must be wired by reviewed service code; there is no operational authority service
or native provider supplied/enabled in this phase.

Host compromise, administrator actions, hypervisor/kernel faults, hardware power
loss, timer-manager failure, disk corruption and unavailable container/VM control
remain residual risks. VM/host suspension and clock behavior require explicit
validation. Stop confirmation can arrive later than the enforcement deadline;
until then the state is uncertain. Mock acknowledgements do not prove isolation
or a real-time guarantee. Backend, supervisor and authority journals cannot
commit atomically; intent-first records, persistent native tickets, reconciliation
and no-replay semantics are required across all boundaries.

## Review and separate live-test boundary

Only policy/transport boundaries and mock state-machine tests are implemented.
`DockerRuntime.preflight()` still unconditionally refuses live activation.
No flag, socket setting or manifest assertion removes this block.

Separate review/authorization is required for selection and implementation of the
native enforcement provider, protected service/approval-callback wiring, VM/image
provisioning, and each exact live test. Those tests must include supervisor and
authority SIGKILL, lost acknowledgements, daemon outages, suspend/resume and VM
restart, expiry without Python ticks, mismatched CIDs, orphan reconciliation,
concurrent starts and failed termination/timer cleanup. No live commands are
provided as part of this safety-only phase.
