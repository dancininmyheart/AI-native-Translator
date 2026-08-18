export const P1A_SCENARIO_IDS = ["direct-docx", "repair-pptx", "high-risk-pptx"] as const;

export type P1aScenarioId = (typeof P1A_SCENARIO_IDS)[number];
export type P1aTerminalStatus = "COMPLETED" | "NEEDS_REVIEW";
export type P1aEvidenceKind = "real_python_core" | "scripted_fixture_candidate";

export interface P1aFixtureScenario {
  scenario_id: P1aScenarioId;
  document_format: "docx" | "pptx";
  source_relpath: string;
  candidate_relpaths: string[];
  expected_terminal_status: P1aTerminalStatus;
  expected_result_http_status: 200 | 409;
  expected_blocking_counts: number[];
  candidate_sha256: Record<string, string>;
  evidence_kind: P1aEvidenceKind;
  native_python_qa: true;
  repair_execution: "not_applicable" | "scripted_fixture_candidate";
  fixture_relpath?: string;
}

export interface P1aFixtureManifest {
  schema_version: 1;
  generated_by: "translation_core/tools/p1a_demo_fixtures.py";
  model_calls: 0;
  scenarios: P1aFixtureScenario[];
}

export interface P1aScenarioSummary {
  scenario_id: P1aScenarioId;
  evidence_kind: P1aEvidenceKind;
  native_python_qa: true;
  repair_execution: "not_applicable" | "scripted_fixture_candidate";
  pi_session: false;
  model_calls: 0;
  terminal_status: P1aTerminalStatus;
  result_http_status: 200 | 409;
  blocking_counts: number[];
  quality_scores: number[];
  repair_decisions: string[];
  repair_rounds: number;
  max_repair_rounds: number;
  selected_candidate_id: string;
  selected_candidate_sha256: string;
  expected_candidate_sha256: string | null;
  result_sha256: string | null;
  elapsed_ms: number;
  state_transitions: string[];
  tool_sequence: string[];
}

export interface P1aDemoSummary {
  schema_version: 1;
  demo: "p1a-three-path-offline";
  pi_session: false;
  model_calls: 0;
  scenario_count: 3;
  scenarios: P1aScenarioSummary[];
}
