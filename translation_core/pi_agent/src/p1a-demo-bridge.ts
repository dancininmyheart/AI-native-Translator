import path from "node:path";
import { constants } from "node:fs";
import { copyFile, mkdir, readFile, writeFile } from "node:fs/promises";
import { createHash } from "node:crypto";
import type { AppConfig } from "./config.js";
import type { PythonToolEnvelope, PythonToolName } from "./contracts.js";
import { resolveContained } from "./path-policy.js";
import { PythonCliToolBridge, type PythonToolBridge, type PythonToolPayload } from "./python-tool-bridge.js";

interface CandidateStep {
  sourcePath: string;
  sha256: string;
  blockingCount: number;
}

/**
 * A demo-only adapter around the production Python CLI boundary.
 *
 * The two scripted PPTX paths use immutable, pre-generated candidates at each
 * bounded round. Python Core still performs preflight and full QA for every
 * candidate. No scenario selector is accepted by the HTTP API and this class
 * is deliberately unreachable from production main.ts.
 */
export class P1aDemoPythonBridge implements PythonToolBridge {
  private readonly nextStepByJob = new Map<string, number>();

  constructor(
    private readonly config: AppConfig,
    private readonly steps: CandidateStep[],
    private readonly realBridge = new PythonCliToolBridge(config),
  ) {}

  async execute(
    tool: PythonToolName,
    jobId: string,
    payload: PythonToolPayload,
    signal?: AbortSignal,
  ): Promise<PythonToolEnvelope> {
    if (tool === "run_document_qa") {
      const envelope = await this.realBridge.execute(tool, jobId, payload, signal);
      const candidateId = String(payload.candidate_id);
      const round = candidateRound(candidateId);
      const step = this.steps[round];
      if (!step) throw new Error(`P1-A manifest has no QA step for ${candidateId}`);
      const actualErrors = Number((envelope.summary.issue_counts as Record<string, unknown> | undefined)?.error ?? 0);
      if (actualErrors !== step.blockingCount) {
        throw new Error(`P1-A Python QA count mismatch for ${candidateId}: expected ${step.blockingCount}, got ${actualErrors}`);
      }
      return envelope;
    }
    if (tool !== "apply_repair") return this.realBridge.execute(tool, jobId, payload, signal);

    const outputCandidateId = String(payload.output_candidate_id);
    const outputRound = candidateRound(outputCandidateId);
    const step = this.steps[outputRound];
    if (!step) throw new Error(`P1-A manifest has no repair candidate for ${outputCandidateId}`);
    const priorRound = this.nextStepByJob.get(jobId) ?? 0;
    if (outputRound !== priorRound + 1) throw new Error("P1-A candidate sequence is out of order");
    this.nextStepByJob.set(jobId, outputRound);

    const workspace = path.join(this.config.jobsRoot, jobId);
    const extension = path.extname(step.sourcePath).toLowerCase();
    const relpath = `input/scripted-${outputCandidateId}${extension}`;
    const destination = resolveContained(workspace, relpath);
    await mkdir(path.dirname(destination), { recursive: true });
    await copyFile(step.sourcePath, destination, constants.COPYFILE_EXCL);
    const digest = createHash("sha256").update(await readFile(destination)).digest("hex");
    if (digest !== step.sha256) throw new Error(`P1-A immutable candidate hash mismatch for ${outputCandidateId}`);

    const request = await readJobRequest(workspace);
    const registration = await this.realBridge.execute(
      "preflight_document",
      jobId,
      {
        input_relpath: request.input_relpath,
        existing_translation_relpath: relpath,
        candidate_id: outputCandidateId,
        source_language: request.source_language,
        target_language: request.target_language,
      },
      signal,
    );
    const candidateArtifact = registration.artifacts.find(
      (artifact) => artifact.artifact_id === `candidate:${outputCandidateId}` && artifact.kind === "candidate_document",
    );
    if (!candidateArtifact || candidateArtifact.sha256 !== digest) {
      throw new Error(`P1-A Python candidate registration failed for ${outputCandidateId}`);
    }

    const reportRelpath = `reports/demo-repair/${outputCandidateId}.json`;
    const report = resolveContained(workspace, reportRelpath);
    await mkdir(path.dirname(report), { recursive: true });
    await writeFile(report, `${JSON.stringify({
      schema_version: 1,
      evidence_kind: "scripted_fixture_candidate",
      candidate_id: outputCandidateId,
      candidate_sha256: digest,
    })}\n`, { encoding: "utf8", flag: "wx", mode: 0o600 });
    const reportBytes = await readFile(report);

    return {
      tool,
      job_id: jobId,
      status: "COMPLETED",
      summary: {
        output_created: true,
        output_candidate_id: outputCandidateId,
        evidence_kind: "scripted_fixture_candidate",
      },
      artifacts: [
        candidateArtifact,
        {
          artifact_id: `repair:${outputCandidateId}`,
          kind: "repair_report",
          relpath: reportRelpath,
          filename: path.basename(report),
          size_bytes: reportBytes.length,
          sha256: createHash("sha256").update(reportBytes).digest("hex"),
        },
      ],
      issues: [],
      error: null,
    };
  }
}

/** Stages the public translation mapping, then delegates every tool to Python Core. */
export class P1aFixtureTranslationBridge implements PythonToolBridge {
  constructor(
    private readonly config: AppConfig,
    private readonly fixtureSource: string,
    private readonly realBridge = new PythonCliToolBridge(config),
  ) {}

  async execute(
    tool: PythonToolName,
    jobId: string,
    payload: PythonToolPayload,
    signal?: AbortSignal,
  ): Promise<PythonToolEnvelope> {
    if (tool === "translate_once") {
      const destination = resolveContained(path.join(this.config.jobsRoot, jobId), "fixture.json");
      const expected = await readFile(this.fixtureSource);
      try {
        await writeFile(destination, expected, { flag: "wx", mode: 0o600 });
      } catch (error) {
        if (!isExistingPathError(error) || !Buffer.from(await readFile(destination)).equals(expected)) throw error;
      }
    }
    return this.realBridge.execute(tool, jobId, payload, signal);
  }
}

function candidateRound(candidateId: string): number {
  const match = /^candidate-r([0-3])$/.exec(candidateId);
  if (!match) throw new Error(`invalid P1-A candidate identifier: ${candidateId}`);
  return Number(match[1]);
}

function isExistingPathError(error: unknown): boolean {
  return error !== null && typeof error === "object" && "code" in error && (error as { code?: unknown }).code === "EEXIST";
}

async function readJobRequest(workspace: string): Promise<{
  input_relpath: string;
  source_language: string;
  target_language: string;
}> {
  const parsed = JSON.parse(await readFile(resolveContained(workspace, "job-request.json"), "utf8")) as unknown;
  if (!isRecord(parsed)) throw new Error("P1-A job request metadata is invalid");
  const { input_relpath, source_language, target_language } = parsed;
  if (![input_relpath, source_language, target_language].every((value) => typeof value === "string" && value.length > 0)) {
    throw new Error("P1-A job request metadata is incomplete");
  }
  return {
    input_relpath: input_relpath as string,
    source_language: source_language as string,
    target_language: target_language as string,
  };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}
