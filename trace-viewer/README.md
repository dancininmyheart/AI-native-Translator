# OfficeGuard public Trace Viewer

A zero-dependency, read-only static viewer for sanitized `schema_version: 1` traces. It displays:

- state transitions and controlled tool-call order;
- QA blocker counts and quality snapshots;
- selected candidate SHA-256 and release decision;
- terminal status and HTTP outcome;
- token, cost, and latency metrics, with missing measurements explicitly labelled **Not recorded**.

The browser never uploads a selected file. Rendering uses DOM text nodes, the parser rejects unknown fields, and the local server applies a restrictive Content Security Policy.

## Run locally

From the repository root:

```powershell
node trace-viewer/serve.mjs
```

Then open <http://127.0.0.1:4173>. An optional port may be passed as the first argument.

## Test

```powershell
node --test trace-viewer/test/viewer.test.mjs
```

The tests validate all bundled samples, missing-metric rendering, strict field rejection, offline evidence labels, and the text-node/accessibility boundary.

## Project and replay traces

Build the TypeScript package first, then run the versioned CLI:

```powershell
Set-Location translation_core/pi_agent
npm run build
node dist/src/public-trace-cli.js project-p1a ../tmp/p1a-demo/demo-trace.json ../../trace-viewer/examples 2026-08-18T00:00:00.000Z
node dist/src/public-trace-cli.js validate ../../trace-viewer/examples/p1a-direct-docx.json
node dist/src/public-trace-cli.js replay ../../trace-viewer/examples/p1a-high-risk-pptx.json
```

`project-p1a` is an allowlist projection: document text, prompts, model responses, credentials, and filesystem paths are never copied. The bundled examples deliberately preserve the honest offline boundary: `pi_session=false` and `model_calls=0`.

## Schema boundary

The public schema accepts only fixed version-1 keys and bounded values. It caps JSON at 1 MiB, restricts tools and states to known enums, requires lowercase SHA-256 digests, and rejects extra nested fields. `ordering=within_stream` means order is authoritative inside each displayed stream; it does not invent cross-stream timestamps absent from the source evidence.
