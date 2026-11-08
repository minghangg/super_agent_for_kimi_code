[English](05-artifact-recycle-bin.en.md) | [中文](05-artifact-recycle-bin.zh.md)

# 05 · Artifact recycle bin and metadata retention

> Series: design philosophy ｜ [01 Core architecture](01-core-architecture.en.md) ｜ [02 Requirements Analyst](02-requirements-analyst.en.md) ｜ [03 Agent-to-agent communication](03-agent-communication.en.md) ｜ [04 Human-facing Reporter](04-human-reporter.en.md) ｜ 05 Artifact recycle bin
> Version: 2026-11-07
> Goal: intermediate artifacts are usable while work is running, reclaimable for a bounded period after close-out, and traceable throughout — without a heavy database.
> Nature: a design proposal. It changes no rules, runs no cleanup and authorizes no automatic deletion.

## 1. Correct one assumption first

**A recycle bin solves storage growth, process traceability and delayed deletion. It does not, by itself, stop a worker from damaging production.**

Production safety remains the job of:

- project permissions and command allowlists;
- working-directory isolation;
- authorization for production writes;
- separation between test targets and real environments;
- operation auditing and a stop mechanism.

Keeping a temporary script only helps investigate afterwards. It does not prevent the change the script already made. The design must therefore keep **artifact retention** and **production protection** apart.

## 2. Classify first; do not sweep everything into the bin

| Category | Examples | Destination |
|---|---|---|
| Formal requirements and conclusions | briefs, worker deliveries, QA conclusions, user decisions | full text into Issue comments |
| Formal project assets | tests, fixtures, project scripts, build config | stay in the project repo, committed normally |
| Temporary working artifacts | exploration notes, throwaway scripts, intermediate output, diagnostic logs | used during work, then into the recycle bin |
| Screenshots and binary evidence | screenshots, exports, test attachments | recycle bin; the Issue keeps only a reference |
| Secrets and sensitive data | tokens, credentials, production personal data | forbidden; on discovery, alert and handle separately |
| Long-lived run state | sessions, cursors, outbox, health | existing run state; not reclaimed as temporary files |

The core rule:

> **Formal deliveries go to Issues, project assets go to the repository, and only time-bounded temporary artifacts go to the recycle bin.**

## 3. Do not encode all metadata in file names

File names are good for quick human recognition and bad for carrying metadata:

- length is limited, and Windows path limits bite sooner;
- CJK text, spaces and reserved characters need escaping;
- changing metadata means renaming, which breaks references;
- project, Issue and owner fields repeat or change;
- a file name cannot reliably express a hash, an expiry policy or a retention pin.

Recommended instead:

```text
stable file name + a sidecar JSON per artifact + a lightweight index
```

The file name keeps only:

```text
<artifact_id>__<safe_slug>.<ext>
```

For example:

```text
art_01KABC...__login-repro.py
art_01KABD...__qa-result.log
```

Metadata lives in a sibling sidecar:

```text
art_01KABC...__login-repro.py
art_01KABC...__login-repro.json
```

## 4. Directory layout

One directory per project — but do not organise by project alone, or close-out will still require scanning many files to find the matching Issue.

```text
recycle-bin/
├── config.json
├── index.json
├── events.jsonl
└── projects/
    └── repo-a/
        └── ISS-123/
            ├── art_01__diagnose.py
            ├── art_01__diagnose.json
            ├── art_02__test-output.log
            └── art_02__test-output.json
```

For high-volume or long-running projects, add a date level:

```text
projects/repo-a/2026-11/ISS-123/
```

`artifact_id` is the permanent internal reference. Moving a directory or renaming a file does not break references from Issues, Assignments or the Reporter.

### Where it lives

- Under the run-state/evidence root the workbench defines, never inside a project's git working tree.
- Follow `KIMI_CODE_HOME` and existing path resolution; never hard-code a user directory.
- A file-event source may watch config and registration events, but **the config directory is not the artifact store**.
- Keep it on the same volume as the target workspace where possible, so a move is an atomic rename rather than a large copy.

## 5. Metadata design

Each artifact's sidecar contains at least:

```json
{
  "schema_version": 1,
  "artifact_id": "art_01KABC...",
  "project": "repo-a",
  "repo": "owner/repo-a",
  "issue": 123,
  "thread": "owner/repo-a#123",
  "assignment_id": "asg_123_worker_2",
  "created_by": {
    "role": "worker",
    "session_id": "session_..."
  },
  "kind": "script",
  "media_type": "text/x-python",
  "original_path": "<workspace>/diagnose.py",
  "file_name": "diagnose.py",
  "extension": ".py",
  "size_bytes": 4821,
  "sha256": "...",
  "created_at": "...",
  "last_used_at": "...",
  "issue_closed_at": null,
  "retention": {
    "policy": "temporary",
    "days": 7,
    "expires_at": "...",
    "pinned": false
  },
  "lifecycle": "active",
  "sensitivity": "normal",
  "evidence_ref": null
}
```

### Key fields

- `kind`: script, log, note, screenshot, archive, test-output, other.
- `original_path`: provenance only; never used to re-execute automatically.
- `sha256`: tells whether the content changed, so "same name, different content" cannot be confused.
- `assignment_id`: which execution produced it.
- `expires_at`: the reclaim criterion.
- `pinned`: retention paused because of an open Issue, an audit or a user request.
- `lifecycle`: active, quarantined, expired, deleted.

### The index keeps only short fields

`index.json` need not duplicate every sidecar. Keep:

```text
artifact_id / project / issue / kind / size /
lifecycle / expires_at / relative_path
```

Maintenance reads the index and never opens artifact content. If the index is corrupt, rebuild it from the sidecars.

## 6. Where metadata comes from

**Register at creation time; do not guess afterwards by reading files.**

Give workers and QA one entry point:

```bash
python -m super_agent artifact register \
  --issue <repo>#123 \
  --assignment <asg_id> \
  --kind script \
  --path <file> \
  --retention temporary
```

The registration tool fills in automatically:

- project, Issue, Assignment;
- the current role and session;
- media type, size, extension;
- creation time, hash;
- the retention policy.

The worker supplies only the business judgement the tool cannot infer: `kind` and intent.

For historical loose files:

- use the existing path, owning task and mtime as hints;
- anything whose project or Issue cannot be determined goes to `unclassified`;
- never read content to "guess" ownership;
- unclassified files are never deleted automatically.

## 7. Lifecycle and automatic reclaim

```text
active
  │ finished, or the task moves to delivery
  ▼
quarantined
  │ waiting for Issue close and the retention period
  ▼
expired
  │ the reclaim job runs
  ▼
deleted (only a tombstone remains)
```

### Suggested defaults

| Policy | Suggested period | Applies to |
|---|---:|---|
| `scratch` | 7 days after close-out | throwaway scripts, exploration notes, intermediate output |
| `evidence` | 30 days after close-out | QA screenshots, logs, test attachments |
| `pinned` | never deleted automatically | open issues, user-requested retention, audit material |

Configuration supports a global default, per-project overrides and per-artifact overrides:

```json
{
  "default_retention": {
    "scratch_days": 7,
    "evidence_days": 30
  },
  "projects": {
    "repo-a": {
      "scratch_days": 7,
      "evidence_days": 30
    }
  }
}
```

### When the clock starts

From the **later** of:

- the artifact's last-used time;
- the owning Issue's close time.

This way nothing is deleted during development merely because seven days have passed since creation.

After close-out:

- scratch keeps another 7 days;
- evidence keeps another 30 days;
- anything with an open Issue, an active Assignment or a pin is not deleted.

## 8. Deletion safety

Automatic reclaim is a continuous write operation. Open it carefully in the first version.

### First enablement

1. Configure periods and scope.
2. Generate the proposed deletion list.
3. Report project, Issue, file count, total bytes and the earliest/latest times.
4. The user approves the retention policy.
5. Dry-run first.
6. Only then may background execution be allowed.

### Before every reclaim

All of these must hold:

- the sidecar is valid;
- the project is on the allowlist;
- the Issue is closed;
- no Assignment is active;
- nothing is pinned;
- `expires_at` has passed;
- the file hash matches the registered one;
- the path is still inside the recycle-bin root;
- it is not a symlink or a junction pointing outside;
- no lock is held and there was no recent write.

A hash mismatch means the file changed after registration: **stop the deletion and alert; never reclaim on stale metadata.**

After deletion only a short tombstone remains:

```json
{
  "artifact_id": "art_...",
  "issue": 123,
  "deleted_at": "...",
  "size_bytes": 4821,
  "sha256": "...",
  "reason": "retention_expired"
}
```

Tombstones may be kept for a further fixed period, as proof of what was deleted when.

## 9. Windows and failure handling

Must account for:

- CJK text, spaces, reserved names and very long paths;
- case-insensitive name collisions;
- files locked by a running process;
- cross-volume moves that are not atomic;
- junctions/symlinks pointing outside the root;
- a crash in the middle of a move;
- the same file registered twice by one Assignment.

Corresponding rules:

- artifact ids avoid name collisions;
- prefer same-volume moves;
- move via a temporary name plus atomic replace;
- resolve real paths before moving or deleting;
- if locked, postpone; never kill the process;
- after a crash, reconcile via the index and sidecars;
- registering the same path twice must compare hashes and never silently overwrite the old record.

## 10. Relationship to the other four designs

### With 01 core architecture

A new artifact-lifecycle facility, not a new LLM role:

```text
Artifact Registry
├─ registration and metadata
├─ project/Issue classification
├─ retention policy
└─ automatic reclaim and tombstones
```

It is deterministic program code. It judges no requirement and no quality.

### With 02 Requirements Analyst

The Analyst reads evidence by reference only:

```text
artifact://art_01KABC...
```

It may read the evidence it needs, but it may not hoard every research file, nor copy sensitive content into the requirement fact store.

### With 03 agent-to-agent communication

Communication carries only:

```json
{
  "artifact_ref": "art_01KABC...",
  "kind": "test-output",
  "sha256": "..."
}
```

Never the file body, and never semantics hidden inside a long file name.

### With 04 Reporter

The Reporter gains a read-only storage view:

- temporary artifact count and size per project;
- how many will be reclaimed in the next 7 days;
- pinned artifacts;
- unclassified items, hash anomalies, failed deletions;
- evidence links inside Issue detail.

The Reporter may only display. It offers no "clean up now" button and no script entry point.

### With the event bus / process-document governance / file events

- Event bus: carries registration, close-out and expiry events.
- Process-document governance: formal text into Issues, temporary artifacts into the bin, so process documents do not pile up again.
- File event source: reuse file events and hot reload; a retention-policy change does not restart anything.
- Close-out reconciliation: close the business session only; do not delete evidence immediately — retention is governed here.

## 11. Implementation order

### Stage 1: register only, delete nothing

- artifact id, sidecar, index;
- filing by project/Issue;
- hash and size registration;
- the Reporter shows usage;
- dry-run produces the reclaim list.

### Stage 2: close-out and retention periods

- compute the period after an Issue closes;
- scratch 7 days, evidence 30 days;
- pins and active Assignments block reclaim;
- verify nothing is deleted before close-out.

### Stage 3: limited automatic reclaim

- one project first;
- every reclaim has a list, a log and tombstones;
- failures may retry but must not widen the scope;
- after a week of verification, extend to other projects.

### Stage 4: tidying historical files

- inventory first, read no content;
- file by metadata where determinate;
- everything else goes to `unclassified`;
- deleting historical files still requires a separate list and authorization.

## 12. Acceptance criteria

- Classify by project, Issue, role, kind and period without opening file content.
- Formal Issue comments and project test files are never reclaimed.
- Files in development, under QA or bound to an active Assignment are never deleted early.
- The 7-day scratch / 30-day evidence policy is configurable and obeyed exactly.
- Pins, hash changes, unknown ownership and external links all block deletion.
- A process restart never moves a file twice, overwrites an artifact or loses a tombstone.
- The Reporter shows usage, expiry and anomalies, but cannot execute cleanup.
- An artifact remains findable for its retention period after the Issue closes.
- Storage usage drops as expected once the period passes.

## Conclusion

> **A stable artifact id + sidecar metadata + project/Issue directories + a lightweight index + 7/30-day tiered retention + safe automatic reclaim.**

Do not encode all metadata into file names, and do not let a config directory carry storage semantics.
