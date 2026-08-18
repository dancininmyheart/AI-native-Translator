const FORMATS = ["doc", "docx", "ppt", "pptx"];
const EVIDENCE_KINDS = ["real_python_core", "scripted_fixture_candidate", "sanitized_runtime_trace"];
const TERMINAL_STATUSES = ["COMPLETED", "COMPLETED_WITH_WARNINGS", "NEEDS_REVIEW", "FAILED", "CANCELLED"];
const JOB_STATUSES = [
  "QUEUED", "PREFLIGHT", "TRANSLATING", "QA", "REPAIRING", "VERIFYING",
  "COMPLETED", "COMPLETED_WITH_WARNINGS", "NEEDS_REVIEW", "FAILED", "CANCELLED",
];
const TOOLS = [
  "preflight_document", "translate_once", "run_document_qa", "apply_repair", "verify_candidate", "finalize_result",
];
const OUTCOMES = ["recorded", "succeeded", "failed", "skipped"];
const IDENTIFIER = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;
const SHA256 = /^[a-f0-9]{64}$/;
const MAX_COUNT = 1_000_000;
const MAX_TOKENS = 1_000_000_000_000;
const MAX_DURATION_MS = 31_536_000_000;

export const MAX_PUBLIC_TRACE_BYTES = 1_048_576;

export function parsePublicTraceJson(text) {
  if (typeof text !== "string") throw new Error("Public trace JSON must be text");
  if (text.length > MAX_PUBLIC_TRACE_BYTES || new TextEncoder().encode(text).byteLength > MAX_PUBLIC_TRACE_BYTES) {
    throw new Error("Public trace JSON exceeds the size limit");
  }
  let value;
  try {
    value = JSON.parse(text);
  } catch {
    throw new Error("Public trace is not valid JSON");
  }
  return parsePublicTrace(value);
}

export function parsePublicTrace(value) {
  const root = exactRecord(value, "public trace", [
    "schema_version", "trace_id", "generated_at", "document", "evidence", "terminal",
    "states", "tool_calls", "qa_snapshots", "candidates", "usage",
  ]);
  if (root.schema_version !== 1) throw new Error("Public trace schema_version must be 1");
  const document = exactRecord(root.document, "document", ["format"]);
  const evidence = exactRecord(root.evidence, "evidence", ["kind", "pi_session", "model_calls", "ordering"]);
  const terminal = exactRecord(root.terminal, "terminal", ["status", "result_http_status", "reason_code"]);
  const usage = exactRecord(root.usage, "usage", [
    "input_tokens", "output_tokens", "total_tokens", "cost_usd", "latency_ms",
  ]);

  const states = array(root.states, "states", 256, (item, index) => {
    const state = exactRecord(item, `states[${index}]`, ["sequence", "status", "elapsed_ms"]);
    sequence(state.sequence, index, `states[${index}].sequence`);
    return {
      sequence: index,
      status: enumeration(state.status, JOB_STATUSES, `states[${index}].status`),
      elapsed_ms: nullableInteger(state.elapsed_ms, 0, MAX_DURATION_MS, `states[${index}].elapsed_ms`),
    };
  });
  if (states.length === 0) throw new Error("states must contain at least one item");

  const toolCalls = array(root.tool_calls, "tool_calls", 256, (item, index) => {
    const call = exactRecord(item, `tool_calls[${index}]`, ["sequence", "tool", "outcome", "duration_ms"]);
    sequence(call.sequence, index, `tool_calls[${index}].sequence`);
    return {
      sequence: index,
      tool: enumeration(call.tool, TOOLS, `tool_calls[${index}].tool`),
      outcome: enumeration(call.outcome, OUTCOMES, `tool_calls[${index}].outcome`),
      duration_ms: nullableInteger(call.duration_ms, 0, MAX_DURATION_MS, `tool_calls[${index}].duration_ms`),
    };
  });

  const qaSnapshots = array(root.qa_snapshots, "qa_snapshots", 128, (item, index) => {
    const qa = exactRecord(item, `qa_snapshots[${index}]`, [
      "sequence", "blocking_count", "quality_score", "issue_counts",
    ]);
    sequence(qa.sequence, index, `qa_snapshots[${index}].sequence`);
    return {
      sequence: index,
      blocking_count: integer(qa.blocking_count, 0, MAX_COUNT, `qa_snapshots[${index}].blocking_count`),
      quality_score: nullableNumber(qa.quality_score, 0, 100, `qa_snapshots[${index}].quality_score`),
      issue_counts: qa.issue_counts === null ? null : issueCounts(qa.issue_counts, index),
    };
  });

  const candidates = array(root.candidates, "candidates", 128, (item, index) => {
    const candidate = exactRecord(item, `candidates[${index}]`, [
      "sequence", "candidate_id", "sha256", "selected", "released",
    ]);
    sequence(candidate.sequence, index, `candidates[${index}].sequence`);
    return {
      sequence: index,
      candidate_id: identifier(candidate.candidate_id, `candidates[${index}].candidate_id`),
      sha256: digest(candidate.sha256, `candidates[${index}].sha256`),
      selected: boolean(candidate.selected, `candidates[${index}].selected`),
      released: boolean(candidate.released, `candidates[${index}].released`),
    };
  });
  if (candidates.length === 0) throw new Error("candidates must contain at least one item");
  const terminalStatus = enumeration(terminal.status, TERMINAL_STATUSES, "terminal.status");
  const resultHttpStatus = nullableInteger(terminal.result_http_status, 100, 599, "terminal.result_http_status");
  if (states.at(-1)?.status !== terminalStatus) throw new Error("The last state must match terminal.status");
  if (candidates.some((candidate) => candidate.released && !candidate.selected)) {
    throw new Error("A released candidate must be selected");
  }
  if (candidates.filter((candidate) => candidate.selected).length > 1) {
    throw new Error("At most one candidate may be selected");
  }
  const releasedCount = candidates.filter((candidate) => candidate.released).length;
  if (terminalStatus === "COMPLETED" || terminalStatus === "COMPLETED_WITH_WARNINGS") {
    if (releasedCount !== 1 || resultHttpStatus !== 200) {
      throw new Error("A completed trace must release exactly one selected candidate with HTTP 200");
    }
    completedGateEvidence(states, toolCalls, qaSnapshots);
  } else if (releasedCount !== 0) {
    throw new Error(`${terminalStatus} traces must not release a candidate`);
  }
  if (terminalStatus === "NEEDS_REVIEW" && resultHttpStatus !== 409) {
    throw new Error("A NEEDS_REVIEW trace must record HTTP 409");
  }

  const inputTokens = nullableInteger(usage.input_tokens, 0, MAX_TOKENS, "usage.input_tokens");
  const outputTokens = nullableInteger(usage.output_tokens, 0, MAX_TOKENS, "usage.output_tokens");
  const totalTokens = nullableInteger(usage.total_tokens, 0, MAX_TOKENS, "usage.total_tokens");
  const tokens = [inputTokens, outputTokens, totalTokens];
  if (tokens.some((item) => item === null) && tokens.some((item) => item !== null)) {
    throw new Error("Usage token metrics must all be recorded or all be null");
  }
  if (inputTokens !== null && outputTokens !== null && totalTokens !== inputTokens + outputTokens) {
    throw new Error("usage.total_tokens must equal input_tokens + output_tokens");
  }

  return {
    schema_version: 1,
    trace_id: identifier(root.trace_id, "trace_id"),
    generated_at: timestamp(root.generated_at, "generated_at"),
    document: { format: enumeration(document.format, FORMATS, "document.format") },
    evidence: {
      kind: enumeration(evidence.kind, EVIDENCE_KINDS, "evidence.kind"),
      pi_session: boolean(evidence.pi_session, "evidence.pi_session"),
      model_calls: integer(evidence.model_calls, 0, MAX_COUNT, "evidence.model_calls"),
      ordering: enumeration(evidence.ordering, ["within_stream"], "evidence.ordering"),
    },
    terminal: {
      status: terminalStatus,
      result_http_status: resultHttpStatus,
      reason_code: terminal.reason_code === null ? null : identifier(terminal.reason_code, "terminal.reason_code"),
    },
    states,
    tool_calls: toolCalls,
    qa_snapshots: qaSnapshots,
    candidates,
    usage: {
      input_tokens: inputTokens,
      output_tokens: outputTokens,
      total_tokens: totalTokens,
      cost_usd: nullableNumber(usage.cost_usd, 0, 1_000_000, "usage.cost_usd"),
      latency_ms: nullableInteger(usage.latency_ms, 0, MAX_DURATION_MS, "usage.latency_ms"),
    },
  };
}

function exactRecord(value, label, allowedKeys) {
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
    if (!Object.hasOwn(value, key)) throw new Error(`${label} is missing ${JSON.stringify(key)}`);
  }
  return value;
}

function array(value, label, maximum, parseItem) {
  if (!Array.isArray(value)) throw new Error(`${label} must be an array`);
  if (value.length > maximum) throw new Error(`${label} exceeds its item limit`);
  return value.map(parseItem);
}

function issueCounts(value, index) {
  const label = `qa_snapshots[${index}].issue_counts`;
  const counts = exactRecord(value, label, ["error", "warning", "info"]);
  return {
    error: integer(counts.error, 0, MAX_COUNT, `${label}.error`),
    warning: integer(counts.warning, 0, MAX_COUNT, `${label}.warning`),
    info: integer(counts.info, 0, MAX_COUNT, `${label}.info`),
  };
}

function completedGateEvidence(states, toolCalls, qaSnapshots) {
  if (qaSnapshots.length === 0) throw new Error("A completed trace requires at least one QA snapshot");
  if (qaSnapshots.at(-1).blocking_count !== 0) {
    throw new Error("A completed trace requires a final QA snapshot with zero blocking issues");
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
      "A completed trace requires run_document_qa before verify_candidate and final finalize_result evidence",
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
    throw new Error("A completed trace requires fresh QA after the last candidate mutation");
  }
  if (![qaToolIndex, verifyIndex, finalizeIndex].every((index) => {
    const outcome = toolCalls[index]?.outcome;
    return outcome === "recorded" || outcome === "succeeded";
  })) {
    throw new Error("A completed trace release-gate tool evidence must be successful");
  }
  const stateNames = states.map(({ status }) => status);
  const verifyingIndex = stateNames.lastIndexOf("VERIFYING");
  const statesBeforeVerifying = stateNames.slice(0, verifyingIndex);
  const qaStateIndex = statesBeforeVerifying.lastIndexOf("QA");
  if (verifyingIndex < 0 || qaStateIndex < 0 || qaStateIndex >= verifyingIndex) {
    throw new Error("A completed trace requires QA before VERIFYING state evidence");
  }
  const candidateMutationStateAfterQa = statesBeforeVerifying
    .slice(qaStateIndex + 1)
    .some((status) => status === "TRANSLATING" || status === "REPAIRING");
  if (candidateMutationStateAfterQa) {
    throw new Error("A completed trace requires fresh QA state after the last mutation");
  }
}

function sequence(value, expected, label) {
  if (value !== expected) throw new Error(`${label} must be ${expected}`);
}

function enumeration(value, allowed, label) {
  if (typeof value !== "string" || !allowed.includes(value)) throw new Error(`${label} is not allowed`);
  return value;
}

function identifier(value, label) {
  if (typeof value !== "string" || !IDENTIFIER.test(value)) throw new Error(`${label} is not a safe identifier`);
  return value;
}

function digest(value, label) {
  if (typeof value !== "string" || !SHA256.test(value)) throw new Error(`${label} must be a lowercase SHA-256 digest`);
  return value;
}

function timestamp(value, label) {
  if (typeof value !== "string" || value.length > 32) throw new Error(`${label} must be a timestamp`);
  const parsed = new Date(value);
  if (!Number.isFinite(parsed.getTime()) || parsed.toISOString() !== value) throw new Error(`${label} must be canonical UTC`);
  return value;
}

function boolean(value, label) {
  if (typeof value !== "boolean") throw new Error(`${label} must be a boolean`);
  return value;
}

function integer(value, minimum, maximum, label) {
  if (!Number.isSafeInteger(value) || value < minimum || value > maximum) {
    throw new Error(`${label} must be an integer between ${minimum} and ${maximum}`);
  }
  return value;
}

function nullableInteger(value, minimum, maximum, label) {
  return value === null ? null : integer(value, minimum, maximum, label);
}

function finiteNumber(value, minimum, maximum, label) {
  if (typeof value !== "number" || !Number.isFinite(value) || value < minimum || value > maximum) {
    throw new Error(`${label} must be a finite number between ${minimum} and ${maximum}`);
  }
  return value;
}

function nullableNumber(value, minimum, maximum, label) {
  return value === null ? null : finiteNumber(value, minimum, maximum, label);
}
