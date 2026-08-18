# ADR-003: Fail Closed at the Deliverable Boundary

- **Status:** Accepted
- **Date:** 2026-08-18

## Context

A generated Office file may exist while still being unsafe to release: QA may
contain blocking errors, a repair may have regressed, verification may have
failed, the deadline may have expired, or recovery evidence may be incomplete.
Treating “file exists” or “model says done” as success converts uncertainty
into a customer-visible artifact.

## Decision

Separate artifact existence from deliverable authorization. A selected
candidate can be finalized only after fresh full QA reports zero blocking
errors and final package/regression/openability verification passes for that
same candidate. Finalization transitions the job to `COMPLETED` or
`COMPLETED_WITH_WARNINGS` and records the result/report paths.

When a gate cannot be satisfied, the service uses `NEEDS_REVIEW`, `FAILED`, or
`CANCELLED`. It may retain a candidate and QA report for diagnosis, but the
result endpoint returns a conflict unless the job is in one of the two
completed states. Path containment and deliverable extension checks are still
applied after the terminal-state check.

Unknown/corrupt recovery evidence, tool failures, budget exhaustion, and
non-improving repair therefore reduce availability or require human review;
they do not broaden publication authority.

## Consequences

Benefits:

- A high-risk or unverifiable candidate cannot become downloadable merely
  because bytes exist on disk or a path appears in durable state.
- Failure states remain honest and auditable.
- Recovery can retry/reconcile evidence without silently weakening the gate.

Costs and trade-offs:

- Some usable-looking translations are withheld and require operator review.
- Verification/provider outages can reduce release coverage.
- The product needs a clear review workflow and retention policy for retained
  candidates and reports.

## Alternatives considered

- **Publish with a warning whenever a file exists:** rejected because warnings
  do not prevent consumers from relying on an artifact with known blockers.
- **Let the model declare completion:** rejected because model prose is not
  durable gate evidence.
- **Use a score threshold alone:** rejected because aggregate scores can hide a
  new blocking issue; the workflow also binds issue counts/fingerprints and
  verification to the selected candidate.

## Validation

The focused security-boundary test places an existing result path on a
`NEEDS_REVIEW` job and verifies that the download API remains closed. Other
tests cover QA/verification authority and recovery evidence. This validates
specific implementation behavior, not a complete security assessment.
