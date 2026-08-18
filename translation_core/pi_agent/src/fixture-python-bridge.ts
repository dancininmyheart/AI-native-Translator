import path from "node:path";
import { copyFile, mkdir, writeFile } from "node:fs/promises";
import type { AppConfig } from "./config.js";
import type { PythonToolEnvelope, PythonToolName } from "./contracts.js";
import { assertSafeId, resolveContained } from "./path-policy.js";
import type { PythonToolBridge, PythonToolPayload } from "./python-tool-bridge.js";

export class FixturePythonToolBridge implements PythonToolBridge {
  constructor(private readonly config: AppConfig) {}

  async execute(
    tool: PythonToolName,
    jobId: string,
    payload: PythonToolPayload,
    signal?: AbortSignal,
  ): Promise<PythonToolEnvelope> {
    assertSafeId(jobId, "job_id");
    if (signal?.aborted) throw new Error("fixture tool aborted");
    const workspace = path.join(this.config.jobsRoot, jobId);
    const artifacts: PythonToolEnvelope["artifacts"] = [];
    if (tool === "preflight_document" && payload.existing_translation_relpath) {
      const candidateId = String(payload.candidate_id);
      const suppliedRelpath = String(payload.existing_translation_relpath);
      const supplied = resolveContained(workspace, suppliedRelpath);
      const outputRelpath = `candidates/${candidateId}${path.extname(supplied).toLowerCase()}`;
      const output = resolveContained(workspace, outputRelpath);
      await mkdir(path.dirname(output), { recursive: true });
      await copyFile(supplied, output);
      artifacts.push(await fixtureArtifact(output, outputRelpath, `candidate:${candidateId}`, "candidate_document"));
    }
    if (tool === "translate_once") {
      const candidateId = String(payload.candidate_id);
      const inputRelpath = String(payload.input_relpath);
      const input = resolveContained(workspace, inputRelpath);
      const inputExtension = path.extname(input).toLowerCase();
      const outputExtension = inputExtension === ".doc" ? ".docx" : inputExtension === ".ppt" ? ".pptx" : inputExtension;
      const candidateRelpath = `candidates/${candidateId}${outputExtension}`;
      const candidate = resolveContained(workspace, candidateRelpath);
      await mkdir(path.dirname(candidate), { recursive: true });
      await copyFile(input, candidate);
      artifacts.push(await fixtureArtifact(candidate, candidateRelpath, `candidate:${candidateId}`, "candidate_document"));
    }
    if (tool === "apply_repair") {
      const sourceId = String(payload.candidate_id);
      const outputId = String(payload.output_candidate_id);
      const candidatesDir = resolveContained(workspace, "candidates");
      const sourceCandidates = [".docx", ".pptx"].map((extension) => path.join(candidatesDir, `${sourceId}${extension}`));
      const { access } = await import("node:fs/promises");
      let source = "";
      for (const value of sourceCandidates) {
        try {
          await access(value);
          source = value;
          break;
        } catch {}
      }
      if (!source) throw new Error("fixture candidate not found");
      const outputRelpath = `candidates/${outputId}${path.extname(source)}`;
      const output = resolveContained(workspace, outputRelpath);
      await copyFile(source, output);
      artifacts.push(await fixtureArtifact(output, outputRelpath, `candidate:${outputId}`, "candidate_document"));
    }
    if (tool === "run_document_qa" || tool === "verify_candidate") {
      const candidateId = String(payload.candidate_id);
      const reportKind = tool === "run_document_qa" ? "qa_report" : "verification_report";
      const reportRelpath = `reports/${reportKind}/${candidateId}.json`;
      const report = resolveContained(workspace, reportRelpath);
      await mkdir(path.dirname(report), { recursive: true });
      await writeFile(report, JSON.stringify({ fixture: true, candidate_id: candidateId, issues: [] }), "utf8");
      artifacts.push(await fixtureArtifact(report, reportRelpath, `${reportKind}:${candidateId}`, reportKind));
    }
    if (tool === "finalize_result") {
      const candidateId = String(payload.candidate_id);
      const candidatesDir = resolveContained(workspace, "candidates");
      const { access } = await import("node:fs/promises");
      let source = "";
      for (const extension of [".docx", ".pptx"]) {
        const value = path.join(candidatesDir, `${candidateId}${extension}`);
        try {
          await access(value);
          source = value;
          break;
        } catch {}
      }
      if (!source) throw new Error("fixture candidate not found");
      const resultRelpath = `result/translated${path.extname(source)}`;
      const reportRelpath = "result/qa-report.json";
      const result = resolveContained(workspace, resultRelpath);
      await mkdir(path.dirname(result), { recursive: true });
      await copyFile(source, result);
      await writeFile(
        resolveContained(workspace, reportRelpath),
        JSON.stringify({ fixture: true, candidate_id: candidateId, issues: [] }),
        "utf8",
      );
      artifacts.push(await fixtureArtifact(result, resultRelpath, "final_result", "translated_document"));
    }
    const summary: Record<string, unknown> = { fixture: true, completed: true };
    if (tool === "translate_once") {
      const requestedSource = String(payload.source_language ?? "auto").toLowerCase();
      const requestedTarget = String(payload.target_language ?? "auto").toLowerCase();
      const resolvedSource = requestedSource === "auto" ? "und" : requestedSource;
      const resolvedTarget = requestedTarget === "auto"
        ? (resolvedSource.split("-", 1)[0] === "zh" ? "en" : "zh")
        : requestedTarget.split("-", 1)[0];
      summary.source_language = resolvedSource;
      summary.requested_target_language = requestedTarget;
      summary.resolved_target_language = resolvedTarget;
      summary.target_language = resolvedTarget;
    }
    return {
      tool,
      job_id: jobId,
      status: "COMPLETED",
      summary,
      artifacts,
      issues: [],
      error: null,
    };
  }
}

async function fixtureArtifact(
  absolutePath: string,
  relpath: string,
  artifactId: string,
  kind: string,
): Promise<PythonToolEnvelope["artifacts"][number]> {
  const { createHash } = await import("node:crypto");
  const { readFile, stat } = await import("node:fs/promises");
  const data = await readFile(absolutePath);
  return {
    artifact_id: artifactId,
    kind,
    relpath,
    filename: path.basename(absolutePath),
    size_bytes: (await stat(absolutePath)).size,
    sha256: createHash("sha256").update(data).digest("hex"),
  };
}
