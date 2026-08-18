# ADR-002: Preserve Immutable Candidate Generations

- **Status:** Accepted
- **Date:** 2026-08-18

## Context

In-place repair destroys the exact artifact and QA evidence that justified a
decision. It makes regression comparison, crash recovery, audit, and rollback
ambiguous: after a partial write, neither the old nor new state can be trusted
without reconstruction.

## Decision

Represent each translation/repair output as a new candidate generation:
`candidate-r0` is the initial or pretranslated candidate and each accepted
outer repair creates the next bounded generation through `candidate-r3`.
Candidate records bind ID, relative path, size, SHA-256, source identity,
languages, creator, parent, and QA/verification evidence.

The selected-candidate pointer may move; the prior candidate bytes do not. A
new repair candidate must receive fresh full QA. If full QA does not strictly
improve it without introducing new blocking identities, the system retains the
parent and enters `NEEDS_REVIEW` rather than silently adopting the regression.
Finalization copies the selected verified candidate into a separate result
artifact and records its digest.

This is application-level immutability enforced by create-new paths, no silent
overwrite, state contracts, and digest checks. It is not filesystem WORM
storage; an administrator with write access remains inside the deployment
trust boundary and integrity checks must detect unexpected mutation.

## Consequences

Benefits:

- Every repair has a comparable before/after artifact and QA snapshot.
- Rollback is pointer selection/retention rather than reverse mutation.
- Crash recovery can validate durable candidates by identity and hash.
- Audits can explain which generation was evaluated, selected, and released.

Costs and trade-offs:

- Storage grows with each repair generation, though the hard round cap bounds
  growth.
- State must keep candidate, parent, report, and digest relationships
  consistent.
- Cleanup/retention policy is required for completed and abandoned jobs.

## Alternatives considered

- **Edit the current document in place:** rejected because partial failure and
  regression erase trustworthy evidence.
- **Keep only byte backups:** rejected because backups without candidate IDs,
  digests, parentage, and QA binding are difficult to reason about during
  recovery.
- **Store only patches:** rejected as the primary artifact because Office
  package transformations and native conversions are not reliably reversible
  from a generic patch log alone.

## Validation

Existing tool and recovery tests exercise candidate creation, parent
retention, digest binding, regression rejection, and final-result adoption.
They demonstrate the implemented protocol; they are not proof against a
privileged storage attacker or every filesystem race.
