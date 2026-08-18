import { rm } from "node:fs/promises";
import { afterEach, expect, it } from "vitest";
import type {
  PythonToolEnvelope,
  PythonToolName,
  TranslationJobRecord,
} from "../src/contracts.js";
import { AgentJobRunner } from "../src/job-runner.js";
import { NoopProviderLimiter } from "../src/provider-limiter.js";
import type { PythonToolBridge } from "../src/python-tool-bridge.js";
import { InMemoryJobRegistry } from "../src/registry.js";
import { testConfig } from "./helpers.js";

const cleanup: string[] = [];

afterEach(async () => {
  await Promise.all(cleanup.splice(0).map((value) => rm(value, { recursive: true, force: true })));
});

it("completes a fixture candidate whose initial QA has no blockers", async () => {
  const config = await testConfig();
  cleanup.push(config.dataRoot);
  const registry = new InMemoryJobRegistry();
  const job = queuedJob("fixture-passes-initial-qa", 2);
  await registry.createJob(job, `${job.id}-idempotency`);
  const calls: PythonToolName[] = [];

  const bridge: PythonToolBridge = {
    execute: async (tool, jobId) => {
      calls.push(tool);
      if (tool === "translate_once") {
        return completed(tool, jobId, {
          summary: {
            requested_target_language: "zh",
            resolved_source_language: "en",
            resolved_target_language: "zh",
          },
          artifacts: [artifact("candidate:candidate-r0", "candidate_document", "candidates/candidate-r0.docx")],
        });
      }
      if (tool === "run_document_qa") {
        return completed(tool, jobId, {
          summary: { issue_counts: { error: 0, warning: 0, info: 0 } },
          artifacts: [artifact("qa:candidate-r0", "qa_report", "reports/qa/candidate-r0.json")],
        });
      }
      if (tool === "verify_candidate") {
        return completed(tool, jobId, {
          summary: { issue_counts: { error: 0, warning: 0, info: 0 } },
          artifacts: [artifact("verification:candidate-r0", "verification_report", "reports/verification/candidate-r0.json")],
        });
      }
      if (tool === "finalize_result") {
        return completed(tool, jobId, {
          artifacts: [artifact("result", "translated_document", "result/translated.docx")],
        });
      }
      return completed(tool, jobId);
    },
  };
  const runner = new AgentJobRunner(
    config,
    registry,
    bridge,
    null,
    new NoopProviderLimiter(),
  );

  await runner.run(job.id);

  expect(await registry.getJob(job.id)).toMatchObject({
    status: "COMPLETED",
    candidateId: "candidate-r0",
    repairRound: 0,
    qaIssueCounts: { error: 0, warning: 0, info: 0 },
    resultRelpath: "result/translated.docx",
  });
  expect(calls).not.toContain("apply_repair");
});

it("promotes a strictly improving repaired fixture candidate and completes it", async () => {
  const config = await testConfig();
  cleanup.push(config.dataRoot);
  const registry = new InMemoryJobRegistry();
  const job = queuedJob("fixture-improves-after-repair", 2);
  await registry.createJob(job, `${job.id}-idempotency`);

  const bridge: PythonToolBridge = {
    execute: async (tool, jobId, payload) => {
      if (tool === "translate_once") {
        return completed(tool, jobId, {
          summary: {
            requested_target_language: "zh",
            resolved_source_language: "en",
            resolved_target_language: "zh",
          },
          artifacts: [artifact("candidate:candidate-r0", "candidate_document", "candidates/candidate-r0.docx")],
        });
      }
      if (tool === "run_document_qa") {
        const candidateId = String(payload.candidate_id);
        if (candidateId === "candidate-r0") {
          return completed(tool, jobId, {
            summary: { issue_counts: { error: 1, warning: 0, info: 0 } },
            issues: [blockingIssue("overflow", 1)],
            artifacts: [artifact("qa:candidate-r0", "qa_report", "reports/qa/candidate-r0.json")],
          });
        }
        return completed(tool, jobId, {
          summary: { issue_counts: { error: 0, warning: 0, info: 0 } },
          artifacts: [artifact("qa:candidate-r1", "qa_report", "reports/qa/candidate-r1.json")],
        });
      }
      if (tool === "apply_repair") {
        return completed(tool, jobId, {
          summary: { output_created: true, output_candidate_id: "candidate-r1" },
          artifacts: [artifact("candidate:candidate-r1", "candidate_document", "candidates/candidate-r1.docx")],
        });
      }
      if (tool === "verify_candidate") {
        return completed(tool, jobId, {
          summary: { issue_counts: { error: 0, warning: 0, info: 0 } },
          artifacts: [artifact("verification:candidate-r1", "verification_report", "reports/verification/candidate-r1.json")],
        });
      }
      if (tool === "finalize_result") {
        return completed(tool, jobId, {
          artifacts: [artifact("result", "translated_document", "result/translated.docx")],
        });
      }
      return completed(tool, jobId);
    },
  };
  const runner = new AgentJobRunner(
    config,
    registry,
    bridge,
    null,
    new NoopProviderLimiter(),
  );

  await runner.run(job.id);

  expect(await registry.getJob(job.id)).toMatchObject({
    status: "COMPLETED",
    candidateId: "candidate-r1",
    repairRound: 1,
    qaIssueCounts: { error: 0, warning: 0, info: 0 },
    reportRelpath: "reports/qa/candidate-r1.json",
    resultRelpath: "result/translated.docx",
  });
});

it("stops before a new fixture repair when the soft deadline has expired", async () => {
  const config = await testConfig();
  cleanup.push(config.dataRoot);
  const registry = new InMemoryJobRegistry();
  const job = queuedJob("fixture-soft-deadline", 2);
  job.softDeadlineAt = new Date(Date.now() - 1_000).toISOString();
  await registry.createJob(job, `${job.id}-idempotency`);
  const calls: PythonToolName[] = [];

  const bridge: PythonToolBridge = {
    execute: async (tool, jobId) => {
      calls.push(tool);
      if (tool === "translate_once") {
        return completed(tool, jobId, {
          summary: {
            requested_target_language: "zh",
            resolved_source_language: "en",
            resolved_target_language: "zh",
          },
          artifacts: [artifact("candidate:candidate-r0", "candidate_document", "candidates/candidate-r0.docx")],
        });
      }
      if (tool === "run_document_qa") {
        return completed(tool, jobId, {
          summary: { issue_counts: { error: 1, warning: 0, info: 0 } },
          issues: [blockingIssue("overflow", 1)],
          artifacts: [artifact("qa:candidate-r0", "qa_report", "reports/qa/candidate-r0.json")],
        });
      }
      return completed(tool, jobId);
    },
  };
  const runner = new AgentJobRunner(
    config,
    registry,
    bridge,
    null,
    new NoopProviderLimiter(),
  );

  await runner.run(job.id);

  expect(await registry.getJob(job.id)).toMatchObject({
    status: "NEEDS_REVIEW",
    candidateId: "candidate-r0",
    repairRound: 0,
    errorCode: "qa_unresolved",
    errorSummary: expect.stringContaining("soft deadline"),
  });
  expect((await registry.getJob(job.id))?.resultRelpath).toBeUndefined();
  expect(calls).not.toContain("apply_repair");
  expect(calls).not.toContain("verify_candidate");
  expect(calls).not.toContain("finalize_result");
});

it("requires review without finalizing when fixture blockers remain at the repair limit", async () => {
  const config = await testConfig();
  cleanup.push(config.dataRoot);
  const registry = new InMemoryJobRegistry();
  const job = queuedJob("fixture-exhausts-repair-limit", 1);
  await registry.createJob(job, `${job.id}-idempotency`);
  const calls: PythonToolName[] = [];

  const bridge: PythonToolBridge = {
    execute: async (tool, jobId, payload) => {
      calls.push(tool);
      if (tool === "translate_once") {
        return completed(tool, jobId, {
          summary: {
            requested_target_language: "zh",
            resolved_source_language: "en",
            resolved_target_language: "zh",
          },
          artifacts: [artifact("candidate:candidate-r0", "candidate_document", "candidates/candidate-r0.docx")],
        });
      }
      if (tool === "run_document_qa") {
        const candidateId = String(payload.candidate_id);
        const isInitialCandidate = candidateId === "candidate-r0";
        return completed(tool, jobId, {
          summary: {
            issue_counts: {
              error: 1,
              warning: isInitialCandidate ? 1 : 0,
              info: 0,
            },
          },
          issues: [
            blockingIssue("overflow", 1),
            ...(isInitialCandidate ? [{ severity: "warning", code: "spacing", stage: "document_qa" }] : []),
          ],
          artifacts: [artifact(
            `qa:${candidateId}`,
            "qa_report",
            `reports/qa/${candidateId}.json`,
          )],
        });
      }
      if (tool === "apply_repair") {
        return completed(tool, jobId, {
          summary: { output_created: true, output_candidate_id: "candidate-r1" },
          artifacts: [artifact("candidate:candidate-r1", "candidate_document", "candidates/candidate-r1.docx")],
        });
      }
      if (tool === "finalize_result") {
        return completed(tool, jobId, {
          artifacts: [artifact("result", "translated_document", "result/translated.docx")],
        });
      }
      return completed(tool, jobId);
    },
  };
  const runner = new AgentJobRunner(
    config,
    registry,
    bridge,
    null,
    new NoopProviderLimiter(),
  );

  await runner.run(job.id);

  expect(await registry.getJob(job.id)).toMatchObject({
    status: "NEEDS_REVIEW",
    candidateId: "candidate-r1",
    repairRound: 1,
    qaIssueCounts: { error: 1, warning: 0, info: 0 },
    errorCode: "qa_unresolved",
  });
  expect((await registry.getJob(job.id))?.resultRelpath).toBeUndefined();
  expect(calls).not.toContain("verify_candidate");
  expect(calls).not.toContain("finalize_result");
});

it("runs exactly two improving fixture repairs for blockers 3 to 2 to 1, then requires review", async () => {
  const config = await testConfig();
  cleanup.push(config.dataRoot);
  const registry = new InMemoryJobRegistry();
  const job = queuedJob("fixture-two-round-limit", 2);
  await registry.createJob(job, `${job.id}-idempotency`);
  const calls: PythonToolName[] = [];
  const blockerCounts: Record<string, number> = {
    "candidate-r0": 3,
    "candidate-r1": 2,
    "candidate-r2": 1,
  };

  const bridge: PythonToolBridge = {
    execute: async (tool, jobId, payload) => {
      calls.push(tool);
      if (tool === "translate_once") {
        return completed(tool, jobId, {
          summary: {
            requested_target_language: "zh",
            resolved_source_language: "en",
            resolved_target_language: "zh",
          },
          artifacts: [artifact("candidate:candidate-r0", "candidate_document", "candidates/candidate-r0.docx")],
        });
      }
      if (tool === "run_document_qa") {
        const candidateId = String(payload.candidate_id);
        const count = blockerCounts[candidateId];
        if (count === undefined) throw new Error(`unexpected QA candidate: ${candidateId}`);
        return completed(tool, jobId, {
          summary: { issue_counts: { error: count, warning: 0, info: 0 } },
          issues: Array.from({ length: count }, (_, index) => blockingIssue("overflow", index + 1)),
          artifacts: [artifact(
            `qa:${candidateId}`,
            "qa_report",
            `reports/qa/${candidateId}.json`,
          )],
        });
      }
      if (tool === "apply_repair") {
        const outputCandidateId = String(payload.output_candidate_id);
        return completed(tool, jobId, {
          summary: { output_created: true, output_candidate_id: outputCandidateId },
          artifacts: [artifact(
            `candidate:${outputCandidateId}`,
            "candidate_document",
            `candidates/${outputCandidateId}.docx`,
          )],
        });
      }
      if (tool === "finalize_result") {
        return completed(tool, jobId, {
          artifacts: [artifact("result", "translated_document", "result/translated.docx")],
        });
      }
      return completed(tool, jobId);
    },
  };
  const runner = new AgentJobRunner(
    config,
    registry,
    bridge,
    null,
    new NoopProviderLimiter(),
  );

  await runner.run(job.id);

  expect(await registry.getJob(job.id)).toMatchObject({
    status: "NEEDS_REVIEW",
    candidateId: "candidate-r2",
    repairRound: 2,
    qaIssueCounts: { error: 1, warning: 0, info: 0 },
    errorCode: "qa_unresolved",
    errorSummary: expect.stringContaining("after 2 repair round(s)"),
  });
  expect((await registry.getJob(job.id))?.resultRelpath).toBeUndefined();
  expect(calls.filter((tool) => tool === "run_document_qa")).toHaveLength(3);
  expect(calls.filter((tool) => tool === "apply_repair")).toHaveLength(2);
  expect(calls).not.toContain("verify_candidate");
  expect(calls).not.toContain("finalize_result");
});

function queuedJob(id: string, maxRepairRounds: number): TranslationJobRecord {
  const now = new Date();
  return {
    id,
    sourceId: "source",
    executionMode: "pi_agent_live",
    sourceFilename: "source.docx",
    inputRelpath: "input/source.docx",
    sourceLanguage: "en",
    targetLanguage: "zh",
    requestedTargetLanguage: "zh",
    qualityProfile: "standard",
    maxRepairRounds,
    repairRound: 0,
    status: "QUEUED",
    softDeadlineAt: new Date(now.getTime() + 60_000).toISOString(),
    hardDeadlineAt: new Date(now.getTime() + 120_000).toISOString(),
    createdAt: now.toISOString(),
    updatedAt: now.toISOString(),
  };
}

function completed(
  tool: PythonToolName,
  jobId: string,
  overrides: Partial<PythonToolEnvelope> = {},
): PythonToolEnvelope {
  return {
    tool,
    job_id: jobId,
    status: "COMPLETED",
    summary: {},
    artifacts: [],
    issues: [],
    error: null,
    ...overrides,
  };
}

function artifact(
  artifactId: string,
  kind: string,
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

function blockingIssue(code: string, page: number): Record<string, unknown> {
  return {
    code,
    message: `${code} detected`,
    severity: "error",
    stage: "document_qa",
    page,
  };
}
