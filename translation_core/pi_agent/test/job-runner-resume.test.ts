import path from "node:path";
import { createHash } from "node:crypto";
import { mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { afterEach, expect, it } from "vitest";
import type { AgentSession, ToolDefinition } from "@earendil-works/pi-coding-agent";
import type { PythonToolEnvelope, PythonToolName, TranslationJobRecord } from "../src/contracts.js";
import { AgentJobRunner, safeResumeStatus } from "../src/job-runner.js";
import type { SessionFactory } from "../src/pi-session.js";
import { NoopProviderLimiter } from "../src/provider-limiter.js";
import type { PythonToolBridge } from "../src/python-tool-bridge.js";
import { InMemoryJobRegistry } from "../src/registry.js";
import { testConfig } from "./helpers.js";

const cleanup: string[] = [];
afterEach(async () => Promise.all(cleanup.splice(0).map((value) => rm(value, { recursive: true, force: true }))));

it("resumes REPAIRING at QA without replaying preflight or translation", async () => {
  const config = await testConfig({ fixtureMode: false });
  cleanup.push(config.dataRoot);
  const registry = new InMemoryJobRegistry();
  const now = new Date();
  const originalStartedAt = new Date(now.getTime() - 10_000).toISOString();
  const job: TranslationJobRecord = {
    id: "resume-job",
    sourceId: "source",
    executionMode: "pi_agent_live",
    sourceFilename: "source.docx",
    inputRelpath: "input/source.docx",
    sourceLanguage: "en",
    targetLanguage: "zh-CN",
    qualityProfile: "standard",
    maxRepairRounds: 2,
    repairRound: 1,
    candidateId: "candidate-r1",
    status: "REPAIRING",
    softDeadlineAt: new Date(now.getTime() + 60_000).toISOString(),
    hardDeadlineAt: new Date(now.getTime() + 120_000).toISOString(),
    createdAt: originalStartedAt,
    updatedAt: originalStartedAt,
    startedAt: originalStartedAt,
  };
  await registry.createJob(job, "resume-idempotency");
  const calls: PythonToolName[] = [];
  const bridge: PythonToolBridge = {
    execute: async (tool, jobId): Promise<PythonToolEnvelope> => {
      calls.push(tool);
      const artifacts = tool === "run_document_qa"
        ? [artifact("qa_report", "reports/qa.json")]
        : tool === "verify_candidate"
          ? [artifact("verification_report", "reports/verification.json")]
          : tool === "finalize_result"
            ? [artifact("translated_document", "results/translated.docx")]
            : [];
      return {
        tool,
        job_id: jobId,
        status: "COMPLETED",
        summary: {},
        artifacts,
        issues: tool === "verify_candidate" ? [{ code: "VERIFY_WARNING", severity: "warning" }] : [],
        error: null,
      };
    },
  };
  let prompt = "";
  const sessionFactory: SessionFactory = {
    probe: async () => undefined,
    create: async (_cwd, tools: ToolDefinition[]) => {
      const invoke = async (name: string) => {
        const tool = tools.find((value) => value.name === name)!;
        await tool.execute(name, { candidate_id: "candidate-r1" }, undefined, undefined, {} as never);
      };
      const session = {
        subscribe: () => () => undefined,
        prompt: async (value: string) => {
          prompt = value;
          await invoke("run_document_qa");
          await invoke("verify_candidate");
          await invoke("finalize_result");
        },
        waitForIdle: async () => undefined,
        abort: async () => undefined,
        dispose: () => undefined,
      } as unknown as AgentSession;
      return { session, dispose: () => undefined };
    },
  };
  const runner = new AgentJobRunner(config, registry, bridge, sessionFactory, new NoopProviderLimiter());
  await runner.run(job.id);
  const result = await registry.getJob(job.id);
  expect(result?.status).toBe("COMPLETED_WITH_WARNINGS");
  expect(result?.warnings).toEqual(["verification_issue_count:1"]);
  expect(result?.startedAt).toBe(originalStartedAt);
  expect(result?.repairRound).toBe(1);
  expect(calls).toEqual(["run_document_qa", "verify_candidate", "finalize_result"]);
  expect(prompt).toContain("running QA first");
  expect(prompt).toContain("never call preflight_document or translate_once");
  expect(safeResumeStatus("TRANSLATING")).toBe("PREFLIGHT");
  expect(safeResumeStatus("VERIFYING")).toBe("QA");
});

it("settles from durable QA state when the QA bridge exits after recording its result", async () => {
  const config = await testConfig({ fixtureMode: false });
  cleanup.push(config.dataRoot);
  const registry = new InMemoryJobRegistry();
  const now = new Date();
  const job: TranslationJobRecord = {
    id: "durable-qa-job",
    sourceId: "source",
    executionMode: "pi_agent_live",
    sourceFilename: "source.docx",
    inputRelpath: "input/source.docx",
    sourceLanguage: "auto",
    targetLanguage: "auto",
    qualityProfile: "standard",
    maxRepairRounds: 2,
    repairRound: 0,
    status: "QUEUED",
    softDeadlineAt: new Date(now.getTime() + 60_000).toISOString(),
    hardDeadlineAt: new Date(now.getTime() + 120_000).toISOString(),
    createdAt: now.toISOString(),
    updatedAt: now.toISOString(),
  };
  await registry.createJob(job, "durable-qa-idempotency");

  const jobRoot = path.join(config.jobsRoot, job.id);
  const candidateRelpath = "candidates/candidate-r0.docx";
  const reportRelpath = "reports/qa/candidate-r0.json";
  const candidateBytes = Buffer.from("registered candidate", "utf8");
  const candidateSha256 = createHash("sha256").update(candidateBytes).digest("hex");
  const sourceBytes = Buffer.from("durable QA input", "utf8");
  const sourceSha256 = createHash("sha256").update(sourceBytes).digest("hex");
  const normalizedBytes = Buffer.from("durable QA normalized", "utf8");
  const normalizedSha256 = createHash("sha256").update(normalizedBytes).digest("hex");
  const normalizedRelpath = "normalized/source.docx";
  const bridge: PythonToolBridge = {
    execute: async (tool, jobId): Promise<PythonToolEnvelope> => {
      if (tool === "translate_once") {
        await mkdir(path.join(jobRoot, "candidates"), { recursive: true });
        await writeFile(path.join(jobRoot, ...candidateRelpath.split("/")), candidateBytes);
        return {
          ...completedEnvelope(tool, jobId, [artifact("candidate_document", candidateRelpath)]),
          summary: {
            requested_target_language: "auto",
            resolved_source_language: "en",
            resolved_target_language: "zh",
          },
        };
      }
      if (tool === "run_document_qa") {
        await mkdir(path.join(jobRoot, "input"), { recursive: true });
        await mkdir(path.join(jobRoot, "normalized"), { recursive: true });
        await mkdir(path.join(jobRoot, "reports", "qa"), { recursive: true });
        await writeFile(path.join(jobRoot, "input", "source.docx"), sourceBytes);
        await writeFile(path.join(jobRoot, "normalized", "source.docx"), normalizedBytes);
        await writeFile(
          path.join(jobRoot, ...reportRelpath.split("/")),
          JSON.stringify({
            job_id: job.id,
            candidate_id: "candidate-r0",
            status: "NEEDS_REVIEW",
            candidate_sha256: candidateSha256,
            source_sha256: normalizedSha256,
            issue_counts: { error: 2, warning: 1, info: 0 },
            issues: [
              durableIssue("error", "QA_ERROR_1"),
              durableIssue("error", "QA_ERROR_2"),
              durableIssue("warning", "QA_WARNING_1"),
            ],
            semantic_qa: true,
            metadata: {},
          }),
        );
        await writeFile(
          path.join(jobRoot, "agent-tool-state.json"),
          JSON.stringify({
            schema_version: 1,
            job_id: job.id,
            source: {
              input_relpath: job.inputRelpath,
              input_sha256: sourceSha256,
              size_bytes: sourceBytes.length,
              source_format: "docx",
              normalized_format: "docx",
              normalized_relpath: normalizedRelpath,
              normalized_sha256: normalizedSha256,
            },
            candidates: {
              "candidate-r0": {
                candidate_id: "candidate-r0",
                relpath: candidateRelpath,
                sha256: candidateSha256,
                size_bytes: candidateBytes.length,
                document_format: "docx",
                source_relpath: normalizedRelpath,
                source_sha256: normalizedSha256,
                pipeline_status: "COMPLETED",
                created_by: "translate_once",
                parent_candidate_id: null,
                source_language: "en",
                target_language: "zh",
                latest_qa: {
                  status: "NEEDS_REVIEW",
                  candidate_sha256: candidateSha256,
                  source_sha256: normalizedSha256,
                  report_relpath: reportRelpath,
                  issue_counts: { error: 2, warning: 1, info: 0 },
                  checked_at: now.toISOString(),
                },
                verification: null,
                created_at: now.toISOString(),
              },
            },
            results: {},
            created_at: now.toISOString(),
            updated_at: now.toISOString(),
          }),
        );
        throw new Error("Python tool run_document_qa exited with controlled code 1");
      }
      return completedEnvelope(tool, jobId);
    },
  };
  const sessionFactory = scriptedSessionFactory(async (tools) => {
    await invokeTool(tools, "preflight_document", {});
    await invokeTool(tools, "translate_once", {});
    await expect(
      invokeTool(tools, "run_document_qa", { candidate_id: "candidate-r0" }),
    ).rejects.toThrow("controlled code 1");
  });

  const runner = new AgentJobRunner(config, registry, bridge, sessionFactory, new NoopProviderLimiter());
  await runner.run(job.id);

  expect(await registry.getJob(job.id)).toMatchObject({
    status: "NEEDS_REVIEW",
    errorCode: "repair_no_candidate",
    candidateId: "candidate-r0",
    qaIssueCounts: { error: 2, warning: 1, info: 0 },
    reportRelpath,
    resolvedSourceLanguage: "en",
    resolvedTargetLanguage: "zh",
  });
});

it("fails when translation throws without registering a candidate even if the session catches it", async () => {
  const config = await testConfig({ fixtureMode: false });
  cleanup.push(config.dataRoot);
  const registry = new InMemoryJobRegistry();
  const now = new Date();
  const job: TranslationJobRecord = {
    id: "no-candidate-job",
    sourceId: "source",
    executionMode: "pi_agent_live",
    sourceFilename: "source.pptx",
    inputRelpath: "input/source.pptx",
    sourceLanguage: "auto",
    targetLanguage: "auto",
    qualityProfile: "standard",
    maxRepairRounds: 2,
    repairRound: 0,
    status: "QUEUED",
    softDeadlineAt: new Date(now.getTime() + 60_000).toISOString(),
    hardDeadlineAt: new Date(now.getTime() + 120_000).toISOString(),
    createdAt: now.toISOString(),
    updatedAt: now.toISOString(),
  };
  await registry.createJob(job, "no-candidate-idempotency");
  const bridge: PythonToolBridge = {
    execute: async (tool, jobId): Promise<PythonToolEnvelope> => {
      if (tool === "translate_once") {
        throw new Error("Python tool translate_once exited with controlled code 1");
      }
      return completedEnvelope(tool, jobId);
    },
  };
  const sessionFactory = scriptedSessionFactory(async (tools) => {
    await invokeTool(tools, "preflight_document", {});
    await expect(invokeTool(tools, "translate_once", {})).rejects.toThrow("controlled code 1");
  });

  const runner = new AgentJobRunner(config, registry, bridge, sessionFactory, new NoopProviderLimiter());
  await runner.run(job.id);

  expect(await registry.getJob(job.id)).toMatchObject({
    status: "FAILED",
    errorCode: "agent_execution_failed",
    errorSummary: expect.stringContaining("translate_once"),
  });
});

it("fails a swallowed preflight error and records the controlled tool end", async () => {
  const config = await testConfig({ fixtureMode: false });
  cleanup.push(config.dataRoot);
  const registry = new InMemoryJobRegistry();
  const now = new Date();
  const job: TranslationJobRecord = {
    id: "preflight-invalid-pptx-job",
    sourceId: "source",
    executionMode: "pi_agent_live",
    sourceFilename: "source.pptx",
    inputRelpath: "input/source.pptx",
    sourceLanguage: "en",
    targetLanguage: "auto",
    qualityProfile: "standard",
    maxRepairRounds: 2,
    repairRound: 0,
    status: "QUEUED",
    softDeadlineAt: new Date(now.getTime() + 60_000).toISOString(),
    hardDeadlineAt: new Date(now.getTime() + 120_000).toISOString(),
    createdAt: now.toISOString(),
    updatedAt: now.toISOString(),
  };
  await registry.createJob(job, "preflight-invalid-pptx-idempotency");
  const bridge: PythonToolBridge = {
    execute: async (tool, jobId): Promise<PythonToolEnvelope> => ({
      tool,
      job_id: jobId,
      status: "FAILED",
      summary: {},
      artifacts: [],
      issues: [],
      error: {
        code: "TOOL_EXECUTION_FAILED",
        message: "agent tool execution failed",
      },
    }),
  };
  const sessionFactory = scriptedSessionFactory(async (tools) => {
    await expect(invokeTool(tools, "preflight_document", {})).rejects.toThrow(
      "preflight_document failed: TOOL_EXECUTION_FAILED",
    );
  });
  const runner = new AgentJobRunner(
    config,
    registry,
    bridge,
    sessionFactory,
    new NoopProviderLimiter(),
  );

  await runner.run(job.id);

  expect(await registry.getJob(job.id)).toMatchObject({
    status: "FAILED",
    errorCode: "agent_execution_failed",
    errorSummary: expect.stringContaining("preflight_document"),
  });
  const eventLines = (await readFile(
    path.join(config.jobsRoot, job.id, "agent-events.jsonl"),
    "utf8",
  )).trim().split(/\r?\n/).map((line) => JSON.parse(line) as Record<string, unknown>);
  expect(eventLines).toEqual(expect.arrayContaining([
    expect.objectContaining({
      type: "tool_end",
      tool: "preflight_document",
      outcome: "FAILED",
      code: "TOOL_EXECUTION_FAILED",
    }),
  ]));
});

it("continues from a durable NEEDS_REVIEW translation candidate without repeating translation", async () => {
  const config = await testConfig({ fixtureMode: false });
  cleanup.push(config.dataRoot);
  const registry = new InMemoryJobRegistry();
  const now = new Date();
  const job: TranslationJobRecord = {
    id: "durable-candidate-job",
    sourceId: "source",
    executionMode: "pi_agent_live",
    sourceFilename: "source.docx",
    inputRelpath: "input/source.docx",
    sourceLanguage: "auto",
    targetLanguage: "auto",
    qualityProfile: "standard",
    maxRepairRounds: 2,
    repairRound: 0,
    status: "QUEUED",
    softDeadlineAt: new Date(now.getTime() + 60_000).toISOString(),
    hardDeadlineAt: new Date(now.getTime() + 120_000).toISOString(),
    createdAt: now.toISOString(),
    updatedAt: now.toISOString(),
  };
  await registry.createJob(job, "durable-candidate-idempotency");
  const jobRoot = path.join(config.jobsRoot, job.id);
  const candidateRelpath = "candidates/candidate-r0.docx";
  const candidateBytes = Buffer.from("registered before bridge exit", "utf8");
  const candidateSha256 = createHash("sha256").update(candidateBytes).digest("hex");
  const sourceBytes = Buffer.from("durable candidate input", "utf8");
  const sourceSha256 = createHash("sha256").update(sourceBytes).digest("hex");
  const normalizedBytes = Buffer.from("durable candidate normalized", "utf8");
  const normalizedSha256 = createHash("sha256").update(normalizedBytes).digest("hex");
  const normalizedRelpath = "normalized/source.docx";
  const calls: PythonToolName[] = [];
  const bridge: PythonToolBridge = {
    execute: async (tool, jobId): Promise<PythonToolEnvelope> => {
      calls.push(tool);
      if (tool === "translate_once") {
        await mkdir(path.join(jobRoot, "input"), { recursive: true });
        await mkdir(path.join(jobRoot, "normalized"), { recursive: true });
        await mkdir(path.join(jobRoot, "candidates"), { recursive: true });
        await writeFile(path.join(jobRoot, "input", "source.docx"), sourceBytes);
        await writeFile(path.join(jobRoot, "normalized", "source.docx"), normalizedBytes);
        await writeFile(path.join(jobRoot, ...candidateRelpath.split("/")), candidateBytes);
        await writeFile(
          path.join(jobRoot, "agent-tool-state.json"),
          JSON.stringify({
            schema_version: 1,
            job_id: job.id,
            source: {
              input_relpath: job.inputRelpath,
              input_sha256: sourceSha256,
              size_bytes: sourceBytes.length,
              source_format: "docx",
              normalized_format: "docx",
              normalized_relpath: normalizedRelpath,
              normalized_sha256: normalizedSha256,
            },
            candidates: {
              "candidate-r0": {
                candidate_id: "candidate-r0",
                relpath: candidateRelpath,
                sha256: candidateSha256,
                size_bytes: candidateBytes.length,
                document_format: "docx",
                source_relpath: normalizedRelpath,
                source_sha256: normalizedSha256,
                pipeline_status: "NEEDS_REVIEW",
                created_by: "translate_once",
                parent_candidate_id: null,
                source_language: "zh",
                target_language: "en",
                latest_qa: null,
                verification: null,
                created_at: now.toISOString(),
              },
            },
            results: {},
            created_at: now.toISOString(),
            updated_at: now.toISOString(),
          }),
        );
        throw new Error("Python tool translate_once exited after registering candidate-r0");
      }
      if (tool === "run_document_qa") {
        return {
          ...completedEnvelope(tool, jobId, [artifact("qa_report", "reports/qa/candidate-r0.json")]),
          status: "NEEDS_REVIEW",
          summary: { issue_counts: { error: 98, warning: 47, info: 0 } },
          issues: [{ code: "QA_BLOCKING", severity: "error", stage: "semantic_qa" }],
        };
      }
      if (tool === "apply_repair") {
        return {
          ...completedEnvelope(tool, jobId),
          status: "NEEDS_REVIEW",
          summary: { output_created: false, output_candidate_id: "candidate-r1" },
          issues: [{ code: "REPAIR_NOT_IMPROVED", severity: "error" }],
          error: {
            code: "REPAIR_NOT_IMPROVED",
            message: "bounded repair did not improve full QA",
          },
        };
      }
      if (tool === "preflight_document") return completedEnvelope(tool, jobId);
      throw new Error(`unexpected recovery tool: ${tool}`);
    },
  };
  const sessionFactory = scriptedSessionFactory(async (tools) => {
    await invokeTool(tools, "preflight_document", {});
    await expect(invokeTool(tools, "translate_once", {})).rejects.toThrow(
      "after registering candidate-r0",
    );
  });

  const runner = new AgentJobRunner(config, registry, bridge, sessionFactory, new NoopProviderLimiter());
  await runner.run(job.id);

  expect(calls).toEqual([
    "preflight_document",
    "translate_once",
    "run_document_qa",
    "apply_repair",
  ]);
  expect(await registry.getJob(job.id)).toMatchObject({
    status: "NEEDS_REVIEW",
    errorCode: "repair_not_improved",
    candidateId: "candidate-r0",
    qaIssueCounts: { error: 98, warning: 47, info: 0 },
    resolvedSourceLanguage: "zh",
    resolvedTargetLanguage: "en",
  });
});

it("continues from a durable NEEDS_REVIEW repair candidate at QA", async () => {
  const config = await testConfig({ fixtureMode: false });
  cleanup.push(config.dataRoot);
  const registry = new InMemoryJobRegistry();
  const now = new Date();
  const job: TranslationJobRecord = {
    id: "durable-repair-candidate-job",
    sourceId: "source",
    executionMode: "pi_agent_live",
    sourceFilename: "source.docx",
    inputRelpath: "input/source.docx",
    sourceLanguage: "en",
    targetLanguage: "zh",
    qualityProfile: "standard",
    maxRepairRounds: 2,
    repairRound: 1,
    candidateId: "candidate-r1",
    status: "REPAIRING",
    softDeadlineAt: new Date(now.getTime() + 60_000).toISOString(),
    hardDeadlineAt: new Date(now.getTime() + 120_000).toISOString(),
    createdAt: now.toISOString(),
    updatedAt: now.toISOString(),
  };
  await registry.createJob(job, "durable-repair-candidate-idempotency");

  const jobRoot = path.join(config.jobsRoot, job.id);
  const sourceBytes = Buffer.from("durable repair input", "utf8");
  const sourceSha256 = createHash("sha256").update(sourceBytes).digest("hex");
  const normalizedBytes = Buffer.from("durable repair normalized", "utf8");
  const normalizedSha256 = createHash("sha256").update(normalizedBytes).digest("hex");
  const candidateBytes = Buffer.from("durable repaired candidate", "utf8");
  const candidateSha256 = createHash("sha256").update(candidateBytes).digest("hex");
  const normalizedRelpath = "normalized/source.docx";
  const candidateRelpath = "candidates/candidate-r1.docx";
  await mkdir(path.join(jobRoot, "input"), { recursive: true });
  await mkdir(path.join(jobRoot, "normalized"), { recursive: true });
  await mkdir(path.join(jobRoot, "candidates"), { recursive: true });
  await writeFile(path.join(jobRoot, "input", "source.docx"), sourceBytes);
  await writeFile(path.join(jobRoot, "normalized", "source.docx"), normalizedBytes);
  await writeFile(path.join(jobRoot, "candidates", "candidate-r1.docx"), candidateBytes);
  await writeFile(
    path.join(jobRoot, "agent-tool-state.json"),
    JSON.stringify({
      schema_version: 1,
      job_id: job.id,
      source: {
        input_relpath: job.inputRelpath,
        input_sha256: sourceSha256,
        size_bytes: sourceBytes.length,
        source_format: "docx",
        normalized_format: "docx",
        normalized_relpath: normalizedRelpath,
        normalized_sha256: normalizedSha256,
      },
      candidates: {
        "candidate-r1": {
          candidate_id: "candidate-r1",
          relpath: candidateRelpath,
          sha256: candidateSha256,
          size_bytes: candidateBytes.length,
          document_format: "docx",
          source_relpath: normalizedRelpath,
          source_sha256: normalizedSha256,
          pipeline_status: "NEEDS_REVIEW",
          created_by: "apply_repair",
          parent_candidate_id: "candidate-r0",
          source_language: "en",
          target_language: "zh",
          latest_qa: null,
          verification: null,
          created_at: now.toISOString(),
        },
      },
      results: {},
      created_at: now.toISOString(),
      updated_at: now.toISOString(),
    }),
  );

  const calls: PythonToolName[] = [];
  const bridge: PythonToolBridge = {
    execute: async (tool, jobId): Promise<PythonToolEnvelope> => {
      calls.push(tool);
      if (tool === "run_document_qa") {
        return {
          ...completedEnvelope(tool, jobId, [artifact("qa_report", "reports/qa/candidate-r1.json")]),
          status: "NEEDS_REVIEW",
          summary: { issue_counts: { error: 3, warning: 2, info: 0 } },
          issues: [{ code: "QA_BLOCKING", severity: "error", stage: "semantic_qa" }],
        };
      }
      if (tool === "apply_repair") {
        return {
          ...completedEnvelope(tool, jobId),
          status: "NEEDS_REVIEW",
          summary: { output_created: false, output_candidate_id: "candidate-r2" },
          issues: [{ code: "REPAIR_NOT_IMPROVED", severity: "error" }],
          error: {
            code: "REPAIR_NOT_IMPROVED",
            message: "bounded repair did not improve full QA",
          },
        };
      }
      throw new Error(`unexpected recovery tool: ${tool}`);
    },
  };
  const sessionFactory = scriptedSessionFactory(async () => {
    throw new Error("Pi request failed after durable blocking QA");
  });
  const runner = new AgentJobRunner(config, registry, bridge, sessionFactory, new NoopProviderLimiter());

  await runner.run(job.id);

  expect(calls).toEqual(["run_document_qa", "apply_repair"]);
  expect(await registry.getJob(job.id)).toMatchObject({
    status: "NEEDS_REVIEW",
    errorCode: "repair_not_improved",
    candidateId: "candidate-r1",
    repairRound: 1,
    qaIssueCounts: { error: 3, warning: 2, info: 0 },
  });
});

it("deterministically finishes a clear QA snapshot after the orchestration model stops with error", async () => {
  const config = await testConfig({ fixtureMode: false });
  cleanup.push(config.dataRoot);
  const registry = new InMemoryJobRegistry();
  const now = new Date();
  const job: TranslationJobRecord = {
    id: "post-qa-delivery-job",
    sourceId: "source",
    executionMode: "pi_agent_live",
    sourceFilename: "source.docx",
    inputRelpath: "input/source.docx",
    sourceLanguage: "en",
    targetLanguage: "zh",
    qualityProfile: "standard",
    maxRepairRounds: 2,
    repairRound: 0,
    status: "QUEUED",
    softDeadlineAt: new Date(now.getTime() + 60_000).toISOString(),
    hardDeadlineAt: new Date(now.getTime() + 120_000).toISOString(),
    createdAt: now.toISOString(),
    updatedAt: now.toISOString(),
  };
  await registry.createJob(job, "post-qa-delivery-idempotency");
  const calls: PythonToolName[] = [];
  const bridge: PythonToolBridge = {
    execute: async (tool, jobId): Promise<PythonToolEnvelope> => {
      calls.push(tool);
      if (tool === "translate_once") {
        return completedEnvelope(tool, jobId, [
          artifact("candidate_document", "candidates/candidate-r0.docx"),
        ]);
      }
      if (tool === "run_document_qa") {
        return {
          ...completedEnvelope(tool, jobId, [artifact("qa_report", "reports/qa/candidate-r0.json")]),
          summary: { issue_counts: { error: 0, warning: 0, info: 0 } },
        };
      }
      if (tool === "verify_candidate") {
        return completedEnvelope(tool, jobId, [
          artifact("verification_report", "reports/verification/candidate-r0.json"),
        ]);
      }
      if (tool === "finalize_result") {
        return completedEnvelope(tool, jobId, [
          artifact("translated_document", "results/translated.docx"),
        ]);
      }
      return completedEnvelope(tool, jobId);
    },
  };
  const sessionFactory = scriptedSessionFactory(
    async (tools) => {
      await invokeTool(tools, "preflight_document", {});
      await invokeTool(tools, "translate_once", {});
      await invokeTool(tools, "run_document_qa", { candidate_id: "candidate-r0" });
    },
    [{ role: "assistant", stopReason: "error" }],
  );

  const runner = new AgentJobRunner(config, registry, bridge, sessionFactory, new NoopProviderLimiter());
  await runner.run(job.id);

  expect(calls).toEqual([
    "preflight_document",
    "translate_once",
    "run_document_qa",
    "verify_candidate",
    "finalize_result",
  ]);
  expect(await registry.getJob(job.id)).toMatchObject({
    status: "COMPLETED",
    candidateId: "candidate-r0",
    reportRelpath: "reports/qa/candidate-r0.json",
    resultRelpath: "results/translated.docx",
  });
});

it("rejects durably finalized output when its QA reports are empty JSON", async () => {
  const config = await testConfig({ fixtureMode: false });
  cleanup.push(config.dataRoot);
  const registry = new InMemoryJobRegistry();
  const now = new Date();
  const job: TranslationJobRecord = {
    id: "durable-finalize-job",
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
    status: "QUEUED",
    softDeadlineAt: new Date(now.getTime() + 60_000).toISOString(),
    hardDeadlineAt: new Date(now.getTime() + 120_000).toISOString(),
    createdAt: now.toISOString(),
    updatedAt: now.toISOString(),
  };
  await registry.createJob(job, "durable-finalize-idempotency");
  const jobRoot = path.join(config.jobsRoot, job.id);
  const candidateRelpath = "candidates/candidate-r0.docx";
  const normalizedRelpath = "normalized/source.docx";
  const qaRelpath = "reports/qa/candidate-r0.json";
  const verificationRelpath = "reports/verification/candidate-r0.json";
  const resultRelpath = "results/translated.docx";
  const candidateBytes = Buffer.from("fully gated candidate", "utf8");
  const candidateSha256 = createHash("sha256").update(candidateBytes).digest("hex");
  const sourceBytes = Buffer.from("registered source", "utf8");
  const sourceSha256 = createHash("sha256").update(sourceBytes).digest("hex");
  const normalizedBytes = Buffer.from("normalized source", "utf8");
  const normalizedSha256 = createHash("sha256").update(normalizedBytes).digest("hex");
  await mkdir(path.join(jobRoot, "input"), { recursive: true });
  await mkdir(path.join(jobRoot, "normalized"), { recursive: true });
  await writeFile(path.join(jobRoot, "input", "source.docx"), sourceBytes);
  await writeFile(path.join(jobRoot, "normalized", "source.docx"), normalizedBytes);
  const calls: PythonToolName[] = [];
  const bridge: PythonToolBridge = {
    execute: async (tool, jobId): Promise<PythonToolEnvelope> => {
      calls.push(tool);
      if (tool === "translate_once") {
        await mkdir(path.join(jobRoot, "candidates"), { recursive: true });
        await writeFile(path.join(jobRoot, ...candidateRelpath.split("/")), candidateBytes);
        return {
          ...completedEnvelope(tool, jobId, [artifact("candidate_document", candidateRelpath)]),
          summary: {
            requested_target_language: "auto",
            resolved_source_language: "en",
            resolved_target_language: "zh",
          },
        };
      }
      if (tool === "run_document_qa") {
        await mkdir(path.join(jobRoot, "reports", "qa"), { recursive: true });
        await writeFile(path.join(jobRoot, ...qaRelpath.split("/")), "{}");
        return {
          ...completedEnvelope(tool, jobId, [artifact("qa_report", qaRelpath)]),
          summary: { issue_counts: { error: 0, warning: 0, info: 0 } },
        };
      }
      if (tool === "verify_candidate") {
        await mkdir(path.join(jobRoot, "reports", "verification"), { recursive: true });
        await writeFile(path.join(jobRoot, ...verificationRelpath.split("/")), "{}");
        return completedEnvelope(tool, jobId, [artifact("verification_report", verificationRelpath)]);
      }
      if (tool === "finalize_result") {
        await mkdir(path.join(jobRoot, "results"), { recursive: true });
        await writeFile(path.join(jobRoot, ...resultRelpath.split("/")), candidateBytes);
        await writeFile(
          path.join(jobRoot, "agent-tool-state.json"),
          JSON.stringify({
            schema_version: 1,
            job_id: job.id,
            source: {
              input_relpath: job.inputRelpath,
              input_sha256: sourceSha256,
              size_bytes: sourceBytes.length,
              source_format: "docx",
              normalized_format: "docx",
              normalized_relpath: normalizedRelpath,
              normalized_sha256: normalizedSha256,
            },
            candidates: {
              "candidate-r0": {
                candidate_id: "candidate-r0",
                relpath: candidateRelpath,
                sha256: candidateSha256,
                size_bytes: candidateBytes.length,
                document_format: "docx",
                source_relpath: normalizedRelpath,
                source_sha256: normalizedSha256,
                pipeline_status: "COMPLETED",
                created_by: "translate_once",
                parent_candidate_id: null,
                source_language: "en",
                target_language: "zh",
                latest_qa: {
                  status: "COMPLETED",
                  candidate_sha256: candidateSha256,
                  source_sha256: normalizedSha256,
                  report_relpath: qaRelpath,
                  issue_counts: { error: 0, warning: 0, info: 0 },
                  checked_at: now.toISOString(),
                },
                verification: {
                  status: "COMPLETED",
                  candidate_sha256: candidateSha256,
                  source_sha256: normalizedSha256,
                  report_relpath: verificationRelpath,
                  issue_counts: { error: 0, warning: 0, info: 0 },
                  checked_at: now.toISOString(),
                },
                created_at: now.toISOString(),
              },
            },
            results: {
              "translated.docx": {
                candidate_id: "candidate-r0",
                relpath: resultRelpath,
                sha256: candidateSha256,
                size_bytes: candidateBytes.length,
                finalized_at: now.toISOString(),
              },
            },
            created_at: now.toISOString(),
            updated_at: now.toISOString(),
          }),
        );
        throw new Error("Python tool finalize_result exited after durable finalization");
      }
      return completedEnvelope(tool, jobId);
    },
  };
  const sessionFactory = scriptedSessionFactory(async (tools) => {
    await invokeTool(tools, "preflight_document", {});
    await invokeTool(tools, "translate_once", {});
    await invokeTool(tools, "run_document_qa", { candidate_id: "candidate-r0" });
    await invokeTool(tools, "verify_candidate", { candidate_id: "candidate-r0" });
    await expect(
      invokeTool(tools, "finalize_result", { candidate_id: "candidate-r0" }),
    ).rejects.toThrow("after durable finalization");
  });

  const runner = new AgentJobRunner(config, registry, bridge, sessionFactory, new NoopProviderLimiter());
  await runner.run(job.id);

  expect(calls).toEqual([
    "preflight_document",
    "translate_once",
    "run_document_qa",
    "verify_candidate",
    "finalize_result",
  ]);
  expect(await registry.getJob(job.id)).toMatchObject({
    status: "NEEDS_REVIEW",
    errorCode: "agent_recovery_failed",
    errorSummary: expect.stringContaining("report"),
  });
});

it("fails closed without gate calls when durable candidate evidence has a digest mismatch", async () => {
  const config = await testConfig({ fixtureMode: false });
  cleanup.push(config.dataRoot);
  const registry = new InMemoryJobRegistry();
  const now = new Date();
  const job: TranslationJobRecord = {
    id: "unsafe-durable-state-job",
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
  await registry.createJob(job, "unsafe-durable-state-idempotency");
  const jobRoot = path.join(config.jobsRoot, job.id);
  const sourceBytes = Buffer.from("unsafe-state input", "utf8");
  const sourceSha256 = createHash("sha256").update(sourceBytes).digest("hex");
  const normalizedBytes = Buffer.from("unsafe-state normalized", "utf8");
  const normalizedSha256 = createHash("sha256").update(normalizedBytes).digest("hex");
  const candidateBytes = Buffer.from("actual bytes", "utf8");
  await mkdir(path.join(jobRoot, "input"), { recursive: true });
  await mkdir(path.join(jobRoot, "normalized"), { recursive: true });
  await mkdir(path.join(jobRoot, "candidates"), { recursive: true });
  await writeFile(path.join(jobRoot, "input", "source.docx"), sourceBytes);
  await writeFile(path.join(jobRoot, "normalized", "source.docx"), normalizedBytes);
  await writeFile(path.join(jobRoot, "candidates", "candidate-r0.docx"), candidateBytes);
  await writeFile(
    path.join(jobRoot, "agent-tool-state.json"),
    JSON.stringify({
      schema_version: 1,
      job_id: job.id,
      source: {
        input_relpath: job.inputRelpath,
        input_sha256: sourceSha256,
        size_bytes: sourceBytes.length,
        source_format: "docx",
        normalized_format: "docx",
        normalized_relpath: "normalized/source.docx",
        normalized_sha256: normalizedSha256,
      },
      candidates: {
        "candidate-r0": {
          candidate_id: "candidate-r0",
          relpath: "candidates/candidate-r0.docx",
          sha256: "a".repeat(64),
          size_bytes: candidateBytes.length,
          document_format: "docx",
          source_relpath: "normalized/source.docx",
          source_sha256: normalizedSha256,
          pipeline_status: "COMPLETED",
          created_by: "translate_once",
          parent_candidate_id: null,
          source_language: "en",
          target_language: "zh",
          latest_qa: null,
          verification: null,
          created_at: now.toISOString(),
        },
      },
      results: {},
      created_at: now.toISOString(),
      updated_at: now.toISOString(),
    }),
  );
  const calls: PythonToolName[] = [];
  const bridge: PythonToolBridge = {
    execute: async (tool) => {
      calls.push(tool);
      throw new Error("unsafe state must not reach a controlled gate");
    },
  };
  const runner = new AgentJobRunner(
    config,
    registry,
    bridge,
    scriptedSessionFactory(async () => undefined),
    new NoopProviderLimiter(),
  );

  await runner.run(job.id);

  expect(calls).toEqual([]);
  expect(await registry.getJob(job.id)).toMatchObject({
    status: "NEEDS_REVIEW",
    errorCode: "agent_recovery_failed",
    errorSummary: expect.stringContaining("digest mismatch"),
  });
});

it("attempts interrupted QA recovery only once when no durable QA result exists", async () => {
  const config = await testConfig({ fixtureMode: false });
  cleanup.push(config.dataRoot);
  const registry = new InMemoryJobRegistry();
  const now = new Date();
  const job: TranslationJobRecord = {
    id: "bounded-qa-recovery-job",
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
  await registry.createJob(job, "bounded-qa-recovery-idempotency");
  const calls: PythonToolName[] = [];
  const bridge: PythonToolBridge = {
    execute: async (tool) => {
      calls.push(tool);
      throw new Error("QA provider remained unavailable");
    },
  };
  const runner = new AgentJobRunner(
    config,
    registry,
    bridge,
    scriptedSessionFactory(async () => undefined),
    new NoopProviderLimiter(),
  );

  await runner.run(job.id);

  expect(calls).toEqual(["run_document_qa"]);
  expect(await registry.getJob(job.id)).toMatchObject({
    status: "NEEDS_REVIEW",
    errorCode: "qa_recovery_failed",
    errorSummary: expect.stringContaining("safe to retry"),
  });
});

it("observes a durable cancellation requested through another runner instance", async () => {
  const config = await testConfig({ fixtureMode: false });
  cleanup.push(config.dataRoot);
  await mkdir(path.join(config.jobsRoot, "remote-cancel-job"), { recursive: true });
  const registry = new InMemoryJobRegistry();
  const now = new Date();
  const job: TranslationJobRecord = {
    id: "remote-cancel-job",
    sourceId: "source",
    executionMode: "pi_agent_live",
    sourceFilename: "source.docx",
    inputRelpath: "input/source.docx",
    sourceLanguage: "en",
    targetLanguage: "zh",
    qualityProfile: "standard",
    maxRepairRounds: 2,
    repairRound: 0,
    status: "QUEUED",
    softDeadlineAt: new Date(now.getTime() + 60_000).toISOString(),
    hardDeadlineAt: new Date(now.getTime() + 120_000).toISOString(),
    createdAt: now.toISOString(),
    updatedAt: now.toISOString(),
  };
  await registry.createJob(job, "remote-cancel-idempotency");
  const bridge: PythonToolBridge = {
    execute: async () => { throw new Error("not used"); },
  };
  let createdResolve!: () => void;
  const created = new Promise<void>((resolve) => { createdResolve = resolve; });
  let abortPrompt: ((error: Error) => void) | undefined;
  let aborted = false;
  const sessionFactory: SessionFactory = {
    probe: async () => undefined,
    create: async () => {
      const session = {
        subscribe: () => () => undefined,
        prompt: async () => {
          createdResolve();
          if (aborted) throw new Error("aborted");
          await new Promise<never>((_resolve, reject) => { abortPrompt = reject; });
        },
        waitForIdle: async () => undefined,
        abort: async () => {
          aborted = true;
          abortPrompt?.(new Error("aborted"));
        },
        dispose: () => undefined,
      } as unknown as AgentSession;
      return { session, dispose: () => undefined };
    },
  };
  const runningReplica = new AgentJobRunner(
    config,
    registry,
    bridge,
    sessionFactory,
    new NoopProviderLimiter(),
  );
  const apiReplica = new AgentJobRunner(
    config,
    registry,
    bridge,
    null,
    new NoopProviderLimiter(),
  );
  const run = runningReplica.run(job.id);
  await created;
  expect(await apiReplica.cancel(job.id)).toBe(true);
  await run;
  const cancelled = await registry.getJob(job.id);
  expect(cancelled?.cancellationRequestedAt).toBeDefined();
  expect(cancelled?.status).toBe("CANCELLED");
});

function artifact(kind: string, relpath: string): PythonToolEnvelope["artifacts"][number] {
  return {
    artifact_id: kind,
    kind,
    relpath,
    filename: relpath.split("/").at(-1)!,
    size_bytes: 1,
    sha256: "0".repeat(64),
  };
}

function durableIssue(severity: "error" | "warning" | "info", code: string) {
  return {
    issue_id: null,
    code,
    message: `${code} message`,
    severity,
    stage: "document_qa",
    block_id: null,
    page: null,
    slide: null,
    recoverable: true,
    details: {},
  };
}

function completedEnvelope(
  tool: PythonToolName,
  jobId: string,
  artifacts: PythonToolEnvelope["artifacts"] = [],
): PythonToolEnvelope {
  return {
    tool,
    job_id: jobId,
    status: "COMPLETED",
    summary: {},
    artifacts,
    issues: [],
    error: null,
  };
}

function scriptedSessionFactory(
  script: (tools: ToolDefinition[]) => Promise<void>,
  messages: unknown[] = [],
): SessionFactory {
  return {
    probe: async () => undefined,
    create: async (_cwd, tools) => {
      const session = {
        messages,
        subscribe: () => () => undefined,
        prompt: async () => script(tools),
        waitForIdle: async () => undefined,
        abort: async () => undefined,
        dispose: () => undefined,
      } as unknown as AgentSession;
      return { session, dispose: () => undefined };
    },
  };
}

async function invokeTool(
  tools: ToolDefinition[],
  name: string,
  params: Record<string, unknown>,
): Promise<void> {
  const tool = tools.find((candidate) => candidate.name === name);
  if (!tool) throw new Error(`missing test tool: ${name}`);
  await tool.execute(`test-${name}`, params, undefined, undefined, {} as never);
}
