# ADR-001: Use a Bounded Agent for Document Repair Decisions

- **Status:** Accepted
- **Date:** 2026-08-18

## Context

Document translation is mostly deterministic orchestration: ingest a known
file, create one translation, run QA, optionally repair, verify, and publish.
An LLM is useful for deciding whether observed QA evidence justifies another
repair attempt, but it is not a trustworthy authority for filesystem access,
provider configuration, retry budgets, or release approval. Office files and
tool output can also contain adversarial instructions.

## Decision

Use Pi as a bounded decision-maker inside a server-owned state machine.
Production sessions receive exactly six custom tools:
`preflight_document`, `translate_once`, `run_document_qa`, `apply_repair`,
`verify_candidate`, and `finalize_result`. Built-in read/write/shell/search
tools are disabled. Model-visible parameters are empty or contain only the
current opaque candidate ID; all paths, languages, providers, concurrency,
budgets, and output names come from trusted job/config state.

Code—not prompt text—enforces current state, candidate equality, full-QA
requirements, verification, soft/hard deadlines, repair caps, and terminal
outcomes. Python independently validates the same fixed tool names and strict
request contracts.

## Consequences

Benefits:

- Prompt injection cannot directly grant a shell, arbitrary file path, secret,
  new tool, or larger budget.
- Tool traces and state transitions are reviewable and reproducible.
- The workflow can stop safely at `NEEDS_REVIEW` when the model or repair
  process cannot satisfy the gates.
- Provider/model replacement does not redefine the authorization boundary.

Costs and trade-offs:

- The agent cannot improvise a new repair primitive without a reviewed code
  and schema change.
- State recovery, idempotency, and gate logic are more complex than a single
  prompt or an unconstrained tool loop.
- Model quality still affects choices among allowed operations; bounded
  capability is not proof of good decisions.

## Alternatives considered

- **Unrestricted coding agent:** rejected because document text could steer
  shell/filesystem capabilities and because results would be difficult to
  authorize or reproduce.
- **Prompt-only restrictions:** rejected because prompts are not an
  authorization mechanism.
- **Fixed two-round workflow with no model:** viable as a baseline and simpler,
  but it spends repair work without evidence and cannot stop/continue based on
  bounded QA observations. It remains an evaluation baseline, not the selected
  runtime architecture.

## Validation

The runtime allowlist, strict parameters, path confinement, and budget checks
are covered by focused tests. Those tests validate selected behavior, not the
absence of every security flaw or every possible model failure.
