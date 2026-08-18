import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { parsePublicTrace } from "../trace-schema.mjs";
import { MISSING_LABEL, buildTraceViewModel } from "../render-model.mjs";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const EXAMPLES = ["p1a-direct-docx.json", "p1a-repair-pptx.json", "p1a-high-risk-pptx.json"];

async function loadExample(filename) {
  return parsePublicTrace(JSON.parse(await readFile(path.join(ROOT, "examples", filename), "utf8")));
}

test("checked-in viewer examples are schema-v1 offline evidence, not claimed model calls", async () => {
  const traces = await Promise.all(EXAMPLES.map(loadExample));

  assert.deepEqual(traces.map((trace) => trace.schema_version), [1, 1, 1]);
  assert.deepEqual(traces.map((trace) => trace.evidence.pi_session), [false, false, false]);
  assert.deepEqual(traces.map((trace) => trace.evidence.model_calls), [0, 0, 0]);
  assert.deepEqual(traces.map((trace) => trace.terminal.status), ["COMPLETED", "COMPLETED", "NEEDS_REVIEW"]);
});

test("DOM-independent view model marks absent measurements and keeps auditable evidence", async () => {
  const trace = await loadExample("p1a-high-risk-pptx.json");
  const model = buildTraceViewModel(trace);

  assert.equal(model.metrics.inputTokens, MISSING_LABEL);
  assert.equal(model.metrics.costUsd, MISSING_LABEL);
  assert.equal(model.metrics.latencyMs, "22488 ms");
  assert.equal(model.terminal.status, "NEEDS_REVIEW");
  assert.equal(model.terminal.reason, "budget_exhausted");
  assert.equal(model.candidates[0].sha256, "631e3c04bbcb148cfb99e77936fca6eded184765d444d54ef88fc41089a212a4");
  assert.equal(model.candidates[0].released, "No");
});

test("viewer parser rejects unauthorized fields and active-markup identifiers", async () => {
  const trace = await loadExample("p1a-direct-docx.json");

  assert.throws(() => parsePublicTrace({ ...trace, prompt: "steal secrets" }), /unknown field/i);
  assert.throws(() => parsePublicTrace({ ...trace, trace_id: "<img src=x onerror=alert(1)>" }), /safe identifier/i);
  assert.throws(
    () => parsePublicTrace({ ...trace, candidates: [{ ...trace.candidates[0], absolute_path: "C:\\private.docx" }] }),
    /unknown field/i,
  );
  assert.throws(
    () => parsePublicTrace({
      ...trace,
      terminal: { status: "NEEDS_REVIEW", result_http_status: 409, reason_code: "budget_exhausted" },
      states: trace.states.map((state, index) => index === trace.states.length - 1 ? { ...state, status: "NEEDS_REVIEW" } : state),
    }),
    /must not release/i,
  );
  assert.throws(
    () => parsePublicTrace({ ...trace, tool_calls: [], qa_snapshots: [] }),
    /completed trace.*QA snapshot/i,
  );
  assert.throws(
    () => parsePublicTrace({
      ...trace,
      tool_calls: trace.tool_calls.map((call, index) =>
        index === trace.tool_calls.length - 1 ? { ...call, outcome: "failed" } : call),
    }),
    /release-gate tool evidence.*successful/i,
  );
  const staleTools = trace.tool_calls.map((call) => ({ ...call }));
  staleTools.splice(3, 0, {
    sequence: 3,
    tool: "apply_repair",
    outcome: "recorded",
    duration_ms: null,
  });
  staleTools.forEach((call, sequence) => { call.sequence = sequence; });
  assert.throws(
    () => parsePublicTrace({ ...trace, tool_calls: staleTools }),
    /fresh QA.*candidate mutation/i,
  );
});

test("browser renderer uses text nodes and exposes labelled keyboard-native controls", async () => {
  const [appSource, html] = await Promise.all([
    readFile(path.join(ROOT, "app.mjs"), "utf8"),
    readFile(path.join(ROOT, "index.html"), "utf8"),
  ]);

  assert.doesNotMatch(appSource, /\.innerHTML\s*=|insertAdjacentHTML|document\.write/);
  assert.match(appSource, /\.textContent\s*=/);
  assert.match(html, /<label[^>]+for="example-select"/);
  assert.match(html, /<label[^>]+for="trace-file"/);
  assert.match(html, /aria-live="polite"/);
  assert.match(html, /<button[^>]+id="load-example"/);
  assert.match(html, /<link rel="icon" href="data:,">/);
});

test("mobile loader fields reset the desktop flex basis instead of becoming tall controls", async () => {
  const styles = await readFile(path.join(ROOT, "styles.css"), "utf8");

  assert.match(
    styles,
    /@media \(max-width: 700px\)[\s\S]*?\.field--grow,\s*\.field--file\s*{\s*flex:\s*0 0 auto;/,
  );
});
