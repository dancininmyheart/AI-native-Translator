# Agent Tool Permission Matrix

This document describes the effective model-facing permission boundary of the
Pi sidecar. The model receives six custom tools and no built-in filesystem or
shell tools. Parameters not shown below are rejected by the TypeBox schemas;
the Python bridge independently uses Pydantic contracts with
`extra="forbid"`.

| Tool | Model-supplied input | Server-bound input | Allowed state and gate | Observable effect | Authority deliberately absent |
| --- | --- | --- | --- | --- | --- |
| `preflight_document` | None | Registered input path; optional separately registered pretranslated candidate; fixed languages and `candidate-r0` | `PREFLIGHT`; pretranslated fields are supplied only in `pretranslated_qa` | Validates/normalizes the registered source and registers pretranslated `candidate-r0` when applicable | No arbitrary path, provider, command, output name, or candidate ID |
| `translate_once` | None | Registered input, languages, quality profile, provider kind, concurrency, and fixed `candidate-r0` | `PREFLIGHT`; only `pi_agent_live` | Creates immutable initial candidate `candidate-r0` and records resolved routing | No prompt, glossary, provider URL/key, path, output name, retry budget, or concurrency override |
| `run_document_qa` | Current opaque `candidate_id` | QA modes and processing concurrency derived from server policy | `QA`; supplied ID must equal the selected candidate | Writes a QA report and persists bounded issue counts/fingerprints | No path, arbitrary candidate, QA-disable flag, provider setting, or report location |
| `apply_repair` | Current opaque `candidate_id` | Next candidate ID, one internal round, QA mode, and processing-provider policy | `QA`; current candidate; QA has issues; before soft deadline; below job and hard repair caps | Attempts one deterministic repair into a new candidate; otherwise retains the parent and enters `NEEDS_REVIEW` | No repair action list, path, output name, nested-loop budget, shell, or provider configuration |
| `verify_candidate` | Current opaque `candidate_id` | Verification profile | `QA`; current candidate; full QA has no blocking errors | Performs final package/regression/openability verification | No path, verification bypass, candidate substitution, or output mutation |
| `finalize_result` | Current opaque `candidate_id` | Fixed result basename | `QA`; current candidate; verification passed; no blocking QA errors | Copies the verified candidate to the deliverable area and transitions to `COMPLETED` or `COMPLETED_WITH_WARNINGS` | No path, basename, unverified candidate, terminal-state override, or direct download authorization |

## Cross-cutting enforcement

- `PiSessionFactory` disables all built-in tools, supplies only the six custom
  tools, and compares the runtime's active tool names with the expected
  allowlist before use.
- Candidate identifiers are opaque `candidate-r0` through `candidate-r3`
  values. Model-facing schemas do not accept filesystem paths.
- The TypeScript sidecar derives job root, job ID, language routing, provider
  settings, timeouts, concurrency, and output names from trusted job/config
  state. The Python bridge adds its fixed job context and uses a shell-free
  process invocation.
- The Python dispatcher has the same six-name allowlist. All request and
  response objects use strict contracts, relative path validation, bounded
  collections, and bounded numeric fields.
- Document text and model-generated prose are untrusted data. They cannot add
  a tool or a parameter. Tool output may still influence the model's next
  choice among already authorized operations, so state, budget, QA, and
  release gates remain authoritative.
- A retained candidate is not automatically a deliverable. The result API
  serves only jobs in `COMPLETED` or `COMPLETED_WITH_WARNINGS`.

## Evidence and limits

The focused tests in
`translation_core/pi_agent/test/security-boundary.test.ts` exercise the
allowlist/schema, workspace path confinement, hard repair cap, closed result
endpoint, and document prompt-injection boundary. Existing tests cover the Pi
active-tool check, Python bridge argv/environment, symlink handling, durable
state validation, and result hash binding.

These tests demonstrate selected implementation behavior. They are not a
complete security audit, penetration test, dependency review, or proof that
all malformed Office documents are safely handled by every native dependency.
