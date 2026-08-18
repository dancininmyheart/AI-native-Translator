# Pi And translation_core Entrypoints

## Runtime Ownership

Use one authoritative implementation for each responsibility:

- `document-translation/`: Pi's production workflow and policy contract;
- `translation_core/`: Office parsing, translation writeback, QA, repair,
  legacy conversion, manifests, and deterministic CLI/library code;
- `translation_core/pi_agent/`: Pi SDK session, custom-tool adapters, job
  worker, cancellation, and event projection.

Package the Skill into the worker image at a stable path such as
`/opt/format-translator/skills/document-translation`. Point the Pi resource
loader at that directory. Do not maintain a second edited copy inside the Pi
application.

Run the Python package from the installed `format-translator-core` wheel. In a
source checkout, run commands from `translation_core/` with `src` on the Python
path or install the project into an isolated environment.

## Pi Session Contract

Create one Pi `AgentSession` per document job. Load this Skill explicitly and
disable all built-in execution/file tools. Register only the six tools below.
The application, not the prompt, must enforce state transitions, deadlines,
repair limits, workspace boundaries, cancellation, and idempotency.

Each tool receives a validated task context plus logical artifact identifiers.
The adapter resolves actual paths inside the server-owned job workspace. Reject
absolute paths, traversal, commands, client-provided provider endpoints, and
credentials in tool arguments.

### `preflight_document`

Pi-facing input is an empty object. The server injects the registered job
context. The Python bridge payload is:

```json
{
  "input_relpath": "input/source.docx"
}
```

For `pretranslated_qa`, the server also injects the all-or-none tuple
`existing_translation_relpath`, `candidate_id="candidate-r0"`,
`source_language`, and `target_language`. `job_root` and `job_id` are bridge-owned
context on every Python request; Pi never supplies them.

The current summary returns `source_format`, `normalized_format`,
`normalized_relpath`, `page_or_slide_count`, `block_count`, `size_bytes`, and
`pretranslated_candidate_id`. Artifacts carry the input, normalized source when
conversion occurred, and the registered pretranslated candidate when present,
including their hashes. Convert DOC to DOCX and PPT to PPTX through
`translation_core` using an isolated LibreOffice profile.

Legacy conversion is an internal, separately timed `convert_legacy` substage of
this tool, not a seventh Pi tool. Preflight may report an explicit source
language supplied by the task, but automatic text-language detection and target
resolution occur inside `translate_once` after extraction and before its first
external provider call.

### `translate_once`

Pi-facing input is an empty object. The server builds the Python bridge payload
from immutable job configuration: `input_relpath`,
`candidate_id="candidate-r0"`, `source_language`, `target_language`, the
server-owned provider config, `layout_mode`, and translation concurrency. The
current adapter omits glossary and retry fields, so the Python contract uses an
empty glossary and its bounded retry default. Provider URLs and credentials are
never Pi arguments.

The current summary returns pipeline status, candidate ID/creation flag,
document format, source/requested/resolved target language, provider kind, issue
counts, `auto_qa_repair=false`, resume state, and whether the provider was
invoked. The candidate/report artifacts contain hashes and durable paths.

For `auto`, extract the full translatable text, apply the deterministic
document-level mixed-script rule from the main Skill, persist the detected
source and resolved target, then use that one target for every block.

Read detailed provider call/recovery evidence from the pipeline manifest/report
artifacts; the current compact summary does not guarantee every timing/token or
child-request field. If there is no translatable text, retain the immutable
candidate with `provider_invoked=false`; do not invent a provider call.

Expose this tool only in `pi_agent_live`. Provider configuration is owned by
the server. Pi must not supply a URL or secret.

### `run_document_qa`

Pi-facing input is only the current `candidate_id`. The server injects
`rendered_layout_required`, `semantic_qa`, and processing concurrency from the
fixed profile/deployment configuration. The current adapter omits
`provider_timeout_seconds`, so the Python contract uses its 60-second default;
do not claim a deployment-specific timeout was injected.

The current summary returns `candidate_id`, severity issue counts,
`quality_score`, `semantic_qa`, and available QA metadata. Structured issues and
the QA report artifact contain codes, stable Office locations when available,
repair hints, and capability/layout evidence. Corpus P0/P1 is derived from these
stage-aware records; it is not a separate Pi request field.

Bind DOCX checks to `format_translator.word_qa.run_docx_qa`. Bind PPTX checks to
`format_translator.pptx_rendered_layout_qa.run_pptx_qa_with_rendered_layout` so
native and configured rendered-layout evidence are combined truthfully.

Preserve every check status and coverage field that the current QA report
actually emits. Semantic-provider failure must not erase deterministic results
or already completed semantic chunks; absent required evidence is `unknown`,
not a synthesized pass.

The current registered response has no standalone `coverage_fingerprint` field.
For comparisons, derive the fingerprint described in
`p0-p1-convergence.md` from the immutable task contract and actual report
metadata. If required evidence is absent, record `unknown` and label the
comparison non-comparable instead of claiming the tool returned it.

For PPTX inventory, traverse the active logical shape tree in `p:spTree` and
groups. Respect the selected `mc:AlternateContent` branch, do not descend into
`graphicFrame`/OLE preview packages as if they were slide shapes, and count an
active logical shape only once.

### `apply_repair`

Pi-facing input is only the current `candidate_id`. The server checks QA state,
remaining rounds, and soft deadline, derives the next immutable
`output_candidate_id`, and sends Python `max_rounds=1` plus rendered-layout and
processing-provider flags. Pi does not send a repair round or action list.

The Python tool derives actions from the persisted full-QA report and the exact
`RepairActionType` whitelist. Its current summary returns parent/output IDs,
whether an output was created, internal round count, accepted score, rollback,
issue counts, processing-provider use, resume/invocation flags, and typed failure
reason when applicable. Candidate and repair-report artifacts contain hashes,
paths, and detailed applied/rejected evidence.

Bind Word repairs to `format_translator.repair_engine`. Bind PowerPoint repairs
to `format_translator.pptx_repair_adapter`. Never expose low-level arbitrary XML
mutation.

The repair report, not `verify_candidate` arguments, records comparison with the
parent. Register an accepted proposal only as a new immutable candidate; never
replace the prior candidate.

### `verify_candidate`

Pi-facing input is only the current `candidate_id`. The live adapter permits the
call only in QA state after current full QA reports zero blocking issues. It
injects `rendered_layout_required` and the bridge context.

The Python tool rechecks package and configured deterministic/layout gates. The
current summary returns `candidate_id`, `package_valid`, package-part count,
severity issue counts, quality score, and available QA metadata; the verification
artifact links candidate/source hashes and detailed issues. It does not return
`coverage_fingerprint`, `repair_budget_status`, or
`unattempted_repairable_blocking_count`. Obtain repair budget/round state from
the durable job ledger and derive comparison evidence from the QA artifact.
Never call this tool merely to inspect a blocking candidate.

Repair promotion compares stable issue code plus Office location in the repair
audit, not list order or transient message text. Layout acceptance uses weighted
severity/risk metrics and retains the source/prior baseline for unresolved
rendered findings; a candidate cannot improve by merely changing issue
serialization, changing renderer/provider/version/capabilities, or making a
required check unavailable.

### `finalize_result`

Pi-facing input is the current `candidate_id`. The server permits finalization
only for that current successfully verified, nonblocking candidate. The bridge
may add a server-owned result basename. Promote the immutable artifact and
return only DOCX or PPTX result metadata/artifacts; refuse an unverified
candidate or a candidate with blocking issues.

## Deterministic Python Entrypoints

The production Pi adapter should call Python library functions or a bounded
local protocol. The installed CLI is useful for diagnostics and acceptance
tests.

Inspect a document:

```bash
format-translator inspect /data/jobs/<job-id>/source/input.pptx
```

Translate through an OpenAI-compatible route configured by an environment file:

```bash
format-translator translate /data/jobs/<job-id>/source/input.docx \
  --output-dir /data/jobs/<job-id> \
  --job-id <job-id> \
  --source auto \
  --target zh \
  --llm-env /run/secrets/translation-provider.env \
  --enable-processing-agent \
  --qa-repair-rounds 2 \
  --concurrency 4
```

Run bounded QA/repair for a pretranslated candidate:

```bash
format-translator qa-repair \
  /data/jobs/<job-id>/source/source.docx \
  /data/jobs/<job-id>/candidates/candidate-r0.docx \
  --work-dir /data/jobs/<job-id>/qa-repair \
  --job-id <job-id>-qa \
  --max-rounds 2
```

Do not let Pi construct these command lines. The custom-tool adapter validates
the structured request and invokes the corresponding library/CLI entrypoint
with server-owned arguments.

## Linux Configuration

Inject model values from a secret manager or environment file. Supported
provider names include:

```text
OPENAI_BASE_URL
OPENAI_API_KEY
OPENAI_MODEL
```

Never print or return the secret. Configure the Linux Office runtime with:

```text
FORMAT_TRANSLATOR_PPTX_LAYOUT_BACKEND=libreoffice
FORMAT_TRANSLATOR_SOFFICE_PATH=/usr/bin/libreoffice
FORMAT_TRANSLATOR_LIBREOFFICE_MAX_WORKERS=3
FORMAT_TRANSLATOR_LIBREOFFICE_QUEUE_TIMEOUT_SECONDS=120
FORMAT_TRANSLATOR_LIBREOFFICE_TIMEOUT_SECONDS=120
FORMAT_TRANSLATOR_LIBREOFFICE_LOCK_DIRECTORY=/var/lock/format-translator/libreoffice
```

All workers on one host must share the LibreOffice lock directory. The
LibreOffice pool limit is independent of the 20-document service concurrency
limit.

## Capability Gate

On Linux, publish `package_openability` for the LibreOffice PPTX backend. Do not
publish `shape_text_bounds`. If a task requires authoritative PowerPoint text
bounds, require an approved Windows layout worker or return
`PPTX_RENDERED_LAYOUT_CAPABILITY_INSUFFICIENT`.

Readiness must be false for new tasks when the source store, job workspace,
provider route, queue, required Office backend, or result authority is
unavailable. Route presence or process liveness alone is not readiness.
