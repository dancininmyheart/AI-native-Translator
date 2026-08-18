import { rm } from "node:fs/promises";
import { afterEach, expect, it } from "vitest";
import { createControlledTools } from "../src/controlled-tools.js";
import type { PythonToolEnvelope, PythonToolName, TranslationJobRecord } from "../src/contracts.js";
import { SafeEventLog } from "../src/event-log.js";
import { JobExecutionContext } from "../src/execution-context.js";
import type { ProviderLimiter } from "../src/provider-limiter.js";
import type { PythonToolBridge } from "../src/python-tool-bridge.js";
import { InMemoryJobRegistry } from "../src/registry.js";
import { testConfig } from "./helpers.js";

const cleanup: string[] = [];
afterEach(async () => Promise.all(cleanup.splice(0).map((value) => rm(value, { recursive: true, force: true }))));

it("enables semantic/processing QA for standard and keeps each repair invocation to one round", async () => {
  const config = await testConfig();
  cleanup.push(config.dataRoot);
  const now = new Date();
  const job: TranslationJobRecord = {
    id: "controlled-job",
    sourceId: "source",
    executionMode: "pi_agent_live",
    sourceFilename: "source.docx",
    inputRelpath: "input/source.docx",
    sourceLanguage: "en",
    targetLanguage: "zh-CN",
    qualityProfile: "standard",
    maxRepairRounds: 2,
    repairRound: 0,
    status: "PREFLIGHT",
    softDeadlineAt: new Date(now.getTime() + 60_000).toISOString(),
    hardDeadlineAt: new Date(now.getTime() + 120_000).toISOString(),
    createdAt: now.toISOString(),
    updatedAt: now.toISOString(),
  };
  const registry = new InMemoryJobRegistry();
  await registry.createJob(job, "controlled-idempotency");
  const calls: Array<{ tool: PythonToolName; payload: Record<string, unknown> }> = [];
  const bridge: PythonToolBridge = {
    execute: async (tool, jobId, payload): Promise<PythonToolEnvelope> => {
      calls.push({ tool, payload });
      const repairOutput = String(payload.output_candidate_id ?? "candidate-r1");
      return {
        tool,
        job_id: jobId,
        status: "COMPLETED",
        summary: tool === "apply_repair"
          ? { output_created: true, output_candidate_id: repairOutput }
          : {},
        artifacts: tool === "apply_repair"
          ? [artifact("candidate_document", `candidate:${repairOutput}`, `candidates/${repairOutput}.docx`)]
          : [],
        issues: tool === "run_document_qa" ? [{ code: "QA_TEST" }] : [],
        error: null,
      };
    },
  };
  const events = new SafeEventLog(config.jobsRoot);
  const context = new JobExecutionContext(job, registry, events, config);
  const permitCounts: number[] = [];
  const limiter: ProviderLimiter = {
    acquire: async (count) => {
      permitCounts.push(count);
      return { release: async () => undefined };
    },
    close: async () => undefined,
  };
  const tools = createControlledTools(context, bridge, events, limiter);
  const invoke = async (name: string, params: Record<string, unknown>) => {
    const tool = tools.find((value) => value.name === name)!;
    await tool.execute(name, params, undefined, undefined, {} as never);
  };
  await invoke("preflight_document", {});
  await invoke("translate_once", {});
  await invoke("run_document_qa", { candidate_id: "candidate-r0" });
  await invoke("apply_repair", { candidate_id: "candidate-r0" });

  expect(calls.find((call) => call.tool === "run_document_qa")!.payload).toMatchObject({
    semantic_qa: true,
    max_processing_concurrency: 4,
  });
  expect(calls.find((call) => call.tool === "apply_repair")!.payload).toMatchObject({
    max_rounds: 1,
    use_processing_provider: true,
  });
  expect(permitCounts).toEqual([4, 4, 1]);
  await expect(
    invoke("apply_repair", { candidate_id: "candidate-r1" }),
  ).rejects.toThrow(/repair requires QA issues/);
});

it("persists validated source and target routing from translate_once", async () => {
  const config = await testConfig();
  cleanup.push(config.dataRoot);
  const now = new Date();
  const job: TranslationJobRecord = {
    id: "controlled-auto-route",
    sourceId: "source",
    executionMode: "pi_agent_live",
    sourceFilename: "source.docx",
    inputRelpath: "input/source.docx",
    sourceLanguage: "auto",
    targetLanguage: "auto",
    requestedTargetLanguage: "auto",
    qualityProfile: "standard",
    maxRepairRounds: 2,
    repairRound: 0,
    status: "PREFLIGHT",
    softDeadlineAt: new Date(now.getTime() + 60_000).toISOString(),
    hardDeadlineAt: new Date(now.getTime() + 120_000).toISOString(),
    createdAt: now.toISOString(),
    updatedAt: now.toISOString(),
  };
  const registry = new InMemoryJobRegistry();
  await registry.createJob(job, "controlled-auto-route-idempotency");
  const bridge: PythonToolBridge = {
    execute: async (tool, jobId) => ({
      tool,
      job_id: jobId,
      status: "COMPLETED",
      summary: tool === "translate_once"
        ? {
            source_language: "ru",
            requested_target_language: "auto",
            resolved_target_language: "zh",
          }
        : {},
      artifacts: [],
      issues: [],
      error: null,
    }),
  };
  const events = new SafeEventLog(config.jobsRoot);
  const context = new JobExecutionContext(job, registry, events, config);
  const limiter: ProviderLimiter = {
    acquire: async () => ({ release: async () => undefined }),
    close: async () => undefined,
  };
  const tools = createControlledTools(context, bridge, events, limiter);
  const invoke = async (name: string) => {
    const tool = tools.find((value) => value.name === name)!;
    await tool.execute(name, {}, undefined, undefined, {} as never);
  };

  await invoke("preflight_document");
  await invoke("translate_once");

  expect(await registry.getJob(job.id)).toMatchObject({
    sourceLanguage: "auto",
    targetLanguage: "auto",
    resolvedSourceLanguage: "ru",
    resolvedTargetLanguage: "zh",
  });
});

it("registers candidate-r0 during pretranslated preflight and forbids translate_once", async () => {
  const config = await testConfig();
  cleanup.push(config.dataRoot);
  const now = new Date();
  const job: TranslationJobRecord = {
    id: "pretranslated-controlled-job",
    sourceId: "source",
    executionMode: "pretranslated_qa",
    candidateSourceId: "candidate-source",
    sourceFilename: "source.doc",
    inputRelpath: "input/source.doc",
    candidateSourceFilename: "translated.docx",
    existingTranslationRelpath: "input/existing-translation.docx",
    sourceLanguage: "en",
    targetLanguage: "zh-CN",
    qualityProfile: "standard",
    maxRepairRounds: 2,
    repairRound: 0,
    status: "PREFLIGHT",
    softDeadlineAt: new Date(now.getTime() + 60_000).toISOString(),
    hardDeadlineAt: new Date(now.getTime() + 120_000).toISOString(),
    createdAt: now.toISOString(),
    updatedAt: now.toISOString(),
  };
  const registry = new InMemoryJobRegistry();
  await registry.createJob(job, "pretranslated-controlled-idempotency");
  const calls: Array<{ tool: PythonToolName; payload: Record<string, unknown> }> = [];
  const bridge: PythonToolBridge = {
    execute: async (tool, jobId, payload) => {
      calls.push({ tool, payload });
      return { tool, job_id: jobId, status: "COMPLETED", summary: {}, artifacts: [], issues: [], error: null };
    },
  };
  const events = new SafeEventLog(config.jobsRoot);
  const context = new JobExecutionContext(job, registry, events, config);
  const limiter: ProviderLimiter = {
    acquire: async () => ({ release: async () => undefined }),
    close: async () => undefined,
  };
  const tools = createControlledTools(context, bridge, events, limiter);
  const preflight = tools.find((tool) => tool.name === "preflight_document")!;
  const translate = tools.find((tool) => tool.name === "translate_once")!;
  await preflight.execute("preflight", {}, undefined, undefined, {} as never);
  expect(context.job.status).toBe("QA");
  expect(context.job.candidateId).toBe("candidate-r0");
  expect(calls[0]!.payload).toEqual({
    input_relpath: "input/source.doc",
    existing_translation_relpath: "input/existing-translation.docx",
    candidate_id: "candidate-r0",
    source_language: "en",
    target_language: "zh-CN",
  });
  await expect(translate.execute("translate", {}, undefined, undefined, {} as never)).rejects.toThrow(/forbidden/);
  expect(calls.filter((call) => call.tool === "translate_once")).toHaveLength(0);
});

it.each([
  {
    name: "missing exact candidate artifact",
    expectedCode: "repair_no_candidate",
    expectedSummary: "Repair did not produce an accepted candidate-r1",
    response: {
      status: "NEEDS_REVIEW",
      summary: { output_created: true, output_candidate_id: "candidate-r1" },
      artifacts: [artifact("candidate_document", "candidate:wrong", "candidates/wrong.docx")],
      issues: [{ severity: "error", code: "REPAIR_NO_OUTPUT" }],
      error: null,
    },
  },
  {
    name: "failed repair envelope",
    expectedCode: "repair_agent_failed",
    expectedSummary: "repair failed safely",
    response: {
      status: "FAILED",
      summary: { output_created: false, output_candidate_id: "candidate-r1" },
      artifacts: [],
      issues: [{ severity: "error", code: "REPAIR_AGENT_FAILED" }],
      error: { code: "REPAIR_AGENT_FAILED", message: "repair failed safely" },
    },
  },
  {
    name: "unknown repair error",
    expectedCode: "repair_no_candidate",
    expectedSummary: "unrecognized repair failure",
    response: {
      status: "NEEDS_REVIEW",
      summary: { output_created: false, output_candidate_id: "candidate-r1" },
      artifacts: [],
      issues: [],
      error: { code: "NEW_REPAIR_FAILURE", message: "unrecognized repair failure" },
    },
  },
  {
    name: "rejected regression repair",
    expectedCode: "repair_not_improved",
    expectedSummary: "repair regressed full QA",
    response: {
      status: "NEEDS_REVIEW",
      summary: {
        output_created: false,
        output_candidate_id: "candidate-r1",
        round_outcome: "rejected_regression",
      },
      artifacts: [],
      issues: [{ severity: "error", code: "REPAIR_NOT_IMPROVED" }],
      error: { code: "REPAIR_NOT_IMPROVED", message: "repair regressed full QA" },
    },
  },
])("retains the parent as NEEDS_REVIEW for $name", async ({
  name,
  response,
  expectedCode,
  expectedSummary,
}) => {
  const config = await testConfig();
  cleanup.push(config.dataRoot);
  const now = new Date();
  const job: TranslationJobRecord = {
    id: `repair-no-candidate-${name.replaceAll(" ", "-")}`,
    sourceId: "source",
    executionMode: "pi_agent_live",
    sourceFilename: "source.docx",
    inputRelpath: "input/source.docx",
    sourceLanguage: "en",
    targetLanguage: "zh",
    qualityProfile: "standard",
    maxRepairRounds: 2,
    repairRound: 0,
    candidateId: "candidate-r0",
    qaQualityScore: 90,
    qaIssueCounts: { error: 1, warning: 0, info: 0 },
    qaBlockingIssueFingerprints: ["a".repeat(64)],
    qaBlockingIssueFingerprintOverflow: false,
    status: "QA",
    softDeadlineAt: new Date(now.getTime() + 60_000).toISOString(),
    hardDeadlineAt: new Date(now.getTime() + 120_000).toISOString(),
    createdAt: now.toISOString(),
    updatedAt: now.toISOString(),
  };
  const registry = new InMemoryJobRegistry();
  await registry.createJob(job, "repair-no-candidate-idempotency");
  const bridge: PythonToolBridge = {
    execute: async (tool, jobId) => ({
      tool,
      job_id: jobId,
      ...response,
    }),
  };
  const events = new SafeEventLog(config.jobsRoot);
  const context = new JobExecutionContext(job, registry, events, config);
  const limiter: ProviderLimiter = {
    acquire: async () => ({ release: async () => undefined }),
    close: async () => undefined,
  };
  const repair = createControlledTools(context, bridge, events, limiter)
    .find((tool) => tool.name === "apply_repair")!;

  await repair.execute(
    "repair",
    { candidate_id: "candidate-r0" },
    undefined,
    undefined,
    {} as never,
  );

  const persisted = await registry.getJob(job.id);
  expect(persisted).toMatchObject({
    status: "NEEDS_REVIEW",
    errorCode: expectedCode,
    errorSummary: expect.stringContaining(expectedSummary),
    candidateId: "candidate-r0",
    repairRound: 0,
    qaQualityScore: 90,
  });
  expect(persisted?.errorSummary).toContain("retained candidate-r0");
});

it("keeps blocking/full QA evidence authoritative across verification and finalization", async () => {
  const config = await testConfig();
  cleanup.push(config.dataRoot);
  const now = new Date();
  const job: TranslationJobRecord = {
    id: "qa-report-authority",
    sourceId: "source",
    executionMode: "pi_agent_live",
    sourceFilename: "source.docx",
    inputRelpath: "input/source.docx",
    sourceLanguage: "en",
    targetLanguage: "zh",
    qualityProfile: "standard",
    maxRepairRounds: 2,
    repairRound: 0,
    candidateId: "candidate-r0",
    status: "QA",
    softDeadlineAt: new Date(now.getTime() + 60_000).toISOString(),
    hardDeadlineAt: new Date(now.getTime() + 120_000).toISOString(),
    createdAt: now.toISOString(),
    updatedAt: now.toISOString(),
  };
  const registry = new InMemoryJobRegistry();
  await registry.createJob(job, "qa-report-authority-idempotency");
  const bridge: PythonToolBridge = {
    execute: async (tool, jobId): Promise<PythonToolEnvelope> => {
      if (tool === "run_document_qa") {
        return {
          tool,
          job_id: jobId,
          status: "COMPLETED",
          summary: {
            candidate_id: "candidate-r0",
            quality_score: 98,
            issue_counts: { error: 0, warning: 1, info: 0 },
          },
          artifacts: [artifact("qa_report", "qa:candidate-r0", "reports/qa/candidate-r0.json")],
          issues: [{ severity: "warning", code: "STYLE", block_id: "paragraph-1" }],
          error: null,
        };
      }
      if (tool === "verify_candidate") {
        return {
          tool,
          job_id: jobId,
          status: "COMPLETED",
          summary: {
            candidate_id: "candidate-r0",
            quality_score: 100,
            issue_counts: { error: 0, warning: 0, info: 0 },
          },
          artifacts: [artifact(
            "verification_report",
            "verification:candidate-r0",
            "reports/verification/candidate-r0.json",
          )],
          issues: [],
          error: null,
        };
      }
      return {
        tool,
        job_id: jobId,
        status: "COMPLETED",
        summary: { candidate_id: "candidate-r0", finalized: true },
        artifacts: [artifact("translated_document", "final_result", "results/translated.docx")],
        issues: [],
        error: null,
      };
    },
  };
  const events = new SafeEventLog(config.jobsRoot);
  const context = new JobExecutionContext(job, registry, events, config);
  const limiter: ProviderLimiter = {
    acquire: async () => ({ release: async () => undefined }),
    close: async () => undefined,
  };
  const tools = createControlledTools(context, bridge, events, limiter);
  const invoke = async (name: string, params: Record<string, unknown>) => {
    const tool = tools.find((value) => value.name === name)!;
    await tool.execute(name, params, undefined, undefined, {} as never);
  };

  await invoke("run_document_qa", { candidate_id: "candidate-r0" });
  await invoke("verify_candidate", { candidate_id: "candidate-r0" });
  await invoke("finalize_result", { candidate_id: "candidate-r0" });

  expect(await registry.getJob(job.id)).toMatchObject({
    status: "COMPLETED_WITH_WARNINGS",
    qaIssueCounts: { error: 0, warning: 1, info: 0 },
    reportRelpath: "reports/qa/candidate-r0.json",
    warnings: ["qa_issue_count:1"],
  });
});

it("forbids package verification while full QA still has blocking errors", async () => {
  const config = await testConfig();
  cleanup.push(config.dataRoot);
  const now = new Date();
  const job: TranslationJobRecord = {
    id: "blocking-qa-verification",
    sourceId: "source",
    executionMode: "pi_agent_live",
    sourceFilename: "source.docx",
    inputRelpath: "input/source.docx",
    sourceLanguage: "en",
    targetLanguage: "zh",
    qualityProfile: "standard",
    maxRepairRounds: 2,
    repairRound: 0,
    candidateId: "candidate-r0",
    qaQualityScore: 90,
    qaIssueCounts: { error: 1, warning: 0, info: 0 },
    qaBlockingIssueFingerprints: ["a".repeat(64)],
    qaBlockingIssueFingerprintOverflow: false,
    reportRelpath: "reports/qa/candidate-r0.json",
    status: "QA",
    softDeadlineAt: new Date(now.getTime() + 60_000).toISOString(),
    hardDeadlineAt: new Date(now.getTime() + 120_000).toISOString(),
    createdAt: now.toISOString(),
    updatedAt: now.toISOString(),
  };
  const registry = new InMemoryJobRegistry();
  await registry.createJob(job, "blocking-qa-verification-idempotency");
  const calls: PythonToolName[] = [];
  const bridge: PythonToolBridge = {
    execute: async (tool, jobId) => {
      calls.push(tool);
      return {
        tool,
        job_id: jobId,
        status: "COMPLETED",
        summary: {},
        artifacts: [],
        issues: [],
        error: null,
      };
    },
  };
  const events = new SafeEventLog(config.jobsRoot);
  const context = new JobExecutionContext(job, registry, events, config);
  const limiter: ProviderLimiter = {
    acquire: async () => ({ release: async () => undefined }),
    close: async () => undefined,
  };
  const verify = createControlledTools(context, bridge, events, limiter)
    .find((tool) => tool.name === "verify_candidate")!;

  await expect(verify.execute(
    "verify",
    { candidate_id: "candidate-r0" },
    undefined,
    undefined,
    {} as never,
  )).rejects.toThrow(/without blocking issues/);
  expect(calls).toEqual([]);
  expect((await registry.getJob(job.id))?.status).toBe("QA");
});

function artifact(
  kind: string,
  artifactId: string,
  relpath: string,
): PythonToolEnvelope["artifacts"][number] {
  return {
    artifact_id: artifactId,
    kind,
    relpath,
    filename: relpath.split("/").at(-1)!,
    size_bytes: 1,
    sha256: "0".repeat(64),
  };
}
