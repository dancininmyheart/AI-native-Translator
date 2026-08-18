import path from "node:path";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { describe, expect, it } from "vitest";
import { runPublicTraceCli } from "../src/public-trace-cli.js";

describe("public trace CLI", () => {
  it("replays a validated trace from disk without exposing untrusted fields", async () => {
    const directory = await mkdtemp(path.join(tmpdir(), "public-trace-cli-"));
    try {
      const tracePath = path.join(directory, "trace.json");
      await writeFile(tracePath, JSON.stringify({
        schema_version: 1,
        trace_id: "cli-replay",
        generated_at: "2026-08-18T00:00:00.000Z",
        document: { format: "pptx" },
        evidence: {
          kind: "scripted_fixture_candidate",
          pi_session: false,
          model_calls: 0,
          ordering: "within_stream",
        },
        terminal: { status: "NEEDS_REVIEW", result_http_status: 409, reason_code: "budget_exhausted" },
        states: [{ sequence: 0, status: "NEEDS_REVIEW", elapsed_ms: null }],
        tool_calls: [],
        qa_snapshots: [{
          sequence: 0,
          blocking_count: 1,
          quality_score: 90,
          issue_counts: null,
        }],
        candidates: [{
          sequence: 0,
          candidate_id: "candidate-r2",
          sha256: "631e3c04bbcb148cfb99e77936fca6eded184765d444d54ef88fc41089a212a4",
          selected: true,
          released: false,
        }],
        usage: {
          input_tokens: null,
          output_tokens: null,
          total_tokens: null,
          cost_usd: null,
          latency_ms: 22488,
        },
      }), "utf8");
      let stdout = "";
      let stderr = "";

      const exitCode = await runPublicTraceCli(["replay", tracePath], {
        stdout: (text) => { stdout += text; },
        stderr: (text) => { stderr += text; },
      });

      expect(exitCode).toBe(0);
      expect(stderr).toBe("");
      expect(stdout).toContain("TRACE cli-replay schema_version=1");
      expect(stdout).toContain("TERMINAL NEEDS_REVIEW http=409 reason=budget_exhausted");
      expect(stdout).toContain("cost_usd=not recorded");
    } finally {
      await rm(directory, { recursive: true, force: true });
    }
  });

  it("writes versioned allowlist projections from the offline demo envelope", async () => {
    const directory = await mkdtemp(path.join(tmpdir(), "public-trace-project-"));
    try {
      const inputPath = path.join(directory, "demo-trace.json");
      const outputDirectory = path.join(directory, "public");
      await writeFile(inputPath, JSON.stringify({
        schema_version: 1,
        demo: "p1a-three-path-offline",
        pi_session: false,
        model_calls: 0,
        scenario_count: 3,
        secret_prompt: "must never be copied",
        scenarios: p1aDemoScenarios(),
      }), "utf8");
      let stdout = "";

      const exitCode = await runPublicTraceCli(
        ["project-p1a", inputPath, outputDirectory, "2026-08-18T00:00:00.000Z"],
        { stdout: (text) => { stdout += text; }, stderr: () => undefined },
      );

      expect(exitCode).toBe(0);
      const outputPath = path.join(outputDirectory, "p1a-direct-docx.json");
      const projected = await readFile(outputPath, "utf8");
      expect(stdout).toContain(outputPath);
      expect(JSON.parse(projected)).toMatchObject({
        schema_version: 1,
        trace_id: "p1a-direct-docx",
        evidence: { pi_session: false, model_calls: 0 },
      });
      expect(projected).not.toMatch(/secret_prompt|must never be copied/i);
    } finally {
      await rm(directory, { recursive: true, force: true });
    }
  });
});

function p1aDemoScenarios(): Array<Record<string, unknown>> {
  const directHash = "14d4a537f6b6c391d003867b7a5e5dd714580b5d2dfc704193e0ad3f092eabac";
  const repairHash = "d1264225654c4b5c6e2e05b2cc9b5daef350c06d1f5afa0b89379ec5db255b07";
  return [
    {
      scenario_id: "direct-docx",
      evidence_kind: "real_python_core",
      pi_session: false,
      model_calls: 0,
      terminal_status: "COMPLETED",
      result_http_status: 200,
      blocking_counts: [0],
      quality_scores: [100],
      repair_decisions: ["not_needed"],
      selected_candidate_id: "candidate-r0",
      selected_candidate_sha256: directHash,
      result_sha256: directHash,
      elapsed_ms: 4140,
      state_transitions: ["PREFLIGHT", "TRANSLATING", "QA", "VERIFYING", "QA", "COMPLETED"],
      tool_sequence: ["preflight_document", "translate_once", "run_document_qa", "verify_candidate", "finalize_result"],
    },
    {
      scenario_id: "repair-pptx",
      evidence_kind: "scripted_fixture_candidate",
      pi_session: false,
      model_calls: 0,
      terminal_status: "COMPLETED",
      result_http_status: 200,
      blocking_counts: [2, 0],
      quality_scores: [80, 100],
      repair_decisions: ["promote:candidate-r1"],
      selected_candidate_id: "candidate-r1",
      selected_candidate_sha256: repairHash,
      result_sha256: repairHash,
      elapsed_ms: 24407,
      state_transitions: ["PREFLIGHT", "QA", "REPAIRING", "QA", "VERIFYING", "QA", "COMPLETED"],
      tool_sequence: ["preflight_document", "run_document_qa", "apply_repair", "run_document_qa", "verify_candidate", "finalize_result"],
    },
    {
      scenario_id: "high-risk-pptx",
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
    },
  ];
}
