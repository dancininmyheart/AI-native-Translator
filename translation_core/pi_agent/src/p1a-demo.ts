import path from "node:path";
import { createHash } from "node:crypto";
import { existsSync } from "node:fs";
import { spawn } from "node:child_process";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { Readable } from "node:stream";
import type { AppConfig } from "./config.js";
import type { JobStatus, QueueAdapter, QueuePayload, TranslationJobRecord } from "./contracts.js";
import { buildApi } from "./api.js";
import { AgentJobRunner } from "./job-runner.js";
import { NoopProviderLimiter } from "./provider-limiter.js";
import { InMemoryJobRegistry } from "./registry.js";
import { StorageService } from "./storage.js";
import { PythonCliToolBridge } from "./python-tool-bridge.js";
import { P1aDemoPythonBridge, P1aFixtureTranslationBridge } from "./p1a-demo-bridge.js";
import {
  P1A_SCENARIO_IDS,
  type P1aDemoSummary,
  type P1aFixtureManifest,
  type P1aFixtureScenario,
  type P1aScenarioSummary,
} from "./p1a-demo-types.js";

const SHA256 = /^[a-f0-9]{64}$/;

export interface P1aDemoOptions {
  outputDir: string;
  pythonExecutable?: string;
}

export async function runP1aDemo(options: P1aDemoOptions): Promise<P1aDemoSummary> {
  const outputDir = path.resolve(options.outputDir);
  await mkdir(outputDir, { recursive: true });
  const repoRoot = resolveRepositoryRoot();
  const pythonExecutable = options.pythonExecutable ?? resolvePythonExecutable(repoRoot);
  await generateFixtures(repoRoot, outputDir, pythonExecutable);
  const manifest = parseManifest(JSON.parse(await readFile(path.join(outputDir, "manifest.json"), "utf8")));
  const scenarios: P1aScenarioSummary[] = [];
  for (const fixture of manifest.scenarios) {
    scenarios.push(await runScenario({ fixture, outputDir, repoRoot, pythonExecutable }));
  }
  const summary: P1aDemoSummary = {
    schema_version: 1,
    demo: "p1a-three-path-offline",
    pi_session: false,
    model_calls: 0,
    scenario_count: 3,
    scenarios,
  };
  await writeFile(path.join(outputDir, "demo-trace.json"), `${JSON.stringify(summary, null, 2)}\n`, {
    encoding: "utf8",
    mode: 0o600,
  });
  return summary;
}

async function runScenario(input: {
  fixture: P1aFixtureScenario;
  outputDir: string;
  repoRoot: string;
  pythonExecutable: string;
}): Promise<P1aScenarioSummary> {
  const startedAt = performance.now();
  const { fixture, outputDir, repoRoot, pythonExecutable } = input;
  const dataRoot = path.join(outputDir, "runs", fixture.scenario_id);
  const config = demoConfig(dataRoot, repoRoot, pythonExecutable);
  const registry = new InMemoryJobRegistry();
  const storage = new StorageService(config);
  await storage.initialize();
  const candidateSteps = fixture.candidate_relpaths.map((relpath, index) => ({
    sourcePath: resolveManifestPath(outputDir, relpath),
    sha256: fixture.candidate_sha256[relpath]!,
    blockingCount: fixture.expected_blocking_counts[index]!,
  }));
  const bridge = fixture.scenario_id === "direct-docx"
    ? new P1aFixtureTranslationBridge(
        config,
        resolveManifestPath(outputDir, fixture.fixture_relpath ?? missingFixturePath(fixture.scenario_id)),
      )
    : fixture.evidence_kind === "scripted_fixture_candidate"
      ? new P1aDemoPythonBridge(config, candidateSteps)
      : new PythonCliToolBridge(config);
  const runner = new AgentJobRunner(config, registry, bridge, null, new NoopProviderLimiter());
  const queue = new DeferredDemoQueue();
  const app = await buildApi({ config, registry, storage, runner, queue });
  try {
    const source = await storage.ingest(
      path.basename(fixture.source_relpath),
      Readable.from(await readFile(resolveManifestPath(outputDir, fixture.source_relpath))),
    );
    await registry.putSource(source);
    let candidateSourceId: string | undefined;
    if (fixture.scenario_id !== "direct-docx") {
      const candidate = await storage.ingest(
        path.basename(fixture.candidate_relpaths[0]!),
        Readable.from(await readFile(candidateSteps[0]!.sourcePath)),
      );
      await registry.putSource(candidate);
      candidateSourceId = candidate.id;
    }
    const request = fixture.scenario_id === "direct-docx"
      ? {
          source_id: source.id,
          execution_mode: "pi_agent_live",
          source_language: "en",
          target_language: "zh",
          quality_profile: "fast",
          max_repair_rounds: 0,
        }
      : {
          source_id: source.id,
          execution_mode: "pretranslated_qa",
          candidate_source_id: candidateSourceId,
          source_language: "en",
          target_language: "zh",
          quality_profile: "fast",
          max_repair_rounds: fixture.candidate_relpaths.length - 1,
        };
    const created = await app.inject({
      method: "POST",
      url: "/v1/translation-jobs",
      headers: { "idempotency-key": `public-p1a-${fixture.scenario_id}` },
      payload: request,
    });
    if (created.statusCode !== 202) throw new Error(`P1-A create failed for ${fixture.scenario_id}: HTTP ${created.statusCode}`);
    const jobId = String(created.json().job_id);
    await queue.run(jobId, runner);
    const job = await requireTerminalJob(registry, jobId);
    const finalExpectedBlockingCount = fixture.expected_blocking_counts.at(-1)!;
    if (job.qaIssueCounts?.error !== finalExpectedBlockingCount) {
      throw new Error(
        `P1-A ${fixture.scenario_id} final Python QA mismatch: expected ${finalExpectedBlockingCount}, got ${String(job.qaIssueCounts?.error)}`,
      );
    }
    const result = await app.inject({ method: "GET", url: `/v1/translation-jobs/${jobId}/result` });
    const resultSha256 = result.statusCode === 200 ? sha256(result.rawPayload) : null;
    const selectedCandidateId = job.candidateId ?? "";
    const selectedRound = candidateRound(selectedCandidateId);
    const selectedManifestPath = fixture.candidate_relpaths[selectedRound];
    const expectedHash = fixture.scenario_id === "direct-docx"
      ? null
      : fixture.candidate_sha256[selectedManifestPath!]!;
    const selectedHash = fixture.scenario_id === "direct-docx" ? resultSha256! : expectedHash!;
    assertScenarioOutcome(fixture, job, result.statusCode, resultSha256, selectedHash);
    const eventText = await readFile(path.join(config.jobsRoot, jobId, "agent-events.jsonl"), "utf8");
    const events = eventText.trim().split(/\r?\n/).filter(Boolean).map((line) => JSON.parse(line) as Record<string, unknown>);
    const stateTransitions = events
      .filter((event) => event.type === "status" && typeof event.status === "string")
      .map((event) => String(event.status));
    const toolSequence = events
      .filter((event) => event.type === "tool_start" && typeof event.tool === "string")
      .map((event) => String(event.tool));
    return {
      scenario_id: fixture.scenario_id,
      evidence_kind: fixture.evidence_kind,
      native_python_qa: true,
      repair_execution: fixture.repair_execution,
      pi_session: false,
      model_calls: 0,
      terminal_status: fixture.expected_terminal_status,
      result_http_status: fixture.expected_result_http_status,
      blocking_counts: fixture.expected_blocking_counts,
      quality_scores: fixture.expected_blocking_counts.map((count) => Math.max(0, 100 - Math.min(70, count * 10))),
      repair_decisions: repairDecisions(fixture),
      repair_rounds: job.repairRound,
      max_repair_rounds: job.maxRepairRounds,
      selected_candidate_id: selectedCandidateId,
      selected_candidate_sha256: selectedHash,
      expected_candidate_sha256: expectedHash,
      result_sha256: resultSha256,
      elapsed_ms: Math.round(performance.now() - startedAt),
      state_transitions: stateTransitions,
      tool_sequence: toolSequence,
    };
  } finally {
    await app.close();
    await queue.close();
    await registry.close();
  }
}

class DeferredDemoQueue implements QueueAdapter {
  private readonly waiting: QueuePayload[] = [];

  async add(payload: QueuePayload): Promise<void> {
    this.waiting.push(payload);
  }

  async ensureRunnable(payload: QueuePayload): Promise<void> {
    await this.add(payload);
  }

  async cancel(jobId: string): Promise<boolean> {
    const index = this.waiting.findIndex((payload) => payload.jobId === jobId);
    if (index < 0) return false;
    this.waiting.splice(index, 1);
    return true;
  }

  async run(jobId: string, runner: AgentJobRunner): Promise<void> {
    const index = this.waiting.findIndex((payload) => payload.jobId === jobId);
    if (index < 0) throw new Error("P1-A deferred job is missing");
    this.waiting.splice(index, 1);
    await runner.run(jobId);
  }

  async close(): Promise<void> {
    this.waiting.splice(0);
  }
}

function demoConfig(dataRoot: string, repoRoot: string, pythonExecutable: string): AppConfig {
  return {
    host: "127.0.0.1",
    port: 0,
    fixtureMode: true,
    dataRoot,
    jobsRoot: path.join(dataRoot, "jobs"),
    uploadsRoot: path.join(dataRoot, "uploads"),
    skillDir: path.join(repoRoot, "document-translation"),
    redisUrl: "redis://127.0.0.1:6379/15",
    queueName: "p1a-offline-demo",
    jobConcurrency: 1,
    rateLimitMax: 1,
    rateLimitDurationMs: 1000,
    translationConcurrency: 1,
    providerMaxConcurrency: 1,
    semanticQaEnabled: false,
    processingRepairEnabled: false,
    softTimeoutMs: 27 * 60 * 1000,
    hardTimeoutMs: 35 * 60 * 1000,
    defaultRepairRounds: 2,
    hardMaxRepairRounds: 3,
    maxInputBytes: 50 * 1024 * 1024,
    pythonExecutable,
    pythonModule: "format_translator.agent_tool_cli",
    pythonToolTimeoutMs: 120_000,
    sofficeExecutable: "soffice",
    piAgentDir: path.join(dataRoot, "pi-config"),
    piModelProvider: "offline-demo-unused",
    piThinkingLevel: "off",
  };
}

async function generateFixtures(repoRoot: string, outputDir: string, pythonExecutable: string): Promise<void> {
  const script = path.join(repoRoot, "translation_core", "tools", "p1a_demo_fixtures.py");
  await runProcess(pythonExecutable, [script, "--output-dir", outputDir], path.join(repoRoot, "translation_core"));
}

async function runProcess(executable: string, argv: string[], cwd: string): Promise<void> {
  const child = spawn(executable, argv, {
    cwd,
    shell: false,
    windowsHide: true,
    env: { ...process.env, PYTHONUNBUFFERED: "1" },
  });
  let stderr = "";
  child.stderr.on("data", (chunk: Buffer) => {
    stderr = `${stderr}${chunk.toString("utf8")}`.slice(-4_000);
  });
  const code = await new Promise<number | null>((resolve, reject) => {
    child.once("error", reject);
    child.once("close", resolve);
  });
  if (code !== 0) throw new Error(`P1-A fixture generation failed (exit ${String(code)}): ${stderr.trim()}`);
}

function parseManifest(value: unknown): P1aFixtureManifest {
  if (!isRecord(value) || value.schema_version !== 1 || value.model_calls !== 0 || !Array.isArray(value.scenarios)) {
    throw new Error("P1-A fixture manifest header is invalid");
  }
  if (value.generated_by !== "translation_core/tools/p1a_demo_fixtures.py") {
    throw new Error("P1-A fixture manifest generator identity is invalid");
  }
  const scenarios = value.scenarios.map(parseScenario);
  if (scenarios.map((scenario) => scenario.scenario_id).join(",") !== P1A_SCENARIO_IDS.join(",")) {
    throw new Error("P1-A fixture manifest must contain the three fixed scenarios in order");
  }
  return { schema_version: 1, generated_by: value.generated_by, model_calls: 0, scenarios };
}

function parseScenario(value: unknown): P1aFixtureScenario {
  if (!isRecord(value) || !P1A_SCENARIO_IDS.includes(value.scenario_id as never)) {
    throw new Error("P1-A scenario identity is invalid");
  }
  const scenarioId = value.scenario_id as P1aFixtureScenario["scenario_id"];
  const candidateRelpaths = stringArray(value.candidate_relpaths, "candidate_relpaths");
  const blockingCounts = integerArray(value.expected_blocking_counts, "expected_blocking_counts");
  if (candidateRelpaths.length !== blockingCounts.length) throw new Error(`P1-A ${scenarioId} round counts do not align`);
  if (!isRecord(value.candidate_sha256)) throw new Error(`P1-A ${scenarioId} candidate hashes are invalid`);
  const candidateSha256: Record<string, string> = {};
  for (const relpath of candidateRelpaths) {
    const digest = value.candidate_sha256[relpath];
    if (typeof digest !== "string" || !SHA256.test(digest)) throw new Error(`P1-A ${scenarioId} candidate hash is invalid`);
    candidateSha256[relpath] = digest;
  }
  const expectedStatus = value.expected_terminal_status;
  const resultStatus = value.expected_result_http_status;
  const evidenceKind = value.evidence_kind;
  if ((expectedStatus !== "COMPLETED" && expectedStatus !== "NEEDS_REVIEW") || (resultStatus !== 200 && resultStatus !== 409)) {
    throw new Error(`P1-A ${scenarioId} expected outcome is invalid`);
  }
  if (evidenceKind !== "real_python_core" && evidenceKind !== "scripted_fixture_candidate") {
    throw new Error(`P1-A ${scenarioId} evidence kind is invalid`);
  }
  if (value.native_python_qa !== true) throw new Error(`P1-A ${scenarioId} must require native Python QA`);
  const repairExecution = value.repair_execution;
  if (repairExecution !== "not_applicable" && repairExecution !== "scripted_fixture_candidate") {
    throw new Error(`P1-A ${scenarioId} repair execution label is invalid`);
  }
  const sourceRelpath = safeRelpath(value.source_relpath, "source_relpath");
  const documentFormat = value.document_format;
  if (documentFormat !== "docx" && documentFormat !== "pptx") throw new Error(`P1-A ${scenarioId} format is invalid`);
  const fixtureRelpath = value.fixture_relpath === undefined ? undefined : safeRelpath(value.fixture_relpath, "fixture_relpath");
  return {
    scenario_id: scenarioId,
    document_format: documentFormat,
    source_relpath: sourceRelpath,
    candidate_relpaths: candidateRelpaths.map((item) => safeRelpath(item, "candidate_relpaths")),
    expected_terminal_status: expectedStatus,
    expected_result_http_status: resultStatus,
    expected_blocking_counts: blockingCounts,
    candidate_sha256: candidateSha256,
    evidence_kind: evidenceKind,
    native_python_qa: true,
    repair_execution: repairExecution,
    ...(fixtureRelpath ? { fixture_relpath: fixtureRelpath } : {}),
  };
}

async function requireTerminalJob(registry: InMemoryJobRegistry, jobId: string): Promise<TranslationJobRecord> {
  const job = await registry.getJob(jobId);
  if (!job || !new Set<JobStatus>(["COMPLETED", "COMPLETED_WITH_WARNINGS", "NEEDS_REVIEW", "FAILED", "CANCELLED"]).has(job.status)) {
    throw new Error(`P1-A job did not reach a terminal state: ${job?.status ?? "missing"}`);
  }
  return job;
}

function assertScenarioOutcome(
  fixture: P1aFixtureScenario,
  job: TranslationJobRecord,
  resultStatus: number,
  resultHash: string | null,
  selectedHash: string,
): void {
  if (job.status !== fixture.expected_terminal_status) {
    throw new Error(`P1-A ${fixture.scenario_id} terminal mismatch: expected ${fixture.expected_terminal_status}, got ${job.status}`);
  }
  if (resultStatus !== fixture.expected_result_http_status) {
    throw new Error(`P1-A ${fixture.scenario_id} result HTTP mismatch: expected ${fixture.expected_result_http_status}, got ${resultStatus}`);
  }
  if (resultStatus === 200 && resultHash !== selectedHash) {
    throw new Error(`P1-A ${fixture.scenario_id} download hash does not match the selected candidate`);
  }
  if (resultStatus === 409 && resultHash !== null) throw new Error(`P1-A ${fixture.scenario_id} exposed a blocked result`);
}

function resolveManifestPath(root: string, relpath: string): string {
  const resolved = path.resolve(root, relpath);
  const relative = path.relative(root, resolved);
  if (relative.startsWith("..") || path.isAbsolute(relative)) throw new Error("P1-A manifest path escapes the output directory");
  return resolved;
}

function safeRelpath(value: unknown, label: string): string {
  if (typeof value !== "string" || value.length < 1 || value.includes("\\") || path.posix.isAbsolute(value)) {
    throw new Error(`P1-A ${label} is invalid`);
  }
  if (value.split("/").some((part) => part === "" || part === "." || part === "..")) throw new Error(`P1-A ${label} is invalid`);
  return value;
}

function stringArray(value: unknown, label: string): string[] {
  if (!Array.isArray(value) || value.length < 1 || !value.every((item) => typeof item === "string")) {
    throw new Error(`P1-A ${label} is invalid`);
  }
  return value as string[];
}

function integerArray(value: unknown, label: string): number[] {
  if (!Array.isArray(value) || value.length < 1 || !value.every((item) => Number.isInteger(item) && Number(item) >= 0)) {
    throw new Error(`P1-A ${label} is invalid`);
  }
  return value as number[];
}

function candidateRound(candidateId: string): number {
  const match = /^candidate-r([0-3])$/.exec(candidateId);
  if (!match) throw new Error(`P1-A selected candidate is invalid: ${candidateId}`);
  return Number(match[1]);
}

function repairDecisions(fixture: P1aFixtureScenario): string[] {
  if (fixture.candidate_relpaths.length === 1) return ["not_needed"];
  const decisions = fixture.candidate_relpaths.slice(1).map((_value, index) => `promote:candidate-r${index + 1}`);
  if (fixture.expected_terminal_status === "NEEDS_REVIEW") decisions.push("stop:budget_exhausted");
  return decisions;
}

function sha256(value: Buffer): string {
  return createHash("sha256").update(value).digest("hex");
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

export function resolveRepositoryRoot(start = path.dirname(fileURLToPath(import.meta.url))): string {
  let current = path.resolve(start);
  for (let depth = 0; depth < 8; depth += 1) {
    if (
      existsSync(path.join(current, "translation_core", "pyproject.toml")) &&
      existsSync(path.join(current, "document-translation", "SKILL.md"))
    ) return current;
    const parent = path.dirname(current);
    if (parent === current) break;
    current = parent;
  }
  throw new Error("P1-A demo could not locate the repository root");
}

function resolvePythonExecutable(repoRoot: string): string {
  const candidates = process.platform === "win32"
    ? [path.join(repoRoot, "translation_core", ".venv", "Scripts", "python.exe"), "python"]
    : [path.join(repoRoot, "translation_core", ".venv", "bin", "python"), "python3"];
  return candidates[0]!;
}

function missingFixturePath(scenarioId: string): never {
  throw new Error(`P1-A ${scenarioId} fixture path is missing`);
}
