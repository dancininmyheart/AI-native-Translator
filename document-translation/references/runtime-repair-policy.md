# Runtime Repair Policy

## Limits

- Default repair limit: 2 iterations.
- Hard ceiling: 3 iterations, even if Pi requests more.
- Start no repair after the soft deadline.
- Create one immutable candidate and one audit per iteration.
- Never modify the source or a previously registered candidate.

The application must enforce these limits outside the prompt. A duplicated
effect/request ID returns its durable prior result rather than applying the
repair again.

## Severity And Delivery

- P0: never deliver.
- P1: never deliver under `all_or_nothing`.
- P2: deliver only when the profile/policy marks its category nonblocking, and
  report it as a warning.
- P3: record without blocking unless deployment policy is stricter.

Use `completed_with_warnings` only when every remaining issue is explicitly
nonblocking. Otherwise use `needs_review`.

## Provider Recovery Before Repair

Provider recovery happens inside `translate_once` or semantic QA and does not
consume an Office repair round. Preserve every successful batch and recover
only the failing subset.

| Failure | Recovery |
| --- | --- |
| HTTP 429 | honor `Retry-After` inside the remaining deadline; never split or fan out |
| HTTP 502/503, timeout, connection reset, network interruption, or remote protocol disconnect | bounded retry; after exhaustion, split only an HTTP 502/503 failed translation batch when the shared budget permits |
| invalid schema, missing/extra/duplicate block ID, or invalid tagged segment | isolate the failed subset; a tagged singleton gets at most two budgeted strict attempts and then fails closed |
| HTTP 400 other than a typed retryable condition | fail the affected request without retry |
| HTTP 401/403 or invalid provider configuration | fail immediately |

For translation batches, `max_provider_retries` defaults to 3. A retryable
failure therefore gets at most three same-batch recovery calls after its base
call, with 1, 2, and 4 second backoff inside the shared deadline. A 429 is never
split. Only an exhausted HTTP 502/503 batch may be halved, to a maximum split
depth of 2. A multi-block protocol failure is isolated immediately by the same
bounded split; a tagged singleton may get at most two strict-schema attempts
(`a0`, then `a1`) when the first response still violates the segment contract.
Do not catch budget, deadline, retryable transport, or cancellation failures as
a reason for the second protocol attempt. Every retry, split child, and strict
attempt consumes the same candidate recovery budget.

Language probe, normal translation batches, and tagged strict retries share one
candidate-level cap on extra requests and recovery wall time. Both caps are
subordinate to cancellation and the job deadline. Keep one durable outer
effect ID, but give every child attempt a deterministic unique request ID and
record its parent, phase, split depth, attempt, and outcome. A durable replay of
the outer effect reuses its recorded result; it does not issue new child calls.
Freeze the initial probe+main batch count before provider recovery. For `b`
initial batches, compute the extra-call cap as
`min(32, 16 + ceil(max(0, b - 36) / 4))` and the recovery wall-time cap as
`min(600 seconds, 180 seconds + max(0, b - 36) * 7.5 seconds)`. Thus files with
at most 36 batches retain 16 calls/180 seconds, while 80 batches receive 27
calls/510 seconds. Dynamic splitting never recomputes or expands either cap.
These caps do not change the separate semantic-QA budget.
Start the recovery clock when recovery is first required, so backoff and sleeps
consume the same wall-time cap. Schedule initial batches through a bounded lazy
worker pool: after a terminal recovery failure, stop unsent batches and cancel
and await siblings instead of eagerly flooding the provider.

For high-batch-count files, persist each successful batch atomically after
schema, exact block-ID, tagged-segment, and protected-token validation. Bind it
to the source/request fingerprint, provider route and version, language pair,
glossary digest, ordered block digest, and parent effect. Resume only exact
matches and preserve original result order. Audit reused versus newly requested
batches plus the final transport class, HTTP status when present, provider
request ID, attempt, split path, and recovery-budget consumption. This is a
production hardening requirement; an in-memory result or generic cache entry
does not provide batch-level resume.

Map transport-layer exceptions by their transport class, not by brittle message
matching. A server disconnect before a valid HTTP response is retryable; an
HTTP success carrying invalid JSON or an invalid result schema is a protocol
failure. Never turn HTTP 401/403, invalid credentials, or invalid endpoint/model
configuration into retries.

Tagged translation is a strict protocol. Require exact block and segment ID
coverage, uniqueness, and order; preserve protected tokens and run/style
boundaries. Recover missing pure whitespace locally only when it is provably
whitespace. If the provider's complete text and non-whitespace segment text do
not agree, retry only that block and then fail closed. Validate this text
contract inside every strict attempt: correct IDs alone do not make an attempt
successful, and a text-invalid `a0` may consume `a1` from the same recovery
budget.

## Registered Actions

Only exact `RepairActionType` values in the deployment whitelist are executable.
The Pi-facing `apply_repair` tool accepts only the current candidate ID; the
controlled Python repair tool derives and validates these actions from persisted
QA. Pi never sends an action list or invents lower-level mutations.

| Finding | Exact action value(s) | Scope |
| --- | --- | --- |
| source-language residual or target-script mismatch | `retranslate` | exact segment/block |
| protected token, number, date, URL, or reference loss | `placeholder_restore`; `retranslate` only when restoration alone cannot be validated | exact segment/block |
| approved terminology conflict | `terminology_fix` | matched locations |
| duplicate or explicitly replacement-safe content | `replace_translation` | exact block/shape with token remapping |
| bounded generic text fitting | `fit_text` | exact hard-constrained block |
| DOCX page-break drift | `insert_page_break_before`, `remove_page_break_before` | exact paragraph |
| DOCX keep/spacing drift | `set_keep_next`, `set_keep_lines`, `set_spacing` | exact paragraph |
| PPTX overflow/capacity | `resolve_text_overflow` (preferred composite); internally normalized `enable_autofit`, `set_textbox_margins`, `adjust_font_size`, `set_font_size` | exact shape |
| PPTX coordinate/bounds drift | `restore_shape_coordinates`, `set_shape_bounds` | exact shape |
| unsafe, ambiguous, or unsupported finding | `flag_for_review` | exact finding |

Provider retry/schema recovery and package/writeback reconstruction are separate
controlled effects described earlier; they are not `RepairActionType` values and
must not be represented as Office repair actions.

Disallow arbitrary serialized-XML replacement, global search/replace, unrelated
shape movement, field or relationship deletion, protected-run merging,
unbounded font shrinking, hiding content, and any action that weakens a quality
assertion.

## Iteration Procedure

Before choosing an action, follow the root-cause grouping and two-round order in
`p0-p1-convergence.md`. Infrastructure/provider/capability recovery is not an
Office mutation and consumes no repair round. A zero-round run is diagnostic,
not post-repair evidence.

For each round:

1. Select blocking or configured repairable findings with stable Office
   locations.
2. Confirm remaining time, cancellation state, current candidate digest, and
   round number.
3. Call `apply_repair` for the current candidate. Its registered core derives
   and validates exact actions; Pi supplies no action plan.
4. The core persists the immutable child and repair report only after its
   internal affected-check and terminal-gate evaluation. The server selects the
   child only when `output_created=true`, the expected child ID, and candidate
   artifact are all present; otherwise it retains the parent.
5. The core acceptance gate compares the parent/child using stable issue
   identities, severity counts, coverage, terminology, structure, package
   health, layout metrics, and capability evidence. P0 must remain zero and no
   new stable P1 may appear. A P1-targeted round must lower total and weighted
   P1; a P2-only round may instead keep P1 unchanged while lowering configured
   blocking P2. Coverage/capability must not weaken.
   A strict PPTX executor may perform one deterministic recovery attempt when
   the first batch has only location-scoped rendered regressions: identify the
   regressed text-shape targets, discard only actions for those targets, rebuild
   from the immutable parent, and rerun native/package/rendered gates. Keep the
   independent actions only if the retry has no new or worsened rendered
   blocker and is a strict improvement. Never recover through this path from a
   renderer authority/capability failure, an unattributable blocker, or a retry
   with no remaining action. Audit the initial blocker count, excluded targets,
   and remaining blocker count. Compare table geometry quantitatively when a
   table finding exists in both baseline and child; greater row-height deviation,
   newly changed rows, or new row-count/coordinate drift is a worsened blocker.
   Before isolating `replace_translation`, map its block ID uniquely to the
   current slide/shape. Missing or ambiguous identity is unattributable and must
   fail closed rather than retaining a possibly causal replacement.
6. After a child is selected, Pi runs outer full QA for that candidate. Call
   `verify_candidate` only when this QA has zero blocking issues; the live Pi
   adapter rejects verification while blocking QA remains.

Rollback means retaining the prior accepted candidate; never mutate a proposed
candidate to masquerade as the earlier one.

Stop immediately when:

- all configured gates pass;
- cancellation is requested;
- the soft deadline prevents another safe round;
- no registered action exists;
- the proposed candidate introduces a new P0/P1;
- a required capability is unavailable;
- the configured limit is reached.

Return `needs_review` when blocking findings remain and an immutable current
full-QA candidate can be preserved. Such a candidate is selected for inspection,
not verified or deliverable. Return `failed` for unrecoverable package,
authority, or workflow failures.

## DOCX Rules

Keep paragraphs, tables, headers/footers, notes, fields, relationships, styles,
numbering, bookmarks, and section geometry intact. Do not merge runs merely to
make translation easier. Restore only allow-listed formatting properties at an
exact structural location.

A provider-only coalescing projection is allowed for a fragmented tagged DOCX
block only when adjacent runs have unique consecutive run indexes, identical
direct style fingerprints, distinct text-node paths, and explicit ordinary-run
metadata. The block may contain no structural risk except
`multiple_text_nodes` and `multiple_styles`; missing, malformed, or unknown risk
metadata disables coalescing. Never coalesce across a field, hyperlink, manual
break, tab, bookmark, comment anchor, tracked change, content control,
drawing/object, note reference, or other unknown boundary. Keep every original
run and text node in the document, expand the provider result to the exact
original IDs/order, and verify unchanged translated-text concatenation before
writeback. Any mapping inconsistency is a protocol failure, not a reason for
partial output.

After every repair, validate ZIP/XML structure and compare structural inventory
against the immutable normalized source.

Do not classify ordinary body-paragraph expansion as a fixed-capacity P1: Word
can reflow it across lines and pages. Use script-aware visual advance estimates,
not raw character-count ratios. Emit a capacity finding only for a stable hard
constraint such as an exact-height table row, a fixed-layout no-wrap table cell,
a non-growing text box/frame, or an explicit maximum-length hint. Any repair or
retranslation hint must name the exact package part and structural location;
never solve it by deleting meaning, changing global fonts, or changing document
geometry.

## PPTX Rules

Identify targets by one-based slide number plus OOXML shape ID. Preserve slide
size, masters, layouts, themes, media, charts, relationships, animation data,
and unrelated object coordinates.

Resolve stored XPath prefixes for linked chart/workbook text from the actual XML
part, including extension namespaces declared on descendants. An unknown prefix,
or the same prefix bound to conflicting URIs within the part, is a scoped
fail-closed linked-location finding. Convert XPath parser/evaluation errors into
stable structured QA evidence; never crash the whole job, silently skip the
location, or guess a namespace URI.

For overflow, apply the least invasive safe step and remain above configured
minimum font sizes. For coordinate repair, reject rotated/flipped or ambiguous
collisions unless a dedicated action explicitly supports them. Re-run native
shape, font, text-box capacity, bounds, coordinate, and package checks.

Group overflow, capacity, font, margin, and autofit findings by text shape.
Prefer one `resolve_text_overflow` action per shape so the adapter can merge the
bounded cascade. Do not emit conflicting low-level actions for the same shape.
When the same text shape also owns overlap findings, repair overflow first,
re-render, and generate a fresh overlap plan only for the residual finding.
Apply the action cap after shape grouping. Rank exact safe restorations first,
then trusted rendered text groups by the smallest required rendered-load
adjustment; this increases the number of actions that can complete without
relaxing the 8 pt hard floor. Preserve original issue/evidence order inside a
shape, and audit cap-excluded and engine-skipped groups separately.

Estimated capacity never overrides authoritative rendered evidence for the same
shape. Under `standard`, an estimate-only finding may authorize a bounded
location-scoped text-side action, but it cannot claim rendered approval; accept
it only when native capacity improves without structural or layout regression.
Under `strict`, estimate-only findings cannot authorize automatic repair: an
issue-derived overflow action must have matching code, slide, shape ID,
rendered measurement, allowed action, and adapter provenance in the current QA
report. Apply autofit, bounded margin reduction, and bounded font reduction
before considering a minimal in-canvas, collision-safe text-box expansion.
Compare rendered layout with the immutable source so inherited template overlap
is not confused with a translation-induced regression. Re-render after each
round and accept only monotonic improvement without a new slide-level
regression or lost capability. Skip an unsafe shape without rolling back safe
independent shapes, but keep its P1 and audit the rejection. For text/object
overlap, repair the text side only; do not move the other object. Never move
unrelated objects or treat a provider repair plan as rendered evidence.

Treat non-positive rendered available width or height as a shape-level
measurement failure. Do not divide by zero, invent an overflow ratio, or drop
the target. Preserve the measurement with a stable blocking reason, continue
measuring independent shapes, and keep the candidate out of final acceptance
until authoritative bounds are available or a reviewed exception exists.

An authoritative source-relative overlap that is new or has worsened is P1.
An unchanged overlap already present in the immutable source is not a
translation regression. A native estimate without authoritative line/object
bounds is `PPTX_SUSPECTED_SHAPE_OVERLAP`, a warning/P2 that is nonblocking under
`standard`; it never proves the rendered gate passed. Under `strict`, the
required rendered capability must still complete or the job remains
`needs_review`.

Do not label intentional text-on-background composition as a collision. A
slide-local image may be excluded from overlap checks only when authoritative
PowerPoint evidence proves all of the following: it is ungrouped, non-text,
covers at least half the slide, and its z-order is behind the measured text
shape, which is itself an identified ungrouped top-level slide object. A group
child's local z-order is not comparable with a top-level image. Missing or
incomparable z-order evidence fails closed. Foreground images, charts, ordinary
shapes, grouped objects, and inherited objects remain eligible overlap targets.

For a required comparison, missing source capability, source/candidate provider
or version mismatch, unstable capability sets, failed object enumeration, or
unstable Shape IDs are blocking capability findings.

## Semantic QA Availability

Deterministic package, structure, token, script, terminology, and layout checks
must continue if semantic QA is unavailable. Preserve successful semantic
chunks and emit a single candidate-level provider-unavailable finding rather
than one duplicate per block. Provider retry exhaustion is final at the
pipeline layer; do not multiply the provider's internal HTTP retry loop. Only a
protocol-invalid tagged block may receive the bounded strict attempts (`a0`,
then `a1`) permitted by the shared candidate budget. Normalize
contradictory `passed` flags conservatively: any error is blocking, and
`passed=false` with no error becomes an explicit review-required error.

The current semantic candidate budget is 180 seconds and at most three extra
logical recovery calls beyond the one base check per selected block. Stop
queued checks after three consecutive protocol-invalid blocks, a provider-wide
failure, cancellation, or budget exhaustion. A provider must declare whether it
owns its internal semantic retries; the pipeline never guesses from a concrete
class name. Preserve aggregate logical-call and reported HTTP-attempt audit
counts, and mark incomplete attempt reporting explicitly.

Validate every semantic result against the requested block ID. Fill a missing
issue block ID with the current block, but treat a nonmatching result or issue
block ID as a protocol error. Pipeline-owned semantic retries use deterministic
unique child request IDs linked to the stable parent and record a typed,
secret-free stop reason on terminal failure.

On Linux, LibreOffice re-open/re-save is an openability gate only. If acceptance
requires authoritative PowerPoint shape text bounds, use an approved Windows
worker or retain `needs_review`; never infer a pass from LibreOffice alone.

## Escalation Evidence

When no safe runtime action exists, preserve a bounded defect package:

- job, source, and candidate hashes;
- file family, task/profile versions, and deployment capabilities;
- issue ID, severity, stable Office location, and repair hint;
- relevant bounded XML/text metrics without unrelated document content;
- attempted actions and before/after quality metrics;
- redacted logs and report/audit identifiers.

Do not change application source code from inside a live document job.
