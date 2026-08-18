import { PYTHON_TOOL_NAMES, type JobStatus, type PythonToolName } from "./contracts.js";

export type PublicEvidenceKind =
  | "real_python_core"
  | "scripted_fixture_candidate"
  | "sanitized_runtime_trace";

export interface PublicTraceV1 {
  schema_version: 1;
  trace_id: string;
  generated_at: string;
  document: { format: "doc" | "docx" | "ppt" | "pptx" };
  evidence: {
    kind: PublicEvidenceKind;
    pi_session: boolean;
    model_calls: number;
    ordering: "within_stream";
  };
  terminal: {
    status: JobStatus;
    result_http_status: number | null;
    reason_code: string | null;
  };
  states: Array<{
    sequence: number;
    status: JobStatus;
    elapsed_ms: number | null;
  }>;
  tool_calls: Array<{
    sequence: number;
    tool: PythonToolName;
    outcome: "recorded" | "succeeded" | "failed" | "skipped";
    duration_ms: number | null;
  }>;
  qa_snapshots: Array<{
    sequence: number;
    blocking_count: number;
    quality_score: number | null;
    issue_counts: { error: number; warning: number; info: number } | null;
  }>;
  candidates: Array<{
    sequence: number;
    candidate_id: string;
    sha256: string;
    selected: boolean;
    released: boolean;
  }>;
  usage: {
    input_tokens: number | null;
    output_tokens: number | null;
    total_tokens: number | null;
    cost_usd: number | null;
    latency_ms: number | null;
  };
}

export function parsePublicTrace(value: unknown): PublicTraceV1 {
  const root = exactRecord(value, "public trace", [
    "schema_version",
    "trace_id",
    "generated_at",
    "document",
    "evidence",
    "terminal",
    "states",
    "tool_calls",
    "qa_snapshots",
    "candidates",
    "usage",
  ]);
  if (root.schema_version !== 1) throw new Error("public trace schema_version must be 1");
  const document = exactRecord(root.document, "document", ["format"]);
  const evidence = exactRecord(root.evidence, "evidence", ["kind", "pi_session", "model_calls", "ordering"]);
  const terminal = exactRecord(root.terminal, "terminal", ["status", "result_http_status", "reason_code"]);
  const usage = exactRecord(root.usage, "usage", [
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cost_usd",
    "latency_ms",
  ]);

  const states = parseArray(root.states, "states", 256, (item, index) => {
    const state = exactRecord(item, `states[${index}]`, ["sequence", "status", "elapsed_ms"]);
    assertSequence(state.sequence, index, `states[${index}].sequence`);
    return {
      sequence: index,
      status: enumValue(state.status, JOB_STATUSES, `states[${index}].status`),
      elapsed_ms: nullableInteger(state.elapsed_ms, 0, MAX_DURATION_MS, `states[${index}].elapsed_ms`),
    };
  });
  if (states.length === 0) throw new Error("states must contain at least one item");

  const toolCalls = parseArray(root.tool_calls, "tool_calls", 256, (item, index) => {
    const tool = exactRecord(item, `tool_calls[${index}]`, ["sequence", "tool", "outcome", "duration_ms"]);
    assertSequence(tool.sequence, index, `tool_calls[${index}].sequence`);
    return {
      sequence: index,
      tool: enumValue(tool.tool, PYTHON_TOOL_NAMES, `tool_calls[${index}].tool`),
      outcome: enumValue(tool.outcome, TOOL_OUTCOMES, `tool_calls[${index}].outcome`),
      duration_ms: nullableInteger(tool.duration_ms, 0, MAX_DURATION_MS, `tool_calls[${index}].duration_ms`),
    };
  });

  const qaSnapshots = parseArray(root.qa_snapshots, "qa_snapshots", 128, (item, index) => {
    const qa = exactRecord(item, `qa_snapshots[${index}]`, [
      "sequence",
      "blocking_count",
      "quality_score",
      "issue_counts",
    ]);
    assertSequence(qa.sequence, index, `qa_snapshots[${index}].sequence`);
    const issueCounts = qa.issue_counts === null ? null : parseIssueCounts(qa.issue_counts, index);
    return {
      sequence: index,
      blocking_count: integer(qa.blocking_count, 0, MAX_COUNT, `qa_snapshots[${index}].blocking_count`),
      quality_score: nullableNumber(qa.quality_score, 0, 100, `qa_snapshots[${index}].quality_score`),
      issue_counts: issueCounts,
    };
  });

  const candidates = parseArray(root.candidates, "candidates", 128, (item, index) => {
    const candidate = exactRecord(item, `candidates[${index}]`, [
      "sequence",
      "candidate_id",
      "sha256",
      "selected",
      "released",
    ]);
    assertSequence(candidate.sequence, index, `candidates[${index}].sequence`);
    return {
      sequence: index,
      candidate_id: safeIdentifier(candidate.candidate_id, `candidates[${index}].candidate_id`),
      sha256: sha256(candidate.sha256, `candidates[${index}].sha256`),
      selected: boolean(candidate.selected, `candidates[${index}].selected`),
      released: boolean(candidate.released, `candidates[${index}].released`),
    };
  });
  if (candidates.length === 0) throw new Error("candidates must contain at least one item");
  const terminalStatus = enumValue(terminal.status, TERMINAL_STATUSES, "terminal.status");
  const resultHttpStatus = nullableInteger(terminal.result_http_status, 100, 599, "terminal.result_http_status");
  const lastState = states.at(-1);
  if (lastState?.status !== terminalStatus) throw new Error("the last state must match terminal.status");
  if (candidates.some((candidate) => candidate.released && !candidate.selected)) {
    throw new Error("a released candidate must be selected");
  }
  if (candidates.filter((candidate) => candidate.selected).length > 1) {
    throw new Error("at most one candidate may be selected");
  }
  const releasedCount = candidates.filter((candidate) => candidate.released).length;
  if (terminalStatus === "COMPLETED" || terminalStatus === "COMPLETED_WITH_WARNINGS") {
    if (releasedCount !== 1 || resultHttpStatus !== 200) {
      throw new Error("a completed trace must release exactly one selected candidate with HTTP 200");
    }
    assertCompletedGateEvidence(states, toolCalls, qaSnapshots);
  } else if (releasedCount !== 0) {
    throw new Error(`${terminalStatus} traces must not release a candidate`);
  }
  if (terminalStatus === "NEEDS_REVIEW" && resultHttpStatus !== 409) {
    throw new Error("a NEEDS_REVIEW trace must record HTTP 409");
  }

  const inputTokens = nullableInteger(usage.input_tokens, 0, MAX_TOKENS, "usage.input_tokens");
  const outputTokens = nullableInteger(usage.output_tokens, 0, MAX_TOKENS, "usage.output_tokens");
  const totalTokens = nullableInteger(usage.total_tokens, 0, MAX_TOKENS, "usage.total_tokens");
  const tokenValues = [inputTokens, outputTokens, totalTokens];
  if (tokenValues.some((item) => item === null) && tokenValues.some((item) => item !== null)) {
    throw new Error("usage token metrics must either all be recorded or all be null");
  }
  if (inputTokens !== null && outputTokens !== null && totalTokens !== inputTokens + outputTokens) {
    throw new Error("usage.total_tokens must equal input_tokens + output_tokens");
  }

  return {
    schema_version: 1,
    trace_id: safeIdentifier(root.trace_id, "trace_id"),
    generated_at: isoTimestamp(root.generated_at, "generated_at"),
    document: { format: enumValue(document.format, DOCUMENT_FORMATS, "document.format") },
    evidence: {
      kind: enumValue(evidence.kind, EVIDENCE_KINDS, "evidence.kind"),
      pi_session: boolean(evidence.pi_session, "evidence.pi_session"),
      model_calls: integer(evidence.model_calls, 0, MAX_COUNT, "evidence.model_calls"),
      ordering: enumValue(evidence.ordering, ORDERINGS, "evidence.ordering"),
    },
    terminal: {
      status: terminalStatus,
      result_http_status: resultHttpStatus,
      reason_code: terminal.reason_code === null ? null : safeIdentifier(terminal.reason_code, "terminal.reason_code"),
    },
    states,
    tool_calls: toolCalls,
    qa_snapshots: qaSnapshots,
    candidates,
    usage: {
      input_tokens: inputTokens,
      output_tokens: outputTokens,
      total_tokens: totalTokens,
      cost_usd: nullableNumber(usage.cost_usd, 0, MAX_COST_USD, "usage.cost_usd"),
      latency_ms: nullableInteger(usage.latency_ms, 0, MAX_DURATION_MS, "usage.latency_ms"),
    },
  };
}

export const MAX_PUBLIC_TRACE_JSON_BYTES = 1_048_576;

export function parsePublicTraceJson(text: string): PublicTraceV1 {
  if (typeof text !== "string") throw new Error("public trace JSON must be text");
  if (text.length > MAX_PUBLIC_TRACE_JSON_BYTES || new TextEncoder().encode(text).byteLength > MAX_PUBLIC_TRACE_JSON_BYTES) {
    throw new Error(`public trace JSON exceeds the ${MAX_PUBLIC_TRACE_JSON_BYTES} byte size limit`);
  }
  let value: unknown;
  try {
    value = JSON.parse(text) as unknown;
  } catch {
    throw new Error("public trace is not valid JSON");
  }
  return parsePublicTrace(value);
}

/** A deterministic, text-only replay suitable for terminals and snapshot evidence. */
export function formatPublicTraceReplay(traceInput: PublicTraceV1): string {
  const trace = parsePublicTrace(traceInput);
  const lines = [
    `TRACE ${trace.trace_id} schema_version=${trace.schema_version} generated_at=${trace.generated_at}`,
    `EVIDENCE kind=${trace.evidence.kind} pi_session=${String(trace.evidence.pi_session)} model_calls=${trace.evidence.model_calls} ordering=${trace.evidence.ordering}`,
    `DOCUMENT format=${trace.document.format}`,
  ];
  for (const [index, state] of trace.states.entries()) {
    lines.push(`STATE ${index + 1}/${trace.states.length} ${state.status} elapsed_ms=${recorded(state.elapsed_ms)}`);
  }
  for (const [index, call] of trace.tool_calls.entries()) {
    lines.push(
      `TOOL ${index + 1}/${trace.tool_calls.length} ${call.tool} outcome=${call.outcome} duration_ms=${recorded(call.duration_ms)}`,
    );
  }
  for (const [index, qa] of trace.qa_snapshots.entries()) {
    const issues = qa.issue_counts === null
      ? "not recorded"
      : `error:${qa.issue_counts.error},warning:${qa.issue_counts.warning},info:${qa.issue_counts.info}`;
    lines.push(
      `QA ${index + 1}/${trace.qa_snapshots.length} blockers=${qa.blocking_count} quality=${recorded(qa.quality_score)} issues=${issues}`,
    );
  }
  for (const [index, candidate] of trace.candidates.entries()) {
    lines.push(
      `CANDIDATE ${index + 1}/${trace.candidates.length} ${candidate.candidate_id} sha256=${candidate.sha256} selected=${String(candidate.selected)} released=${String(candidate.released)}`,
    );
  }
  lines.push(
    `USAGE input_tokens=${recorded(trace.usage.input_tokens)} output_tokens=${recorded(trace.usage.output_tokens)} total_tokens=${recorded(trace.usage.total_tokens)} cost_usd=${recorded(trace.usage.cost_usd)} latency_ms=${recorded(trace.usage.latency_ms)}`,
  );
  lines.push(
    `TERMINAL ${trace.terminal.status} http=${recorded(trace.terminal.result_http_status)} reason=${recorded(trace.terminal.reason_code)}`,
  );
  return `${lines.join("\n")}\n`;
}

/**
 * Allowlist projection for the fixed offline P1-A evidence shape.
 *
 * The input is intentionally `unknown`: callers may pass a richer internal trace,
 * but only bounded operational fields are copied into the public artifact.
 */
export function projectP1aScenarioToPublicTrace(value: unknown, generatedAt: string): PublicTraceV1 {
  const source = projectionRecord(value, "P1-A scenario");
  const scenarioId = enumValue(source.scenario_id, P1A_SCENARIOS, "scenario_id");
  if (source.pi_session !== false || source.model_calls !== 0) {
    throw new Error("P1-A offline evidence must declare pi_session=false and model_calls=0");
  }
  const terminalStatus = enumValue(source.terminal_status, P1A_TERMINAL_STATUSES, "terminal_status");
  const resultHttpStatus = integer(source.result_http_status, 100, 599, "result_http_status");
  const blockingCounts = projectionArray(source.blocking_counts, "blocking_counts", 128, (item, index) =>
    integer(item, 0, MAX_COUNT, `blocking_counts[${index}]`));
  const qualityScores = projectionArray(source.quality_scores, "quality_scores", 128, (item, index) =>
    number(item, 0, 100, `quality_scores[${index}]`));
  if (blockingCounts.length === 0 || blockingCounts.length !== qualityScores.length) {
    throw new Error("P1-A blocking_counts and quality_scores must be non-empty aligned arrays");
  }
  const selectedHash = sha256(source.selected_candidate_sha256, "selected_candidate_sha256");
  const resultHash = source.result_sha256 === null ? null : sha256(source.result_sha256, "result_sha256");
  if (resultHash !== null && resultHash !== selectedHash) {
    throw new Error("P1-A result_sha256 must match the selected candidate");
  }
  const stateTransitions = projectionArray(source.state_transitions, "state_transitions", 256, (item, index) =>
    enumValue(item, JOB_STATUSES, `state_transitions[${index}]`));
  const toolSequence = projectionArray(source.tool_sequence, "tool_sequence", 256, (item, index) =>
    enumValue(item, PYTHON_TOOL_NAMES, `tool_sequence[${index}]`));
  assertExactSequence(
    stateTransitions,
    P1A_STATE_SEQUENCES[scenarioId],
    `fixed P1-A state sequence for ${scenarioId}`,
  );
  assertExactSequence(
    toolSequence,
    P1A_TOOL_SEQUENCES[scenarioId],
    `fixed P1-A tool sequence for ${scenarioId}`,
  );

  return parsePublicTrace({
    schema_version: 1,
    trace_id: `p1a-${scenarioId}`,
    generated_at: isoTimestamp(generatedAt, "generated_at"),
    document: { format: scenarioId === "direct-docx" ? "docx" : "pptx" },
    evidence: {
      kind: enumValue(source.evidence_kind, P1A_EVIDENCE_KINDS, "evidence_kind"),
      pi_session: false,
      model_calls: 0,
      ordering: "within_stream",
    },
    terminal: {
      status: terminalStatus,
      result_http_status: resultHttpStatus,
      reason_code: p1aReasonCode(source.repair_decisions),
    },
    states: stateTransitions.map((status, sequence) => ({ sequence, status, elapsed_ms: null })),
    tool_calls: toolSequence.map((tool, sequence) => ({
      sequence,
      tool,
      outcome: "recorded",
      duration_ms: null,
    })),
    qa_snapshots: blockingCounts.map((blockingCount, sequence) => ({
      sequence,
      blocking_count: blockingCount,
      quality_score: qualityScores[sequence]!,
      issue_counts: null,
    })),
    candidates: [{
      sequence: 0,
      candidate_id: safeIdentifier(source.selected_candidate_id, "selected_candidate_id"),
      sha256: selectedHash,
      selected: true,
      released: resultHash !== null,
    }],
    usage: {
      input_tokens: null,
      output_tokens: null,
      total_tokens: null,
      cost_usd: null,
      latency_ms: integer(source.elapsed_ms, 0, MAX_DURATION_MS, "elapsed_ms"),
    },
  });
}

export function projectP1aDemoToPublicTraces(value: unknown, generatedAt: string): PublicTraceV1[] {
  const source = projectionRecord(value, "P1-A demo");
  if (source.schema_version !== 1 || source.demo !== "p1a-three-path-offline") {
    throw new Error("P1-A demo header is invalid");
  }
  if (source.pi_session !== false || source.model_calls !== 0) {
    throw new Error("P1-A offline demo must declare pi_session=false and model_calls=0");
  }
  const scenarios = projectionArray(source.scenarios, "scenarios", 128, (scenario) =>
    projectP1aScenarioToPublicTrace(scenario, generatedAt));
  if (integer(source.scenario_count, 1, 128, "scenario_count") !== 3 || scenarios.length !== 3) {
    throw new Error("P1-A three-path evidence must contain exactly three scenarios");
  }
  const expectedTraceIds = P1A_SCENARIOS.map((scenarioId) => `p1a-${scenarioId}`);
  if (scenarios.some((scenario, index) => scenario.trace_id !== expectedTraceIds[index])) {
    throw new Error("P1-A scenarios must use the fixed order: direct-docx, repair-pptx, high-risk-pptx");
  }
  return scenarios;
}

const DOCUMENT_FORMATS = ["doc", "docx", "ppt", "pptx"] as const;
const EVIDENCE_KINDS = ["real_python_core", "scripted_fixture_candidate", "sanitized_runtime_trace"] as const;
const ORDERINGS = ["within_stream"] as const;
const TOOL_OUTCOMES = ["recorded", "succeeded", "failed", "skipped"] as const;
const JOB_STATUSES = [
  "QUEUED",
  "PREFLIGHT",
  "TRANSLATING",
  "QA",
  "REPAIRING",
  "VERIFYING",
  "COMPLETED",
  "COMPLETED_WITH_WARNINGS",
  "NEEDS_REVIEW",
  "FAILED",
  "CANCELLED",
] as const satisfies readonly JobStatus[];
const TERMINAL_STATUSES = ["COMPLETED", "COMPLETED_WITH_WARNINGS", "NEEDS_REVIEW", "FAILED", "CANCELLED"] as const;
const P1A_SCENARIOS = ["direct-docx", "repair-pptx", "high-risk-pptx"] as const;
const P1A_STATE_SEQUENCES = {
  "direct-docx": ["PREFLIGHT", "TRANSLATING", "QA", "VERIFYING", "QA", "COMPLETED"],
  "repair-pptx": ["PREFLIGHT", "QA", "REPAIRING", "QA", "VERIFYING", "QA", "COMPLETED"],
  "high-risk-pptx": ["PREFLIGHT", "QA", "REPAIRING", "QA", "REPAIRING", "QA", "NEEDS_REVIEW"],
} as const satisfies Record<(typeof P1A_SCENARIOS)[number], readonly JobStatus[]>;
const P1A_TOOL_SEQUENCES = {
  "direct-docx": ["preflight_document", "translate_once", "run_document_qa", "verify_candidate", "finalize_result"],
  "repair-pptx": ["preflight_document", "run_document_qa", "apply_repair", "run_document_qa", "verify_candidate", "finalize_result"],
  "high-risk-pptx": ["preflight_document", "run_document_qa", "apply_repair", "run_document_qa", "apply_repair", "run_document_qa"],
} as const satisfies Record<(typeof P1A_SCENARIOS)[number], readonly PythonToolName[]>;
const P1A_EVIDENCE_KINDS = ["real_python_core", "scripted_fixture_candidate"] as const;
const P1A_TERMINAL_STATUSES = ["COMPLETED", "NEEDS_REVIEW"] as const;
const SAFE_IDENTIFIER = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;
const SHA256 = /^[a-f0-9]{64}$/;
const MAX_COUNT = 1_000_000;
const MAX_TOKENS = 1_000_000_000_000;
const MAX_COST_USD = 1_000_000;
const MAX_DURATION_MS = 31_536_000_000;

function exactRecord(value: unknown, label: string, allowedKeys: readonly string[]): Record<string, unknown> {
  if (value === null || typeof value !== "object" || Array.isArray(value)) throw new Error(`${label} must be an object`);
  const prototype = Object.getPrototypeOf(value);
  if (prototype !== Object.prototype && prototype !== null) throw new Error(`${label} must be a plain object`);
  const allowed = new Set(allowedKeys);
  for (const key of Object.keys(value)) {
    if (!allowed.has(key)) throw new Error(`${label} contains unknown field ${JSON.stringify(key)}`);
    const descriptor = Object.getOwnPropertyDescriptor(value, key);
    if (descriptor?.get || descriptor?.set) throw new Error(`${label}.${key} must be a data field`);
  }
  for (const key of allowedKeys) {
    if (!Object.hasOwn(value, key)) throw new Error(`${label} is missing required field ${JSON.stringify(key)}`);
  }
  return value as Record<string, unknown>;
}

function parseArray<T>(
  value: unknown,
  label: string,
  maximum: number,
  parseItem: (item: unknown, index: number) => T,
): T[] {
  if (!Array.isArray(value)) throw new Error(`${label} must be an array`);
  if (value.length > maximum) throw new Error(`${label} exceeds the ${maximum} item limit`);
  return value.map(parseItem);
}

function projectionRecord(value: unknown, label: string): Record<string, unknown> {
  if (value === null || typeof value !== "object" || Array.isArray(value)) throw new Error(`${label} must be an object`);
  const prototype = Object.getPrototypeOf(value);
  if (prototype !== Object.prototype && prototype !== null) throw new Error(`${label} must be a plain object`);
  for (const key of Object.keys(value)) {
    const descriptor = Object.getOwnPropertyDescriptor(value, key);
    if (descriptor?.get || descriptor?.set) throw new Error(`${label}.${key} must be a data field`);
  }
  return value as Record<string, unknown>;
}

function projectionArray<T>(
  value: unknown,
  label: string,
  maximum: number,
  parseItem: (item: unknown, index: number) => T,
): T[] {
  return parseArray(value, label, maximum, parseItem);
}

function p1aReasonCode(value: unknown): string | null {
  if (value === undefined) return null;
  const decisions = projectionArray(value, "repair_decisions", 128, (item, index) => {
    if (typeof item !== "string" || item.length > 128) throw new Error(`repair_decisions[${index}] is invalid`);
    return item;
  });
  const stop = decisions.findLast((decision) => decision.startsWith("stop:"));
  return stop ? safeIdentifier(stop.slice("stop:".length), "repair decision reason") : null;
}

function parseIssueCounts(value: unknown, index: number): { error: number; warning: number; info: number } {
  const label = `qa_snapshots[${index}].issue_counts`;
  const counts = exactRecord(value, label, ["error", "warning", "info"]);
  return {
    error: integer(counts.error, 0, MAX_COUNT, `${label}.error`),
    warning: integer(counts.warning, 0, MAX_COUNT, `${label}.warning`),
    info: integer(counts.info, 0, MAX_COUNT, `${label}.info`),
  };
}

function assertCompletedGateEvidence(
  states: Array<{ status: JobStatus }>,
  toolCalls: Array<{ tool: PythonToolName; outcome: "recorded" | "succeeded" | "failed" | "skipped" }>,
  qaSnapshots: Array<{ blocking_count: number }>,
): void {
  if (qaSnapshots.length === 0) throw new Error("a completed trace requires at least one QA snapshot");
  if (qaSnapshots.at(-1)!.blocking_count !== 0) {
    throw new Error("a completed trace requires a final QA snapshot with zero blocking issues");
  }
  const toolNames = toolCalls.map(({ tool }) => tool);
  const finalizeIndex = toolNames.lastIndexOf("finalize_result");
  const verifyIndex = toolNames.lastIndexOf("verify_candidate");
  const qaToolIndex = toolNames.slice(0, verifyIndex).lastIndexOf("run_document_qa");
  if (
    finalizeIndex !== toolNames.length - 1
    || verifyIndex < 0
    || verifyIndex !== finalizeIndex - 1
    || qaToolIndex < 0
  ) {
    throw new Error(
      "a completed trace requires run_document_qa before verify_candidate and final finalize_result evidence",
    );
  }
  const candidateMutationAfterQa = toolNames
    .slice(qaToolIndex + 1, verifyIndex)
    .some((tool) => (
      tool === "preflight_document"
      || tool === "translate_once"
      || tool === "apply_repair"
    ));
  if (candidateMutationAfterQa) {
    throw new Error("a completed trace requires fresh QA after the last candidate mutation");
  }
  if (![qaToolIndex, verifyIndex, finalizeIndex].every((index) => {
    const outcome = toolCalls[index]?.outcome;
    return outcome === "recorded" || outcome === "succeeded";
  })) {
    throw new Error("a completed trace release-gate tool evidence must be successful");
  }
  const stateNames = states.map(({ status }) => status);
  const verifyingIndex = stateNames.lastIndexOf("VERIFYING");
  const statesBeforeVerifying = stateNames.slice(0, verifyingIndex);
  const qaStateIndex = statesBeforeVerifying.lastIndexOf("QA");
  if (verifyingIndex < 0 || qaStateIndex < 0 || qaStateIndex >= verifyingIndex) {
    throw new Error("a completed trace requires QA before VERIFYING state evidence");
  }
  const candidateMutationStateAfterQa = statesBeforeVerifying
    .slice(qaStateIndex + 1)
    .some((status) => status === "TRANSLATING" || status === "REPAIRING");
  if (candidateMutationStateAfterQa) {
    throw new Error("a completed trace requires fresh QA state after the last mutation");
  }
}

function assertExactSequence<T>(actual: readonly T[], expected: readonly T[], label: string): void {
  if (actual.length !== expected.length || actual.some((value, index) => value !== expected[index])) {
    throw new Error(`${label} does not match the recorded demo contract`);
  }
}

function assertSequence(value: unknown, expected: number, label: string): void {
  if (value !== expected) throw new Error(`${label} must be ${expected}`);
}

function enumValue<const T extends readonly string[]>(value: unknown, allowed: T, label: string): T[number] {
  if (typeof value !== "string" || !allowed.includes(value as T[number])) {
    throw new Error(`${label} is not an allowed value`);
  }
  return value as T[number];
}

function safeIdentifier(value: unknown, label: string): string {
  if (typeof value !== "string" || !SAFE_IDENTIFIER.test(value)) throw new Error(`${label} is not a safe identifier`);
  return value;
}

function sha256(value: unknown, label: string): string {
  if (typeof value !== "string" || !SHA256.test(value)) throw new Error(`${label} must be a lowercase SHA-256 digest`);
  return value;
}

function isoTimestamp(value: unknown, label: string): string {
  if (typeof value !== "string" || value.length > 32) throw new Error(`${label} must be an ISO-8601 timestamp`);
  const parsed = new Date(value);
  if (!Number.isFinite(parsed.getTime()) || parsed.toISOString() !== value) {
    throw new Error(`${label} must be a canonical UTC timestamp`);
  }
  return value;
}

function boolean(value: unknown, label: string): boolean {
  if (typeof value !== "boolean") throw new Error(`${label} must be a boolean`);
  return value;
}

function integer(value: unknown, minimum: number, maximum: number, label: string): number {
  if (!Number.isSafeInteger(value) || Number(value) < minimum || Number(value) > maximum) {
    throw new Error(`${label} must be an integer between ${minimum} and ${maximum}`);
  }
  return Number(value);
}

function nullableInteger(value: unknown, minimum: number, maximum: number, label: string): number | null {
  return value === null ? null : integer(value, minimum, maximum, label);
}

function number(value: unknown, minimum: number, maximum: number, label: string): number {
  if (typeof value !== "number" || !Number.isFinite(value) || value < minimum || value > maximum) {
    throw new Error(`${label} must be a finite number between ${minimum} and ${maximum}`);
  }
  return value;
}

function nullableNumber(value: unknown, minimum: number, maximum: number, label: string): number | null {
  return value === null ? null : number(value, minimum, maximum, label);
}

function recorded(value: string | number | null): string {
  return value === null ? "not recorded" : String(value);
}
