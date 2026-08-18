import path from "node:path";
import { createHash } from "node:crypto";
import { mkdir, readFile, writeFile } from "node:fs/promises";

export type EvalStrategy = "no_repair" | "fixed_two_round" | "bounded_agent";

interface EvalRound {
  candidate_id: string;
  candidate_relpath: string;
  blocking_count: number;
  new_regressions: number;
  oracle_semantic_pass: boolean;
  oracle_layout_pass: boolean;
  replay_latency_ms: number;
}

interface EvalScenario {
  scenario_id: string;
  document_format: "docx" | "pptx";
  source_relpath: string;
  max_repair_rounds: number;
  provenance: { kind: "synthetic"; license: "CC0-1.0" };
  rounds: EvalRound[];
}

interface EvalManifest {
  schema_version: 1;
  corpus_id: string;
  corpus_version: string;
  evaluation_scope: "offline_orchestration_policy_replay";
  generated_by: "translation_core/tools/eval_public_corpus.py";
  license: "CC0-1.0";
  pi_session: false;
  model_calls: 0;
  native_office_qa: false;
  qa_annotations: "frozen_synthetic";
  office_artifacts: "runtime_generated_carriers";
  replay_latency: "deterministic_fixture_value_not_wall_clock";
  scenarios: EvalScenario[];
}

export interface EvalScenarioResult {
  scenario_id: string;
  document_format: "docx" | "pptx";
  selected_round: number;
  selected_candidate_id: string;
  terminal_status: "RELEASED" | "NEEDS_REVIEW";
  result_http_status: 200 | 409;
  initial_blocking_count: number;
  final_blocking_count: number;
  selected_new_regressions: number;
  oracle_semantic_pass: boolean;
  oracle_layout_pass: boolean;
  repair_attempts: number;
  replay_latency_ms: number;
  model_calls: 0;
  total_tokens: 0;
  estimated_cost_usd: 0;
}

export interface EvalStrategyMetrics {
  scenario_count: number;
  released_count: number;
  release_coverage: number;
  unsafe_release_count: number;
  conditional_risk: number | null;
  semantic_pass_count: number;
  semantic_pass_rate: number;
  layout_pass_count: number;
  layout_pass_rate: number;
  initially_blocked_count: number;
  repair_uplift_count: number;
  repair_uplift_rate: number;
  new_regression_scenario_count: number;
  new_regression_rate: number;
  total_repair_attempts: number;
  average_repair_attempts: number;
  replay_latency_ms_total: number;
  replay_latency_ms_p50: number;
  replay_latency_ms_p95: number;
  model_calls: 0;
  total_tokens: 0;
  estimated_cost_usd: 0;
}

export interface EvalStrategyResult {
  strategy: EvalStrategy;
  metrics: EvalStrategyMetrics;
  scenarios: EvalScenarioResult[];
}

export interface PublicAgentEvalSummary {
  schema_version: 1;
  eval: "officeguard-public-agent-eval";
  eval_version: "1.0.0";
  corpus_id: string;
  corpus_version: string;
  corpus_manifest_sha256: string;
  model_calls: 0;
  evidence_boundary: {
    evaluation_scope: "offline_orchestration_policy_replay";
    pi_session: false;
    native_model_eval: false;
    corpus_provenance: "synthetic_cc0";
    qa_annotations: "frozen_synthetic";
    office_artifacts: "runtime_generated_carriers";
    replay_latency: "deterministic_fixture_value_not_wall_clock";
  };
  strategies: EvalStrategyResult[];
}

export interface RunPublicAgentEvalOptions {
  manifestPath: string;
  outputDir: string;
}

export async function runPublicAgentEval(options: RunPublicAgentEvalOptions): Promise<PublicAgentEvalSummary> {
  const manifestText = await readFile(options.manifestPath, "utf8");
  const manifest = parseManifest(JSON.parse(manifestText));
  const strategies: EvalStrategy[] = ["no_repair", "fixed_two_round", "bounded_agent"];
  const summary: PublicAgentEvalSummary = {
    schema_version: 1,
    eval: "officeguard-public-agent-eval",
    eval_version: "1.0.0",
    corpus_id: manifest.corpus_id,
    corpus_version: manifest.corpus_version,
    corpus_manifest_sha256: createHash("sha256").update(manifestText, "utf8").digest("hex"),
    model_calls: 0,
    evidence_boundary: {
      evaluation_scope: "offline_orchestration_policy_replay",
      pi_session: false,
      native_model_eval: false,
      corpus_provenance: "synthetic_cc0",
      qa_annotations: "frozen_synthetic",
      office_artifacts: "runtime_generated_carriers",
      replay_latency: "deterministic_fixture_value_not_wall_clock",
    },
    strategies: strategies.map((strategy) => evaluateStrategy(manifest.scenarios, strategy)),
  };
  await mkdir(options.outputDir, { recursive: true });
  await writeFile(path.join(options.outputDir, "eval-summary.json"), `${JSON.stringify(summary, null, 2)}\n`, "utf8");
  await writeFile(path.join(options.outputDir, "eval-report.md"), renderMarkdown(summary), "utf8");
  return summary;
}

function renderMarkdown(summary: PublicAgentEvalSummary): string {
  const rows = summary.strategies.flatMap((strategy) => strategy.scenarios.map((scenario) => (
    `| ${strategy.strategy} | ${scenario.scenario_id} | ${scenario.selected_candidate_id} | ${scenario.terminal_status} | ${scenario.repair_attempts} |`
  )));
  return [
    "# OfficeGuard Public Agent Eval",
    "",
    `Eval contract: \`${summary.eval_version}\`; corpus: \`${summary.corpus_id}@${summary.corpus_version}\`.`,
    `Manifest SHA-256: \`${summary.corpus_manifest_sha256}\`.`,
    "",
    "Evidence boundary: this is an offline orchestration-policy replay with `pi_session=false` and `model_calls=0`; it is not a live-model quality evaluation.",
    "The CC0 OOXML files are runtime-generated carrier artifacts, and the QA/oracle labels are frozen synthetic annotations: this is not native Office QA evidence. Replay latency is a deterministic fixture value, not measured wall-clock latency.",
    "",
    "| Strategy | Release coverage | Conditional risk | Repair uplift | New regression rate | Avg repair attempts | Replay P50/P95 (ms) |",
    "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ...summary.strategies.map(({ strategy, metrics }) => (
      `| ${strategy} | ${percent(metrics.release_coverage)} | ${nullablePercent(metrics.conditional_risk)} | ${percent(metrics.repair_uplift_rate)} | ${percent(metrics.new_regression_rate)} | ${metrics.average_repair_attempts.toFixed(2)} | ${metrics.replay_latency_ms_p50}/${metrics.replay_latency_ms_p95} |`
    )),
    "",
    "| Strategy | Scenario | Selected candidate | Outcome | Repair attempts |",
    "| --- | --- | --- | --- | ---: |",
    ...rows,
    "",
  ].join("\n");
}

function evaluateStrategy(scenarios: EvalScenario[], strategy: EvalStrategy): EvalStrategyResult {
  const results = scenarios.map((scenario) => evaluateScenario(scenario, strategy));
  return { strategy, metrics: calculateMetrics(results), scenarios: results };
}

function evaluateScenario(scenario: EvalScenario, strategy: EvalStrategy): EvalScenarioResult {
  let selectedRound = 0;
  let repairAttempts = 0;
  if (strategy === "fixed_two_round") {
    selectedRound = Math.min(2, scenario.max_repair_rounds, scenario.rounds.length - 1);
    repairAttempts = selectedRound;
  } else if (strategy === "bounded_agent") {
    for (let roundIndex = 1; roundIndex <= Math.min(scenario.max_repair_rounds, scenario.rounds.length - 1); roundIndex += 1) {
      if (scenario.rounds[selectedRound]!.blocking_count === 0) break;
      repairAttempts += 1;
      const candidate = scenario.rounds[roundIndex]!;
      if (candidate.new_regressions > 0 || candidate.blocking_count >= scenario.rounds[selectedRound]!.blocking_count) break;
      selectedRound = roundIndex;
    }
  }
  const selected = scenario.rounds[selectedRound]!;
  const initial = scenario.rounds[0]!;
  const released = selected.blocking_count === 0;
  return {
    scenario_id: scenario.scenario_id,
    document_format: scenario.document_format,
    selected_round: selectedRound,
    selected_candidate_id: selected.candidate_id,
    terminal_status: released ? "RELEASED" : "NEEDS_REVIEW",
    result_http_status: released ? 200 : 409,
    initial_blocking_count: initial.blocking_count,
    final_blocking_count: selected.blocking_count,
    selected_new_regressions: selected.new_regressions,
    oracle_semantic_pass: selected.oracle_semantic_pass,
    oracle_layout_pass: selected.oracle_layout_pass,
    repair_attempts: repairAttempts,
    replay_latency_ms: scenario.rounds.slice(0, repairAttempts + 1)
      .reduce((total, round) => total + round.replay_latency_ms, 0),
    model_calls: 0,
    total_tokens: 0,
    estimated_cost_usd: 0,
  };
}

function calculateMetrics(scenarios: EvalScenarioResult[]): EvalStrategyMetrics {
  const scenarioCount = scenarios.length;
  const released = scenarios.filter((scenario) => scenario.terminal_status === "RELEASED");
  const unsafeReleaseCount = released.filter((scenario) => !scenario.oracle_semantic_pass || !scenario.oracle_layout_pass).length;
  const semanticPassCount = scenarios.filter((scenario) => scenario.oracle_semantic_pass).length;
  const layoutPassCount = scenarios.filter((scenario) => scenario.oracle_layout_pass).length;
  const initiallyBlocked = scenarios.filter((scenario) => scenario.initial_blocking_count > 0);
  const repairUpliftCount = initiallyBlocked.filter((scenario) => scenario.terminal_status === "RELEASED").length;
  const newRegressionScenarioCount = scenarios.filter((scenario) => scenario.selected_new_regressions > 0).length;
  const totalRepairAttempts = scenarios.reduce((total, scenario) => total + scenario.repair_attempts, 0);
  const latencies = scenarios.map((scenario) => scenario.replay_latency_ms);
  return {
    scenario_count: scenarioCount,
    released_count: released.length,
    release_coverage: ratio(released.length, scenarioCount),
    unsafe_release_count: unsafeReleaseCount,
    conditional_risk: released.length === 0 ? null : ratio(unsafeReleaseCount, released.length),
    semantic_pass_count: semanticPassCount,
    semantic_pass_rate: ratio(semanticPassCount, scenarioCount),
    layout_pass_count: layoutPassCount,
    layout_pass_rate: ratio(layoutPassCount, scenarioCount),
    initially_blocked_count: initiallyBlocked.length,
    repair_uplift_count: repairUpliftCount,
    repair_uplift_rate: ratio(repairUpliftCount, initiallyBlocked.length),
    new_regression_scenario_count: newRegressionScenarioCount,
    new_regression_rate: ratio(newRegressionScenarioCount, scenarioCount),
    total_repair_attempts: totalRepairAttempts,
    average_repair_attempts: ratio(totalRepairAttempts, scenarioCount),
    replay_latency_ms_total: latencies.reduce((total, value) => total + value, 0),
    replay_latency_ms_p50: percentile(latencies, 0.5),
    replay_latency_ms_p95: percentile(latencies, 0.95),
    model_calls: 0,
    total_tokens: 0,
    estimated_cost_usd: 0,
  };
}

function ratio(numerator: number, denominator: number): number {
  if (denominator === 0) return 0;
  return Math.round((numerator / denominator) * 10_000) / 10_000;
}

function percentile(values: number[], percentileValue: number): number {
  if (values.length === 0) return 0;
  const sorted = [...values].sort((left, right) => left - right);
  return sorted[Math.max(0, Math.ceil(sorted.length * percentileValue) - 1)]!;
}

function percent(value: number): string {
  return `${(value * 100).toFixed(1)}%`;
}

function nullablePercent(value: number | null): string {
  return value === null ? "N/A" : percent(value);
}

function parseManifest(value: unknown): EvalManifest {
  if (
    !isRecord(value)
    || value.schema_version !== 1
    || value.corpus_id !== "officeguard-public-policy-eval"
    || typeof value.corpus_version !== "string"
    || !/^\d+\.\d+\.\d+$/.test(value.corpus_version)
    || value.evaluation_scope !== "offline_orchestration_policy_replay"
    || value.generated_by !== "translation_core/tools/eval_public_corpus.py"
    || value.license !== "CC0-1.0"
    || value.pi_session !== false
    || value.model_calls !== 0
    || value.native_office_qa !== false
    || value.qa_annotations !== "frozen_synthetic"
    || value.office_artifacts !== "runtime_generated_carriers"
    || value.replay_latency !== "deterministic_fixture_value_not_wall_clock"
    || !Array.isArray(value.scenarios)
    || value.scenarios.length === 0
  ) {
    throw new Error("Public eval manifest header is invalid");
  }
  const scenarioIds = new Set<string>();
  const corpusPaths = new Set<string>();
  for (const [scenarioIndex, scenario] of value.scenarios.entries()) {
    if (!isRecord(scenario) || !Array.isArray(scenario.rounds) || scenario.rounds.length < 1) {
      throw new Error(`Public eval scenario ${scenarioIndex} rounds are invalid`);
    }
    if (typeof scenario.scenario_id !== "string" || !/^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(scenario.scenario_id)) {
      throw new Error(`Public eval scenario ${scenarioIndex} scenario_id is invalid`);
    }
    if (scenarioIds.has(scenario.scenario_id)) throw new Error(`Public eval duplicate scenario_id: ${scenario.scenario_id}`);
    scenarioIds.add(scenario.scenario_id);
    if (scenario.document_format !== "docx" && scenario.document_format !== "pptx") {
      throw new Error(`Public eval ${scenario.scenario_id} document_format is invalid`);
    }
    if (
      !Number.isInteger(scenario.max_repair_rounds)
      || Number(scenario.max_repair_rounds) < 0
      || Number(scenario.max_repair_rounds) > 2
      || scenario.rounds.length - 1 > Number(scenario.max_repair_rounds)
    ) {
      throw new Error(`Public eval ${scenario.scenario_id} max_repair_rounds is invalid`);
    }
    if (
      !isRecord(scenario.provenance)
      || scenario.provenance.kind !== "synthetic"
      || scenario.provenance.license !== "CC0-1.0"
    ) {
      throw new Error(`Public eval ${scenario.scenario_id} provenance is invalid`);
    }
    const sourceRelpath = safeRelpath(scenario.source_relpath, `scenario ${scenarioIndex} source_relpath`);
    assertScenarioPath(sourceRelpath, scenario.scenario_id, scenario.document_format, "source_relpath");
    assertUniqueCorpusPath(sourceRelpath, corpusPaths);
    for (const [roundIndex, round] of scenario.rounds.entries()) {
      if (!isRecord(round) || round.candidate_id !== `candidate-r${roundIndex}`) {
        throw new Error(`Public eval scenario ${scenarioIndex} expected candidate-r${roundIndex}`);
      }
      const candidateRelpath = safeRelpath(round.candidate_relpath, `scenario ${scenarioIndex} candidate_relpath`);
      assertScenarioPath(candidateRelpath, scenario.scenario_id, scenario.document_format, "candidate_relpath");
      assertUniqueCorpusPath(candidateRelpath, corpusPaths);
      nonNegativeInteger(round.blocking_count, `${scenario.scenario_id} round ${roundIndex} blocking_count`);
      nonNegativeInteger(round.new_regressions, `${scenario.scenario_id} round ${roundIndex} new_regressions`);
      nonNegativeInteger(round.replay_latency_ms, `${scenario.scenario_id} round ${roundIndex} replay_latency_ms`);
      if (roundIndex === 0 && round.new_regressions !== 0) {
        throw new Error(`Public eval ${scenario.scenario_id} candidate-r0 new_regressions must be zero`);
      }
      if (typeof round.oracle_semantic_pass !== "boolean" || typeof round.oracle_layout_pass !== "boolean") {
        throw new Error(`Public eval ${scenario.scenario_id} round ${roundIndex} oracle labels are invalid`);
      }
    }
  }
  return value as unknown as EvalManifest;
}

function assertScenarioPath(relpath: string, scenarioId: string, format: string, label: string): void {
  if (!relpath.startsWith(`fixtures/${scenarioId}/`) || path.posix.extname(relpath) !== `.${format}`) {
    throw new Error(`Public eval ${scenarioId} ${label} is invalid`);
  }
}

function assertUniqueCorpusPath(relpath: string, seen: Set<string>): void {
  if (seen.has(relpath)) throw new Error(`Public eval duplicate corpus path: ${relpath}`);
  seen.add(relpath);
}

function nonNegativeInteger(value: unknown, label: string): number {
  if (!Number.isInteger(value) || Number(value) < 0) throw new Error(`Public eval ${label} is invalid`);
  return Number(value);
}

function safeRelpath(value: unknown, label: string): string {
  if (typeof value !== "string" || value.length === 0 || value.includes("\\") || path.posix.isAbsolute(value)) {
    throw new Error(`Public eval ${label} is invalid`);
  }
  if (value.split("/").some((part) => part === "" || part === "." || part === "..")) {
    throw new Error(`Public eval ${label} is invalid`);
  }
  return value;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}
