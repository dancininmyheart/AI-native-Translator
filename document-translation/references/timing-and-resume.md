# Timing And Resume

Use `scripts/translation_run_ledger.py` for a local durable run ledger. The
service may project the same fields into its database, but it must keep one
authoritative state and idempotency record for each effect.

## Initialize

```bash
python scripts/translation_run_ledger.py init \
  --run-dir /data/jobs/job-01 \
  --mode pi_agent_live \
  --source /data/intake/source.docx \
  --target-language zh-cn \
  --file-type docx \
  --page-count 12 \
  --quality-profile standard \
  --max-repair-rounds 2
```

For an existing translation, use `--mode pretranslated_qa`. The command creates
`operator-run.json` with UTF-8 JSON and never overwrites an existing ledger.

Supported input file types are `doc`, `docx`, `ppt`, and `pptx`. Candidate and
output artifacts must be `docx` or `pptx` and must remain inside the run
directory.

## Stage Commands

```bash
python scripts/translation_run_ledger.py start \
  --run-dir /data/jobs/job-01 --stage preflight_document

python scripts/translation_run_ledger.py finish \
  --run-dir /data/jobs/job-01 \
  --stage preflight_document \
  --status completed

python scripts/translation_run_ledger.py interrupt \
  --run-dir /data/jobs/job-01 --reason cooperative_shutdown

python scripts/translation_run_ledger.py resume \
  --run-dir /data/jobs/job-01

python scripts/translation_run_ledger.py close \
  --run-dir /data/jobs/job-01 --status completed

python scripts/translation_run_ledger.py summary \
  --run-dir /data/jobs/job-01
```

The ledger may record these stages even when one Pi tool owns an internal
substage:

```text
preflight_document
convert_legacy
translate_once
run_document_qa
apply_repair          # only while current full QA is repairable
run_document_qa       # repeat for each repaired candidate
verify_candidate      # only after full QA has no blocking issue
finalize_result
```

`convert_legacy` is required only for DOC/PPT and is executed inside
`preflight_document`; it remains separately timed in the ledger and is not a
seventh registered Pi tool. `translate_once` is forbidden in
`pretranslated_qa`. Starting `apply_repair` beyond the ledger's configured limit
fails closed.

## Artifact Commands

Register immutable artifacts before another effect depends on them:

```bash
python scripts/translation_run_ledger.py artifact \
  --run-dir /data/jobs/job-01 \
  --kind candidate \
  --path /data/jobs/job-01/candidates/candidate-r0.docx
```

Use `candidate`, `output`, `report`, `audit`, `manifest`, or `evidence` kinds.
The ledger stores SHA-256 and size and refuses duplicate paths. Registered files
must be inside the run directory; output/candidate suffixes are validated.

## Budget

The default timing policy is:

```text
target_budget_seconds = 1800       # 30-minute service target
soft_deadline_seconds = 1620       # do not start another repair after 27 min
cleanup_grace_seconds = 300        # persist evidence and terminate safely
service_concurrency_limit = 20     # active document jobs, not subrequests
default_repair_rounds = 2
hard_repair_round_limit = 3
```

The hard runtime can include a bounded cleanup grace period, but a job that
misses its contractual deadline must not be mislabeled as completed. Check
remaining budget before provider calls, LibreOffice work, render/layout work,
repair, and finalization.

The 20-job limit belongs to the queue/worker scheduler. Model requests and
LibreOffice processes require smaller, independently enforced limiters.

Count provider recovery separately from base translation calls. Language probe,
main translation, and tagged strict recovery draw from one candidate-level
extra-request and recovery-wall-time budget, and both stop at cancellation or
the earlier job deadline. Freeze initial probe+main batch count `b`; the
production extra-call cap is
`min(32, 16 + ceil(max(0, b - 36) / 4))`, and the translation recovery
wall-time cap is
`min(600 seconds, 180 seconds + max(0, b - 36) * 7.5 seconds)` per candidate.
Changing either formula requires a versioned policy and concurrency-capacity
review. Semantic QA HTTP attempts and all `Retry-After`/
backoff waits use one absolute deadline per logical semantic request, inside a
separate 180-second semantic candidate budget.

## Timing Meanings

- `run_wall_seconds`: initialization to close/current time, including waits and
  interruptions.
- `active_processing_seconds`: completed, failed, and interrupted stage time.
- `true_translation_seconds`: completed `translate_once` time for
  `pi_agent_live`; unavailable for `pretranslated_qa`.
- `validation_seconds`: `run_document_qa` plus `verify_candidate` time.
- `repair_seconds`: `apply_repair` time.
- `conversion_seconds`: `convert_legacy` time.
- `finalization_seconds`: `finalize_result` time.
- `interruption_seconds`: explicitly recorded interrupt-to-resume intervals.
- `remaining_target_budget_seconds`: target budget minus elapsed job wall time.

Provider request service durations may overlap. Record their sum separately and
do not substitute it for stage wall time.

## Resume Rules

When resuming:

1. Load the nonterminal ledger and verify schema.
2. Verify the original source hash and all registered artifact hashes.
3. Mark a process-abandoned open stage `interrupted` before resuming it.
4. Close the explicit interruption record.
5. Continue the first incomplete effect in the state machine.
6. Reuse a completed effect by stable request ID; do not execute it twice.
7. Do not rerun `translate_once` solely because QA is incomplete.
8. Create a new immutable candidate for every repair attempt.
9. Increment `resume_count` and retain all prior timing/audit evidence.

If Pi stops normally after persisting blocking QA, deterministic settlement may
consume the remaining registered repair rounds: call `apply_repair`, run full
QA for each accepted immutable child, and stop on regression, no output, the
soft deadline, or the round limit. The same controlled fallback is allowed
after a Pi request error only when durable candidate and QA evidence passes
hash/schema reconciliation. Never start it after cancellation, lease loss,
hard timeout, or incomplete/untrusted durable state.

Resume is stage-level unless the exact translator checkpoint and candidate
digest are durably recorded. Do not claim block-level resume from a cache entry
alone. Batch-level resume additionally requires exact source/request,
provider-route/version, language-pair, glossary, and ordered-block fingerprints,
plus successful protocol validation and an atomic checkpoint for every reused
batch.

## Batch Timing

Use one child ledger per file and one batch index containing the frozen file
list, child job IDs, source hashes, target language, shared glossary digest,
queue timestamps, and terminal states.

Freeze the manifest by normalized relative path plus source SHA-256 before the
first child starts. For a bounded retry run, a terminal retry may override the
same source from the main run; a nonterminal retry never overrides an existing
terminal result. Persist the selected source-run lineage and every override.

Also freeze the evaluation-contract fields in `p0-p1-convergence.md`. A run with
`max_repair_rounds=0` is a labelled `translation_only` diagnostic: it may expose
a recovered candidate, but it must not override an otherwise-matching
post-repair artifact for selection or contribute to the same P1 delta. Its
different repair setting makes it a separate series. If QA policy, required capability,
provider/renderer version, language route, or repair policy differs, publish a
new evaluation series or stratify the results instead of merging the totals.

Compute candidate-backed corpus P0/P1 only from each file's final selected QA
report and optional verification. The sole exception is a pre-candidate failed
or cancelled job: derive one stage-aware P0 from its durable stage/error
evidence, with candidate/report fields null. Deduplicate candidate findings by
stable issue code or ID plus stable Office location and candidate digest. For
text-object overlap, the stable location includes both the text shape identity
and the collided object's stable identity; different objects must not collapse
into one P1. Do not also count stale candidate P1s from an unselected attempt.

Write a corpus selection manifest with nullable candidate, QA, verification,
repair-audit, capability, comparison-fingerprint, and timing fields. A
pre-candidate failure or cancellation instead points to its durable terminal
status, failed/interrupted stage, and P0 evidence. Record a formal result path
only for deliverable terminal states. For `needs_review`, record the best current
full-QA candidate path separately and label it non-deliverable under
`all_or_nothing` so useful artifacts are discoverable without being mistaken for
approved outputs.

Make P0 derivation stage-aware. Words such as `corrupt`, `missing`, or
`invalid` inside semantic, terminology, language, or translation-validation
findings describe translation quality and remain P1; they must not match a
generic package-corruption rule. Promote an error to P0 only from a failed job
or evidence tied to package/openability, conversion, encryption/macro,
relationship/media/structure integrity, hash integrity, or unsafe writeback.

Report batch wall time, queue time, active child time, per-file translation time,
throughput by page/slide, and excluded/failed samples. Do not divide overlapping
batch wall time by file count and call it average translation time.
