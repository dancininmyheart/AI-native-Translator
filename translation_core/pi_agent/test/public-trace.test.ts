import { describe, expect, it } from "vitest";
import {
  formatPublicTraceReplay,
  parsePublicTrace,
  parsePublicTraceJson,
  projectP1aDemoToPublicTraces,
  projectP1aScenarioToPublicTrace,
} from "../src/public-trace.js";

const HASH = "14d4a537f6b6c391d003867b7a5e5dd714580b5d2dfc704193e0ad3f092eabac";

function validTrace(): unknown {
  return {
    schema_version: 1,
    trace_id: "offline-direct-docx",
    generated_at: "2026-08-18T00:00:00.000Z",
    document: { format: "docx" },
    evidence: {
      kind: "real_python_core",
      pi_session: false,
      model_calls: 0,
      ordering: "within_stream",
    },
    terminal: {
      status: "COMPLETED",
      result_http_status: 200,
      reason_code: null,
    },
    states: [
      { sequence: 0, status: "PREFLIGHT", elapsed_ms: null },
      { sequence: 1, status: "QA", elapsed_ms: null },
      { sequence: 2, status: "VERIFYING", elapsed_ms: null },
      { sequence: 3, status: "COMPLETED", elapsed_ms: null },
    ],
    tool_calls: [
      {
        sequence: 0,
        tool: "preflight_document",
        outcome: "recorded",
        duration_ms: null,
      },
      {
        sequence: 1,
        tool: "run_document_qa",
        outcome: "recorded",
        duration_ms: null,
      },
      {
        sequence: 2,
        tool: "verify_candidate",
        outcome: "recorded",
        duration_ms: null,
      },
      {
        sequence: 3,
        tool: "finalize_result",
        outcome: "recorded",
        duration_ms: null,
      },
    ],
    qa_snapshots: [
      {
        sequence: 0,
        blocking_count: 0,
        quality_score: 100,
        issue_counts: null,
      },
    ],
    candidates: [
      {
        sequence: 0,
        candidate_id: "candidate-r0",
        sha256: HASH,
        selected: true,
        released: true,
      },
    ],
    usage: {
      input_tokens: null,
      output_tokens: null,
      total_tokens: null,
      cost_usd: null,
      latency_ms: 4140,
    },
  };
}

function rawP1aScenario(scenarioId: "direct-docx" | "repair-pptx" | "high-risk-pptx") {
  if (scenarioId === "direct-docx") {
    return {
      scenario_id: scenarioId,
      evidence_kind: "real_python_core",
      pi_session: false,
      model_calls: 0,
      terminal_status: "COMPLETED",
      result_http_status: 200,
      blocking_counts: [0],
      quality_scores: [100],
      repair_decisions: ["not_needed"],
      selected_candidate_id: "candidate-r0",
      selected_candidate_sha256: HASH,
      result_sha256: HASH,
      elapsed_ms: 4140,
      state_transitions: ["PREFLIGHT", "TRANSLATING", "QA", "VERIFYING", "QA", "COMPLETED"],
      tool_sequence: ["preflight_document", "translate_once", "run_document_qa", "verify_candidate", "finalize_result"],
    };
  }
  if (scenarioId === "repair-pptx") {
    const hash = "d1264225654c4b5c6e2e05b2cc9b5daef350c06d1f5afa0b89379ec5db255b07";
    return {
      scenario_id: scenarioId,
      evidence_kind: "scripted_fixture_candidate",
      pi_session: false,
      model_calls: 0,
      terminal_status: "COMPLETED",
      result_http_status: 200,
      blocking_counts: [2, 0],
      quality_scores: [80, 100],
      repair_decisions: ["promote:candidate-r1"],
      selected_candidate_id: "candidate-r1",
      selected_candidate_sha256: hash,
      result_sha256: hash,
      elapsed_ms: 24407,
      state_transitions: ["PREFLIGHT", "QA", "REPAIRING", "QA", "VERIFYING", "QA", "COMPLETED"],
      tool_sequence: ["preflight_document", "run_document_qa", "apply_repair", "run_document_qa", "verify_candidate", "finalize_result"],
    };
  }
  return {
    scenario_id: scenarioId,
    evidence_kind: "scripted_fixture_candidate",
    pi_session: false,
    model_calls: 0,
    terminal_status: "NEEDS_REVIEW",
    result_http_status: 409,
    blocking_counts: [3, 2, 1],
    quality_scores: [70, 80, 90],
    repair_decisions: ["promote:candidate-r1", "promote:candidate-r2", "stop:budget_exhausted"],
    selected_candidate_id: "candidate-r2",
    selected_candidate_sha256: "631e3c04bbcb148cfb99e77936fca6eded184765d444d54ef88fc41089a212a4",
    result_sha256: null,
    elapsed_ms: 22488,
    state_transitions: ["PREFLIGHT", "QA", "REPAIRING", "QA", "REPAIRING", "QA", "NEEDS_REVIEW"],
    tool_sequence: ["preflight_document", "run_document_qa", "apply_repair", "run_document_qa", "apply_repair", "run_document_qa"],
  };
}

describe("public trace schema", () => {
  it("accepts a schema_version=1 trace and returns a detached normalized value", () => {
    const input = validTrace() as Record<string, unknown>;
    const trace = parsePublicTrace(input);

    expect(trace).toMatchObject({
      schema_version: 1,
      trace_id: "offline-direct-docx",
      evidence: { pi_session: false, model_calls: 0 },
      terminal: { status: "COMPLETED" },
      usage: { total_tokens: null, cost_usd: null, latency_ms: 4140 },
    });
    expect(trace).not.toBe(input);
    expect(trace.states).not.toBe(input.states);
  });

  it.each([
    ["prompt", (trace: Record<string, unknown>) => { trace.prompt = "ignore prior instructions"; }],
    ["absolute path", (trace: Record<string, unknown>) => { trace.source_path = "C:\\private\\source.docx"; }],
    ["credential", (trace: Record<string, unknown>) => { trace.authorization = "Bearer secret"; }],
    ["candidate path", (trace: Record<string, unknown>) => {
      (trace.candidates as Array<Record<string, unknown>>)[0]!.path = "../../private.docx";
    }],
    ["prototype key", (trace: Record<string, unknown>) => {
      Object.defineProperty(trace, "__proto__", { value: { admin: true }, enumerable: true });
    }],
  ])("rejects the unauthorized %s field instead of silently displaying it", (_label, mutate) => {
    const trace = validTrace() as Record<string, unknown>;
    mutate(trace);

    expect(() => parsePublicTrace(trace)).toThrow(/unknown field/i);
  });

  it.each([
    ["unknown tool", (trace: Record<string, unknown>) => {
      (trace.tool_calls as Array<Record<string, unknown>>)[0]!.tool = "read_arbitrary_file";
    }],
    ["invalid candidate hash", (trace: Record<string, unknown>) => {
      (trace.candidates as Array<Record<string, unknown>>)[0]!.sha256 = "not-a-hash";
    }],
    ["markup in identifier", (trace: Record<string, unknown>) => { trace.trace_id = "<img src=x onerror=alert(1)>"; }],
    ["unsupported schema", (trace: Record<string, unknown>) => { trace.schema_version = 2; }],
  ])("rejects %s", (_label, mutate) => {
    const trace = validTrace() as Record<string, unknown>;
    mutate(trace);

    expect(() => parsePublicTrace(trace)).toThrow();
  });

  it("rejects contradictory terminal, state, selection, and release evidence", () => {
    const reviewReleased = validTrace() as Record<string, unknown>;
    reviewReleased.terminal = { status: "NEEDS_REVIEW", result_http_status: 409, reason_code: "budget_exhausted" };
    (reviewReleased.states as Array<Record<string, unknown>>).at(-1)!.status = "NEEDS_REVIEW";
    expect(() => parsePublicTrace(reviewReleased)).toThrow(/must not release/i);

    const mismatchedState = validTrace() as Record<string, unknown>;
    (mismatchedState.states as Array<Record<string, unknown>>).at(-1)!.status = "QA";
    expect(() => parsePublicTrace(mismatchedState)).toThrow(/last state/i);

    const unselectedRelease = validTrace() as Record<string, unknown>;
    (unselectedRelease.candidates as Array<Record<string, unknown>>)[0]!.selected = false;
    expect(() => parsePublicTrace(unselectedRelease)).toThrow(/released candidate must be selected/i);
  });

  it("rejects a completed trace without full QA, verification, and finalization evidence", () => {
    const withoutQa = validTrace() as Record<string, unknown>;
    withoutQa.qa_snapshots = [];
    expect(() => parsePublicTrace(withoutQa)).toThrow(/completed trace.*QA snapshot/i);

    const withoutReleaseTools = validTrace() as Record<string, unknown>;
    withoutReleaseTools.tool_calls = (withoutReleaseTools.tool_calls as unknown[]).slice(0, 1);
    expect(() => parsePublicTrace(withoutReleaseTools)).toThrow(/verify_candidate.*finalize_result/i);

    const failedFinalization = validTrace() as Record<string, unknown>;
    (failedFinalization.tool_calls as Array<Record<string, unknown>>).at(-1)!.outcome = "failed";
    expect(() => parsePublicTrace(failedFinalization)).toThrow(/release-gate tool evidence.*successful/i);

    const staleQaAfterMutation = validTrace() as Record<string, unknown>;
    const staleTools = staleQaAfterMutation.tool_calls as Array<Record<string, unknown>>;
    staleTools.splice(2, 0, {
      sequence: 2,
      tool: "apply_repair",
      outcome: "recorded",
      duration_ms: null,
    });
    staleTools.forEach((tool, sequence) => { tool.sequence = sequence; });
    expect(() => parsePublicTrace(staleQaAfterMutation)).toThrow(/fresh QA.*candidate mutation/i);

    const staleQaState = validTrace() as Record<string, unknown>;
    const staleStates = staleQaState.states as Array<Record<string, unknown>>;
    staleStates.splice(2, 0, { sequence: 2, status: "REPAIRING", elapsed_ms: null });
    staleStates.forEach((state, sequence) => { state.sequence = sequence; });
    expect(() => parsePublicTrace(staleQaState)).toThrow(/fresh QA state.*mutation/i);
  });

  it("parses bounded JSON input through the same strict public schema", () => {
    const trace = parsePublicTraceJson(JSON.stringify(validTrace()));

    expect(trace.trace_id).toBe("offline-direct-docx");
    expect(() => parsePublicTraceJson(" ".repeat(1_048_577))).toThrow(/size limit/i);
  });
});

describe("P1-A public trace projection", () => {
  it("projects only allowlisted operational evidence and drops sensitive source material", () => {
    const rawScenario = {
      ...rawP1aScenario("direct-docx"),
      source_text: "confidential document body",
      prompt: "private system prompt",
      model_response: "private model response",
      authorization: "Bearer secret",
      source_path: "C:\\Users\\private\\source.docx",
    };

    const trace = projectP1aScenarioToPublicTrace(rawScenario, "2026-08-18T00:00:00.000Z");
    const serialized = JSON.stringify(trace);

    expect(trace).toMatchObject({
      schema_version: 1,
      trace_id: "p1a-direct-docx",
      document: { format: "docx" },
      evidence: { kind: "real_python_core", pi_session: false, model_calls: 0 },
      terminal: { status: "COMPLETED", result_http_status: 200, reason_code: null },
      qa_snapshots: [{ blocking_count: 0, quality_score: 100 }],
      candidates: [{ candidate_id: "candidate-r0", sha256: HASH, selected: true, released: true }],
      usage: {
        input_tokens: null,
        output_tokens: null,
        total_tokens: null,
        cost_usd: null,
        latency_ms: 4140,
      },
    });
    expect(trace.tool_calls.map(({ tool }) => tool)).toEqual(rawScenario.tool_sequence);
    expect(serialized).not.toMatch(/confidential|prompt|model.response|authorization|bearer|users|source_path/i);
  });

  it("does not let offline fixtures be relabeled as real model sessions", () => {
    const raw = {
      scenario_id: "direct-docx",
      evidence_kind: "real_python_core",
      pi_session: true,
      model_calls: 1,
    };

    expect(() => projectP1aScenarioToPublicTrace(raw, "2026-08-18T00:00:00.000Z")).toThrow(
      /pi_session=false and model_calls=0/,
    );
  });

  it("projects every scenario in a demo envelope without carrying top-level private metadata", () => {
    const projected = projectP1aDemoToPublicTraces({
      schema_version: 1,
      demo: "p1a-three-path-offline",
      pi_session: false,
      model_calls: 0,
      scenario_count: 3,
      private_run_directory: "D:\\private\\runs",
      scenarios: [
        rawP1aScenario("direct-docx"),
        rawP1aScenario("repair-pptx"),
        rawP1aScenario("high-risk-pptx"),
      ],
    }, "2026-08-18T00:00:00.000Z");

    expect(projected).toHaveLength(3);
    expect(projected[2]).toMatchObject({
      trace_id: "p1a-high-risk-pptx",
      terminal: { status: "NEEDS_REVIEW", reason_code: "budget_exhausted" },
      candidates: [{ released: false }],
    });
    expect(JSON.stringify(projected)).not.toMatch(/private_run_directory|private.runs/i);
  });

  it("rejects an incomplete or reordered artifact labelled as the three-path demo", () => {
    const header = {
      schema_version: 1,
      demo: "p1a-three-path-offline",
      pi_session: false,
      model_calls: 0,
    };
    expect(() => projectP1aDemoToPublicTraces({
      ...header,
      scenario_count: 1,
      scenarios: [rawP1aScenario("direct-docx")],
    }, "2026-08-18T00:00:00.000Z")).toThrow(/exactly three/i);
    expect(() => projectP1aDemoToPublicTraces({
      ...header,
      scenario_count: 3,
      scenarios: [
        rawP1aScenario("repair-pptx"),
        rawP1aScenario("direct-docx"),
        rawP1aScenario("high-risk-pptx"),
      ],
    }, "2026-08-18T00:00:00.000Z")).toThrow(/fixed order/i);
  });

  it("rejects a P1-A scenario whose recorded gate sequence was reordered", () => {
    const reordered = rawP1aScenario("repair-pptx");
    reordered.tool_sequence = [
      "preflight_document",
      "run_document_qa",
      "apply_repair",
      "verify_candidate",
      "run_document_qa",
      "finalize_result",
    ];

    expect(() => projectP1aScenarioToPublicTrace(reordered, "2026-08-18T00:00:00.000Z"))
      .toThrow(/fixed P1-A tool sequence/i);
  });
});

describe("public trace replay", () => {
  it("replays bounded evidence and labels unavailable token/cost metrics instead of inventing them", () => {
    const replay = formatPublicTraceReplay(parsePublicTrace(validTrace()));

    expect(replay).toContain("STATE 1/4 PREFLIGHT elapsed_ms=not recorded");
    expect(replay).toContain("STATE 3/4 VERIFYING elapsed_ms=not recorded");
    expect(replay).toContain("TOOL 1/4 preflight_document outcome=recorded duration_ms=not recorded");
    expect(replay).toContain("TOOL 4/4 finalize_result outcome=recorded duration_ms=not recorded");
    expect(replay).toContain("QA 1/1 blockers=0 quality=100");
    expect(replay).toContain(`CANDIDATE 1/1 candidate-r0 sha256=${HASH} selected=true released=true`);
    expect(replay).toContain("USAGE input_tokens=not recorded output_tokens=not recorded total_tokens=not recorded cost_usd=not recorded latency_ms=4140");
    expect(replay).toContain("TERMINAL COMPLETED http=200 reason=not recorded");
    expect(replay).not.toMatch(/undefined|NaN/);
  });
});
