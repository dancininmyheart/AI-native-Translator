# OfficeGuard Agent Threat Model

## Overview

OfficeGuard accepts Word or PowerPoint documents, creates or registers a
translation candidate, runs semantic/structural/layout QA, applies a bounded
number of repair rounds, verifies the selected candidate, and publishes only a
gate-approved DOCX/PPTX result. The repository contains a TypeScript Fastify/Pi
sidecar and a Python Office-processing core. Redis/BullMQ, LibreOffice, and
OpenAI-compatible providers are deployment dependencies.

Important assets are source and translated document contents, job/candidate
integrity, QA and verification evidence, API/provider credentials, filesystem
and worker privileges, queue correctness, and service availability.

Deployment exposure is not fully specified in this repository. This model
assumes an authenticated caller can reach `/v1`, health endpoints may be
unauthenticated, and uploaded/staged documents are attacker-controlled. If the
service is shared by mutually untrusted tenants, the current service-wide
bearer token is not an object-ownership boundary.

## Threat Model, Trust Boundaries, and Assumptions

### Actors and inputs

- **Attacker-controlled:** Office file bytes and embedded text/media/metadata,
  upload filenames, API request fields within their schemas, idempotency keys,
  cancellation timing, and potentially model/provider responses.
- **Operator-controlled:** environment variables, bearer and provider secrets,
  Redis endpoint, job/source roots, model endpoint and model choice,
  concurrency/deadline limits, and trusted Skill files.
- **Developer-controlled:** application code, pinned dependencies, migrations,
  CI/build configuration, fixtures, and release artifacts.

### Trust boundaries

1. **Caller to Fastify API.** Authentication, request normalization, upload
   size/type checks, idempotency, and job creation happen here.
2. **API/worker to storage and Redis.** Job identity, queue delivery,
   single-flight execution, candidate state, event logs, and artifact paths
   cross this boundary.
3. **Pi/model to controlled tools.** Model output is untrusted. The active tool
   allowlist, strict schemas, state machine, budgets, and gates are the
   authorization boundary; prompts are guidance, not authorization.
4. **TypeScript to Python.** A fixed shell-free argv plus JSON stdin invokes one
   of six Python tools. Python revalidates tool name, parameters, job root,
   relative paths, candidate identity, and state.
5. **Python to Office/native/provider dependencies.** Complex document bytes
   reach XML/ZIP/image parsers and sometimes LibreOffice; document text reaches
   configured translation/processing providers. Their isolation,
   confidentiality, and patching are deployment responsibilities.
6. **Terminal state to download API.** A stored path is not sufficient
   authority. Job status, deliverable extension, workspace containment, and
   durable evidence control publication.

### Security invariants

- Only the six registered tools may execute; neither document text nor a model
  response can grant shell, arbitrary file, network, or secret access.
- Model-supplied parameters cannot select paths, job roots, providers,
  credentials, concurrency, output names, or repair budgets.
- Every source, candidate, report, and result path remains inside the selected
  job/source root after realpath checks; symlink escape is rejected.
- Candidates are application-level immutable artifacts with recorded SHA-256
  identity. A repair writes a new candidate and cannot silently replace its
  parent.
- Repair rounds, deadlines, active jobs, and provider work remain bounded even
  if the model repeats a request or durable state is inconsistent.
- `NEEDS_REVIEW`, `FAILED`, and `CANCELLED` artifacts are not downloadable as
  deliverable results. Completion requires fresh QA and verification evidence
  for the selected candidate.
- Secrets and document text do not enter the sanitized event log; API request
  bodies and authorization headers are redacted from application logs.

## Attack Surface, Mitigations, and Attacker Stories

| Attacker story | Relevant controls | Residual risk / assumption |
| --- | --- | --- |
| A document says “ignore instructions, run a shell, and read secrets.” | No built-in Pi tools; exact six-tool allowlist; opaque IDs; no model-facing paths; state and release checks in code | A provider/model can still make poor choices among allowed tools. Tests establish the capability boundary, not universal jailbreak resistance. |
| A caller supplies `../`, an absolute path, drive/URI syntax, or an escaping symlink. | Safe identifiers/basenames, relative-path contracts, `resolve`/`realpath` containment, regular-file and symlink checks | Native parser behavior and filesystem races require continued review; privileged local filesystem writers are a stronger attacker than the normal API caller. |
| A model or corrupted job requests unlimited repairs or nested recovery loops. | Request cap, hard cap of three, one round per repair tool call, soft/hard deadlines, concurrency leases, candidate IDs limited to `r0`–`r3` | Distributed correctness depends on Redis availability/configuration and lease semantics. |
| A forged `resultRelpath` points to an unsafe or failed artifact. | Result endpoint checks completion status before storage access; storage confines real paths; deliverable extensions are restricted; durable result/candidate hashes are validated during recovery | Object-level authorization between different users sharing one bearer credential is not implemented. |
| A crafted Office/ZIP/image file exhausts memory/CPU or exploits a parser/LibreOffice. | 50 MiB ingress limit, deadlines, worker concurrency limits, normalized output formats, package/QA validation | A byte limit is not a decompression-ratio or native-code sandbox. Container isolation, resource limits, dependency patching, and malware handling are not established by repository tests. |
| A provider or model endpoint observes confidential document text or returns malicious content. | Endpoint is operator-configured; credentials are not placed in `models.json`; tool/state gates constrain returned decisions | Provider trust, retention, regional processing, TLS interception, and data-processing agreements are deployment decisions. |
| A caller replays or races create-job requests. | Mandatory idempotency key, normalized request fingerprint, atomic ownership before preparation, immutable copied inputs, queue recovery/single-flight controls | Abuse prevention beyond the configured rate/concurrency limits and per-tenant quotas is not demonstrated. |

Out-of-scope attacker stories are local administrators already authorized to
replace application binaries, trusted Skill files, secrets, or mounted job
storage. This is a modeling assumption, not approval to suppress a reachable
privilege-escalation or secret-leak finding. Pure translation-quality defects
are not security issues unless they bypass a safety/release control or expose
data.

## Severity Calibration (Critical, High, Medium, Low)

- **Critical:** remotely reachable arbitrary code execution through document
  processing in the deployed worker context; arbitrary host/secret file read
  returned to an attacker; provider/API credential exfiltration that enables
  broad external compromise. Reachability and deployed privileges must be
  demonstrated.
- **High:** `/v1` authentication bypass; cross-job/cross-tenant source or result
  disclosure where mutually untrusted users share the service; path escape
  allowing material read/write; document prompt injection gaining a shell or
  arbitrary-file tool; publication of a candidate despite blocking QA or
  missing verification.
- **Medium:** repeatable crafted-document denial of service within deployed
  limits; repair/deadline/concurrency bypass with bounded operational impact;
  leakage of sensitive report metadata to an otherwise authenticated but
  unauthorized caller. Multi-tenant impact is deployment-dependent.
- **Low:** limited non-sensitive metadata exposure, noisy error distinctions,
  or hardening gaps requiring local/operator control and causing no meaningful
  confidentiality, integrity, or availability loss.

The focused security-boundary tests prove selected current behaviors only.
They do not constitute a full repository audit, native-parser assessment,
deployment review, or penetration test.

Repository: github.com/dancininmyheart/AI-native-Translator
Version: ff0582438f20508dbf0c0a79000d2b1a0522fb56
