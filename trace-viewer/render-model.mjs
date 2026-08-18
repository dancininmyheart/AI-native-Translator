export const MISSING_LABEL = "Not recorded";

/**
 * Converts a validated trace into display-only strings. No HTML is produced;
 * the browser layer inserts every value through `textContent`.
 */
export function buildTraceViewModel(trace) {
  return {
    traceId: trace.trace_id,
    generatedAt: trace.generated_at,
    documentFormat: trace.document.format.toUpperCase(),
    evidence: {
      kind: trace.evidence.kind,
      piSession: yesNo(trace.evidence.pi_session),
      modelCalls: String(trace.evidence.model_calls),
      ordering: trace.evidence.ordering === "within_stream"
        ? "Order is authoritative within each stream"
        : trace.evidence.ordering,
    },
    terminal: {
      status: trace.terminal.status,
      httpStatus: measured(trace.terminal.result_http_status),
      reason: measured(trace.terminal.reason_code),
    },
    metrics: {
      inputTokens: measured(trace.usage.input_tokens),
      outputTokens: measured(trace.usage.output_tokens),
      totalTokens: measured(trace.usage.total_tokens),
      costUsd: trace.usage.cost_usd === null ? MISSING_LABEL : `$${trace.usage.cost_usd.toFixed(6)}`,
      latencyMs: trace.usage.latency_ms === null ? MISSING_LABEL : `${trace.usage.latency_ms} ms`,
    },
    states: trace.states.map((state) => ({
      sequence: String(state.sequence + 1),
      status: state.status,
      elapsedMs: state.elapsed_ms === null ? MISSING_LABEL : `${state.elapsed_ms} ms`,
    })),
    tools: trace.tool_calls.map((call) => ({
      sequence: String(call.sequence + 1),
      tool: call.tool,
      outcome: call.outcome,
      durationMs: call.duration_ms === null ? MISSING_LABEL : `${call.duration_ms} ms`,
    })),
    qaSnapshots: trace.qa_snapshots.map((snapshot) => ({
      sequence: String(snapshot.sequence + 1),
      blockingCount: String(snapshot.blocking_count),
      qualityScore: measured(snapshot.quality_score),
      issueCounts: snapshot.issue_counts === null
        ? MISSING_LABEL
        : `${snapshot.issue_counts.error} error / ${snapshot.issue_counts.warning} warning / ${snapshot.issue_counts.info} info`,
    })),
    candidates: trace.candidates.map((candidate) => ({
      sequence: String(candidate.sequence + 1),
      candidateId: candidate.candidate_id,
      sha256: candidate.sha256,
      selected: yesNo(candidate.selected),
      released: yesNo(candidate.released),
    })),
  };
}

function measured(value) {
  return value === null ? MISSING_LABEL : String(value);
}

function yesNo(value) {
  return value ? "Yes" : "No";
}
