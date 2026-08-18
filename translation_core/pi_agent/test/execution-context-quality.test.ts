import { rm } from "node:fs/promises";
import { afterEach, expect, it } from "vitest";
import type {
  PythonToolEnvelope,
  QaIssueCounts,
  TranslationJobRecord,
} from "../src/contracts.js";
import { SafeEventLog } from "../src/event-log.js";
import { JobExecutionContext } from "../src/execution-context.js";
import { InMemoryJobRegistry } from "../src/registry.js";
import { testConfig } from "./helpers.js";

const cleanup: string[] = [];
afterEach(async () => Promise.all(cleanup.splice(0).map((value) => rm(value, { recursive: true, force: true }))));

it.each([
  {
    name: "no-op",
    parentIssues: [issue("OMISSION", "paragraph-1")],
    candidateIssues: [issue("OMISSION", "paragraph-1")],
    parentCounts: counts(1),
    candidateCounts: counts(1),
    parentScore: 90,
    candidateScore: 90,
  },
  {
    name: "no-op with inconsistent claimed score",
    parentIssues: [issue("OMISSION", "paragraph-1")],
    candidateIssues: [issue("OMISSION", "paragraph-1")],
    parentCounts: counts(1),
    candidateCounts: counts(1),
    parentScore: 90,
    candidateScore: 99,
  },
  {
    name: "worse",
    parentIssues: [issue("OMISSION", "paragraph-1")],
    candidateIssues: [issue("OMISSION", "paragraph-1"), issue("MEANING", "paragraph-2")],
    parentCounts: counts(1),
    candidateCounts: counts(2),
    parentScore: 90,
    candidateScore: 80,
  },
])("rejects a $name repaired candidate and restores the persisted parent snapshot", async (scenario) => {
  const fixture = await contextFixture(`quality-${scenario.name.replaceAll(" ", "-")}`);
  await fixture.context.recordQa(
    qaEnvelope("candidate-r0", scenario.parentScore, scenario.parentCounts, scenario.parentIssues),
  );
  await fixture.context.selectCandidate("candidate-r1", { repairRound: 1 });

  const awaitingQa = (await fixture.registry.getJob(fixture.job.id))!;
  expect(awaitingQa).toMatchObject({
    candidateId: "candidate-r1",
    qaQualityScore: null,
    qaIssueCounts: null,
    repairParentCandidateId: "candidate-r0",
    repairParentQaQualityScore: scenario.parentScore,
    repairParentQaIssueCounts: scenario.parentCounts,
    repairParentReportRelpath: "reports/qa/candidate-r0.json",
    reportRelpath: null,
  });

  const resumed = new JobExecutionContext(
    awaitingQa,
    fixture.registry,
    fixture.events,
    fixture.config,
  );
  expect(resumed.qaIssues).toBe(0);
  await resumed.recordQa(
    qaEnvelope("candidate-r1", scenario.candidateScore, scenario.candidateCounts, scenario.candidateIssues),
  );

  const rejected = await fixture.registry.getJob(fixture.job.id);
  expect(rejected).toMatchObject({
    status: "NEEDS_REVIEW",
    errorCode: "repair_not_improved",
    candidateId: "candidate-r0",
    qaQualityScore: scenario.parentScore,
    qaIssueCounts: scenario.parentCounts,
    reportRelpath: "reports/qa/candidate-r0.json",
    repairParentCandidateId: null,
  });
  expect(resumed.qaIssues).toBe(
    scenario.parentCounts.error + scenario.parentCounts.warning + scenario.parentCounts.info,
  );
});

it("persists QA state across restart and rejects a newly introduced blocking fingerprint", async () => {
  const fixture = await contextFixture("quality-new-blocker");
  const parentIssues = [issue("OMISSION", "paragraph-1"), issue("MEANING", "paragraph-2")];
  await fixture.context.recordQa(
    qaEnvelope("candidate-r0", 80, counts(2), parentIssues),
  );

  const persistedBaseline = (await fixture.registry.getJob(fixture.job.id))!;
  expect(persistedBaseline.qaBlockingIssueFingerprints).toHaveLength(2);
  expect(persistedBaseline.qaBlockingIssueFingerprints?.every((value) => /^[a-f0-9]{64}$/.test(value))).toBe(true);
  expect(JSON.stringify(persistedBaseline)).not.toContain("paragraph-1");
  expect(new JobExecutionContext(
    persistedBaseline,
    fixture.registry,
    fixture.events,
    fixture.config,
  ).qaIssues).toBe(2);

  await fixture.context.selectCandidate("candidate-r1", { repairRound: 1 });
  const resumedRecord = (await fixture.registry.getJob(fixture.job.id))!;
  const resumed = new JobExecutionContext(
    resumedRecord,
    fixture.registry,
    fixture.events,
    fixture.config,
  );
  await resumed.recordQa(
    qaEnvelope("candidate-r1", 90, counts(1), [issue("NEW_BLOCKER", "paragraph-9")]),
  );

  expect(await fixture.registry.getJob(fixture.job.id)).toMatchObject({
    status: "NEEDS_REVIEW",
    errorCode: "repair_not_improved",
    candidateId: "candidate-r0",
    qaQualityScore: 80,
  });
});

it("accepts only a strict improvement whose blocking errors are a parent subset", async () => {
  const fixture = await contextFixture("quality-improved");
  const retained = issue("OMISSION", "paragraph-1");
  await fixture.context.recordQa(
    qaEnvelope("candidate-r0", 80, counts(2), [retained, issue("MEANING", "paragraph-2")]),
  );
  await fixture.context.selectCandidate("candidate-r1", { repairRound: 1 });
  await fixture.context.recordQa(
    qaEnvelope("candidate-r1", 90, counts(1), [retained]),
  );

  expect(await fixture.registry.getJob(fixture.job.id)).toMatchObject({
    status: "QA",
    candidateId: "candidate-r1",
    qaQualityScore: 90,
    qaIssueCounts: counts(1),
    reportRelpath: "reports/qa/candidate-r1.json",
    repairParentCandidateId: null,
    repairParentQaQualityScore: null,
  });
});

it("accepts an error-to-warning improvement and tolerates issue-code drift at a stable location", async () => {
  const fixture = await contextFixture("quality-severity-improved");
  await fixture.context.recordQa(
    qaEnvelope(
      "candidate-r0",
      88,
      counts(1, 1),
      [issue("omission_vertical_test_stand", "paragraph-1"), warning("STYLE", "paragraph-2")],
    ),
  );
  await fixture.context.selectCandidate("candidate-r1", { repairRound: 1 });
  await fixture.context.recordQa(
    qaEnvelope(
      "candidate-r1",
      90,
      counts(1),
      [issue("omission", "paragraph-1")],
    ),
  );
  expect(await fixture.registry.getJob(fixture.job.id)).toMatchObject({
    status: "QA",
    candidateId: "candidate-r1",
    qaQualityScore: 90,
  });

  const second = await contextFixture("quality-error-to-warning");
  await second.context.recordQa(
    qaEnvelope("candidate-r0", 90, counts(1), [issue("MEANING", "paragraph-1")]),
  );
  await second.context.selectCandidate("candidate-r1", { repairRound: 1 });
  await second.context.recordQa(
    qaEnvelope("candidate-r1", 98, counts(0, 1), [warning("WORDING", "paragraph-1")]),
  );
  expect(await second.registry.getJob(second.job.id)).toMatchObject({
    status: "QA",
    candidateId: "candidate-r1",
    qaIssueCounts: counts(0, 1),
  });
});

it("fails closed when the QA summary contains blocking issues omitted from the bounded envelope", async () => {
  const fixture = await contextFixture("quality-truncated-blockers");
  await fixture.context.recordQa(
    qaEnvelope("candidate-r0", 80, counts(2), [issue("OMISSION", "paragraph-1")]),
  );
  expect((await fixture.registry.getJob(fixture.job.id))?.qaBlockingIssueFingerprintOverflow).toBe(true);

  await fixture.context.selectCandidate("candidate-r1", { repairRound: 1 });
  await fixture.context.recordQa(
    qaEnvelope("candidate-r1", 90, counts(1), [issue("OMISSION", "paragraph-1")]),
  );

  expect(await fixture.registry.getJob(fixture.job.id)).toMatchObject({
    status: "NEEDS_REVIEW",
    errorCode: "repair_not_improved",
    candidateId: "candidate-r0",
  });
});

it("does not trust QA summary counts that under-report supplied blocking issues", async () => {
  const fixture = await contextFixture("quality-underreported-summary");
  await fixture.context.recordQa(
    qaEnvelope(
      "candidate-r0",
      100,
      counts(0),
      [issue("SEMANTIC_OMISSION", "paragraph-1")],
    ),
  );

  expect(await fixture.registry.getJob(fixture.job.id)).toMatchObject({
    qaQualityScore: 90,
    qaIssueCounts: counts(1),
  });
  expect(fixture.context.qaIssues).toBe(1);
  expect(fixture.context.qaBlockingIssues).toBe(1);
});

async function contextFixture(id: string) {
  const config = await testConfig();
  cleanup.push(config.dataRoot);
  const now = new Date();
  const job: TranslationJobRecord = {
    id,
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
  await registry.createJob(job, `${id}-idempotency`);
  const events = new SafeEventLog(config.jobsRoot);
  return {
    job,
    registry,
    events,
    config,
    context: new JobExecutionContext(job, registry, events, config),
  };
}

function qaEnvelope(
  candidateId: string,
  qualityScore: number,
  issueCounts: QaIssueCounts,
  issues: Array<Record<string, unknown>>,
): PythonToolEnvelope {
  return {
    tool: "run_document_qa",
    job_id: "quality-job",
    status: issueCounts.error > 0 ? "NEEDS_REVIEW" : "COMPLETED",
    summary: {
      candidate_id: candidateId,
      quality_score: qualityScore,
      issue_counts: issueCounts,
    },
    artifacts: [{
      artifact_id: `qa:${candidateId}`,
      kind: "qa_report",
      relpath: `reports/qa/${candidateId}.json`,
      filename: `${candidateId}.json`,
      size_bytes: 1,
      sha256: "0".repeat(64),
    }],
    issues,
    error: null,
  };
}

function counts(error: number, warning = 0, info = 0): QaIssueCounts {
  return { error, warning, info };
}

function issue(code: string, blockId: string): Record<string, unknown> {
  return {
    severity: "error",
    code,
    stage: "semantic",
    block_id: blockId,
    message: `raw message for ${blockId}`,
  };
}

function warning(code: string, blockId: string): Record<string, unknown> {
  return { ...issue(code, blockId), severity: "warning" };
}
