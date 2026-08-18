# Execution Modes

Persist exactly one mode before preflight. Only `pi_agent_live` and
`pretranslated_qa` are valid.

## Decision Table

| Evidence | Mode | Translation action | QA action |
| --- | --- | --- | --- |
| The task requests a new translation and server policy authorizes the configured provider route | `pi_agent_live` | Call `translate_once` and create `candidate-r0` | Run the selected profile, then bounded repair if needed |
| The task supplies both an original Office file and an existing translated Office file | `pretranslated_qa` | None | Register the supplied file as `candidate-r0`, then run QA/repair |

Reject ambiguous requests instead of guessing whether an existing file is a
source or candidate. A mode is part of the immutable task specification.

## `pi_agent_live`

Use the server-owned third-party translation route. Record:

```json
{
  "execution_mode": "pi_agent_live",
  "translation_provenance": "configured_provider",
  "external_translation_api": true,
  "quality_profile": "standard",
  "true_translation_seconds": 0
}
```

The adapter may report a logical route/model identifier, request IDs, retries,
and usage. It must never expose the endpoint secret. Pi may choose workflow
actions but may not replace the configured translator, submit provider
credentials, or translate document blocks in the conversation.

`true_translation_seconds` measures the completed `translate_once` stage,
including provider calls, protected-token restoration, Office writeback, and
candidate packaging. Keep provider service-time sums separate because
concurrent calls overlap.

## `pretranslated_qa`

Require a source and a corresponding candidate of the same Office family:

- DOC/DOCX source with DOCX candidate;
- PPT/PPTX source with PPTX candidate.

Record:

```json
{
  "execution_mode": "pretranslated_qa",
  "translation_provenance": "pretranslated",
  "external_translation_api": false,
  "quality_profile": "standard",
  "true_translation_seconds": null
}
```

Do not call `translate_once`. A location-scoped retranslation may occur only as
a registered repair action when the task policy authorizes the server-owned
provider; record it as repair time and provider usage, not original translation
time.

Never estimate translation time from QA duration, file timestamps, queue time,
or conversation time.

## Mode Transitions

Do not silently change mode. If a different mode is required:

1. Stop the current job before a new translation effect.
2. Preserve source, candidate, ledger, and timing evidence.
3. Record the reason and authorization.
4. Create a new job/attempt with a new idempotency scope.

Do not mix timing or provider provenance across the two jobs.
