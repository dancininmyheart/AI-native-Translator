# P0/P1 Convergence

Use this protocol for a single difficult file and for corpus-wide remediation.
It separates artifact recovery, defect repair, and evaluation so a lower P0
count cannot conceal a newly observable or unevaluated P1 population.

## Freeze The Evaluation Contract

Before comparing runs, persist an evaluation contract containing:

- normalized relative source path and SHA-256 for every sample;
- source/target language route, glossary/protected-term digest, and fidelity
  mode;
- translation provider/model route and version;
- QA classifier/policy version, quality profile, severity mapping, and delivery
  policy, including the P1 weight table;
- required capability set plus renderer/provider name and version;
- configured repair-round limit and action-policy version.

Two totals are comparable only when these fields match. If any field changes,
publish the run as a new evaluation series or show a stratified delta. Never
describe a mixed selection of zero-round and repaired candidates as a
post-repair comparison.

Define `policy_weighted_p1` as the sum of the frozen per-code P1 weights. When
the evaluation policy has no explicit weight for a code, use weight `1`; never
let the model invent weights during a run. Compare continuous PPTX risk/overflow
metrics separately from this count-based score.

`max_repair_rounds=0` is allowed only for a labelled `translation_only`
diagnostic. It can show whether P0 translation failures were recovered and can
provide a candidate for review. Because its repair setting differs, it is a
separate evaluation series: exclude its P1 total from post-repair quality
deltas. For artifact selection only, prefer an otherwise-matching terminal
two-round result over the diagnostic; this is a cross-series priority rule, not
a claim that their P1 totals are comparable.

Production `standard` or `strict` work under `all_or_nothing` should use the
default two rounds whenever repairable blocking P1 exists. The current API still
accepts zero; interpret that value unconditionally as diagnostic. If blocking P1
exists, stop as `needs_review` and start a separate two-round job for convergence;
do not label the zero-round report post-repair or final quality.

## Establish A Truthful Baseline

For every selected candidate, run the same complete QA profile before repair.
Derive and persist a comparison fingerprint from the frozen task contract and
the actual QA report metadata: every required check and its `passed`, `failed`,
or `unavailable` status, semantic block/chunk coverage, classifier/policy
version, capability set, and provider/renderer identity. If a current tool does
not expose one of these fields, record it as `unknown` and label the run
non-comparable; never fabricate a value. An unavailable check is never a pass.

Under the current corpus classification policy, a failed or cancelled job that
never produced a QA candidate contributes one stage-aware P0 and no stale P1
from an unselected candidate. Preserve the distinct terminal status so an
operator cancellation is not misreported as a document defect. When such a file
first produces a candidate, label its P1 as
`newly_observable_after_p0_recovery`; do not count it as a repair-created
regression.

Report at least:

- P0/P1 issue totals and affected file, page/slide, block, and shape counts;
- counts by stable issue code, Office family, language route, and capability;
- newly observable P1 exposed when a former P0 file first produces a candidate;
- repairable, unsafe/unregistered, and provider/capability-blocked cohorts;
- raw pre-repair, accepted post-round-1, accepted post-round-2, and final counts.

Within one candidate, deduplicate by issue code plus stable Office location and
candidate digest. Across parent/child candidates, match resolution and regression
by code plus stable Office location; the different candidate digests establish
lineage but are not part of the cross-candidate identity. For PPTX text/object
overlap, include both the text-shape identity and collided object identity. Do
not equate a change in message wording with a resolved issue.

## Separate Recovery From Document Repair

Do not spend an Office repair round on:

- translation-provider timeout, transport, authentication, or schema failure;
- semantic-QA provider unavailability;
- missing renderer, font, or authoritative PowerPoint capability;
- source/candidate renderer version mismatch or unstable shape enumeration.

Restore the required service/capability, apply its bounded retry policy, and
rerun the same QA contract. If it remains unavailable, retain `needs_review` or
the typed failure. Never shrink text, move a shape, or weaken a gate to make an
infrastructure finding disappear.

## Build Repair Cohorts

The controlled core repair planner translates QA findings into registered
actions only. It groups by stable Office identity and root cause before applying
actions; the Pi-facing tool supplies only the current candidate ID.

| Cohort | Preferred registered action | Guard |
| --- | --- | --- |
| source residue or target-script mismatch | `retranslate` | exact segment/block; preserve protected tokens |
| terminology conflict | `terminology_fix` | approved glossary and matched locations only |
| placeholder, number, date, URL, or reference loss | `placeholder_restore` or constrained `retranslate` | source-derived representation only |
| DOCX run/style drift | allow-listed DOCX paragraph/run property action | exact block/run; preserve fields and relationships |
| PPTX overflow or text-capacity failure | `resolve_text_overflow` | exact text shape and current QA evidence |
| PPTX expansion that remains after the safe layout cascade | `replace_translation` | exact shape; semantic equivalence, target script, and protected tokens must revalidate |
| PPTX font-only regression | `set_font_size` or `adjust_font_size` | configured minimum (currently 8 pt) and source/layout bounds |
| PPTX coordinate/property drift | `restore_shape_coordinates` or `set_shape_bounds` | source-relative, unrotated/unambiguous target only |
| PPTX text/object overlap | overflow action first; then a text-side bounds action if still present | authoritative source-relative rendered evidence; never move the other object |

The PPTX adapter normalizes supported aliases. Prefer the composite
`resolve_text_overflow` action for an overflow cohort rather than emitting
conflicting font, margin, autofit, and expansion actions for the same shape. It
may apply bounded autofit, margins, font reduction, and minimal collision-safe
in-slide expansion. Stay within adapter limits and the per-call action cap
(currently 100).

Do not treat ordinary DOCX body-paragraph growth as a fixed-capacity P1. Only
repair Word layout when a real hard constraint or source-relative property loss
exists.

## Two-Round Schedule

The current failure pattern is dominated by stable shape-anchored PPTX overlap,
overflow, expansion-risk, and capacity findings, followed by stable
block-anchored meaning, omission, and terminology findings. Use that topology,
not raw message order, to prioritize work.

### Round 1: remove deterministic and layout root causes

1. For each PPTX text shape with overflow/capacity evidence, select one
   `resolve_text_overflow` action. If it also has overlap, do not select a
   competing overlap geometry action in this round.
2. Repair authoritative overlap-only shapes through the text side, never by
   moving the collided object.
3. Restore clearly drifted PPTX coordinates/properties.
4. Restore protected representations and deterministic DOCX structure/property
   loss.

For PPTX, submit at most the adapter's current 100 independent actions in the
round; for DOCX, use its registered core limit. If findings exceed the applicable
cap, group by stable shape/block before ranking. Put exact safe restorations
first; for trusted rendered overflow/overlap groups, prefer the smallest
required rendered-load adjustment, then severity, downstream finding count, and
confidence. Preserve original evidence order inside each group. Never truncate
silently; retain the unsubmitted cohort as unresolved evidence for a fresh plan
after QA.

Run full QA and authoritative rendering required by the profile. Accept the
candidate only through the promotion gate below.

### Round 2: recompute and repair residual content

Generate a fresh plan from the accepted round-1 candidate; never reuse stale
findings. Then:

1. complete any still-supported overflow/capacity action omitted by the first
   round's action cap;
2. address text/object overlaps only when overflow for that text shape is gone
   or has improved and the overlap remains source-relative and authoritative;
3. retranslate block-anchored meaning, omission, source-residue, and
   target-script defects with protected-token constraints; use
   `replace_translation` for a shape only when the safe layout cascade is
   insufficient and the replacement passes full semantic/content QA;
4. apply approved terminology fixes and any still-observable safe
   coordinate/property repair.

Re-render and rerun full QA. Call `verify_candidate` only if the fresh full QA
has zero blocking issues. Do not start round 3 merely because P1 remains. A
third round requires explicit policy, remaining budget, a new
registered action supported by current evidence, and an expected strict
improvement; otherwise preserve the best candidate as `needs_review`.

Apply the same format-specific action cap, ranking, and explicit
unsubmitted-cohort rule in round 2. When the cap is reached, retain every omitted
finding in the report; never imply that the two-round budget repaired the whole
cohort.

## Promotion Gate

The controlled core repair tool owns this gate because the Pi-facing tool has no
action-plan or manual-promotion arguments. Compare a proposed candidate with the
current parent, not with an unrelated run. Return `output_created=true` only
when all conditions hold:

1. package/openability/structure gates pass and P0 remains zero;
2. the set of stable P1 identities gains no new member;
3. a P1-targeted round lowers total and policy-weighted P1; a P2-only round may
   instead keep P1 unchanged while a configured blocking P2 strictly improves;
4. no required check, semantic coverage, renderer evidence, or capability is
   removed or weakened, and the derived comparison fingerprint remains
   comparable;
5. rendered PPTX metrics improve monotonically without a new slide-level
   regression;
6. the candidate digest, full-QA report, and repair audit are durably linked;
   verification is additionally required only when the candidate has zero
   blocking issues and is eligible for delivery.

Rejecting a proposal means keeping the parent immutable candidate. Safe actions
for independent shapes may still be audited as attempted, but an unaccepted
proposal cannot become the next round's parent. Pi must additionally confirm the
expected child artifact and run outer full QA after a core-accepted child.

When the first strict PPTX batch improves many independent shapes but introduces
one or more location-scoped rendered regressions, the adapter may isolate those
targets once: rebuild from the parent without actions for the regressed text
shapes, then rerun the same native, package, and rendered gates. This is not a
new repair round. Promote only the rebuilt candidate with zero remaining new or
worsened rendered blockers and a strict quality improvement. Authority or
capability errors, unattributable regressions, and an empty remaining action set
fail closed. Persist the first blocker set, excluded targets, retry blocker set,
and final executed action list.

## Corpus Selection And Artifacts

Maintain one immutable per-file lineage and one corpus selection manifest. For
each source record:

- selected run/job and candidate digest; the candidate fields are nullable only
  for a pre-candidate failure or cancellation, which must instead record the
  terminal status, interrupted/failed stage, and stage-aware P0 evidence;
- evaluation-contract and derived comparison-fingerprint digests;
- repair-round setting, terminal status, and final P0/P1 counts;
- formal result path for `completed` or `completed_with_warnings` only;
- best current full-QA candidate path for `needs_review`, explicitly labelled
  non-deliverable under `all_or_nothing`;
- QA, optional verification, repair-audit, capability, and timing artifact
  paths.

Within one evaluation series, select the accepted post-repair candidate before
the current full-QA pre-repair candidate. A labelled diagnostic candidate is a
last-resort cross-series inspection artifact and never enters post-repair
statistics. A retry may replace an earlier selection only when it uses the same
evaluation contract and passes the promotion/delivery gates. Preserve every
override and reason.

When reporting improvement, show P0 recovery separately from P1 convergence:

```text
P0 recovered: failed jobs before -> failed jobs after
P1 on comparable candidate set: before -> after
Newly observable P1 from recovered P0 files: count
Unresolved: repairable | unsafe/unregistered | capability/provider blocked
```

This prevents increased QA coverage or recovered files from being misreported
as a repair regression while still exposing the absolute remaining P1 burden.

For two aggregated summaries, call the coverage-aware comparison contract and
partition files into `common_covered_files`, `newly_covered_files`, and
`lost_qa_files`. For each count metric, reconcile:

```text
reported total delta
  = common-covered quality delta
  + newly-covered contribution
  - lost-QA baseline contribution
```

Publish raw pre-dedup totals and duplicate removals alongside this decomposition.
If reconciliation does not match, expose the gap as non-QA-covered or selection
churn and do not attribute it to repair quality.

The comparison contract must fail closed. A report path is insufficient QA
coverage: the selected report must parse and carry a terminal QA status. Before
computing any delta, match the classifier and run contract, then match each
common file's source SHA-256, requested/resolved language route, semantic-QA
setting, and rendered backend/capability set. Preserve raw totals and coverage
partitions when evidence is missing or mismatched, but set total/common deltas
and reconciliation to `null`, publish the exact missing/mismatched fields, and
label the runs non-comparable. Never fill absent historical metadata by guess.
`inputs_frozen` must be exactly `true`; two equally unfrozen runs are not a
baseline. In a merged summary, persist and validate the owner run contract for
every selected fallback/retry job so the main run cannot mask a different
profile, provider, or repair limit. For legacy DOC/PPT, compare the QA source
hash with the persisted normalized DOCX/PPTX hash while retaining the distinct
original-input hash as corpus identity.
