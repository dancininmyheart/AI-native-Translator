import path from "node:path";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { afterEach, describe, expect, it } from "vitest";
import { resolveRepositoryRoot, runP1aDemo } from "../src/p1a-demo.js";

const cleanup: string[] = [];

afterEach(async () => {
  await Promise.all(cleanup.splice(0).map(async (value) => {
    for (let attempt = 0; attempt < 5; attempt += 1) {
      try {
        await rm(value, { recursive: true, force: true, maxRetries: 3, retryDelay: 100 });
        return;
      } catch (error) {
        if (attempt === 4) throw error;
        await new Promise((resolve) => setTimeout(resolve, 100));
      }
    }
  }));
}, 30_000);

describe("P1-A public demo", () => {
  it("locates the repository from both source and compiled module depths", () => {
    const repo = resolveRepositoryRoot();
    expect(resolveRepositoryRoot(path.join(repo, "translation_core", "pi_agent", "src"))).toBe(repo);
    expect(resolveRepositoryRoot(path.join(repo, "translation_core", "pi_agent", "dist", "src"))).toBe(repo);
  });

  it("replays pass, promoted repair, and fail-closed paths with sanitized evidence", async () => {
    const outputDir = await mkdtemp(path.join(tmpdir(), "pi-agent-p1a-demo-"));
    cleanup.push(outputDir);

    const summary = await runP1aDemo({ outputDir });

    expect(summary).toMatchObject({
      schema_version: 1,
      pi_session: false,
      model_calls: 0,
      scenario_count: 3,
      scenarios: [
        {
          scenario_id: "direct-docx",
          evidence_kind: "real_python_core",
          native_python_qa: true,
          repair_execution: "not_applicable",
          terminal_status: "COMPLETED",
          result_http_status: 200,
          blocking_counts: [0],
          quality_scores: [100],
          repair_decisions: ["not_needed"],
          selected_candidate_id: "candidate-r0",
        },
        {
          scenario_id: "repair-pptx",
          evidence_kind: "scripted_fixture_candidate",
          native_python_qa: true,
          repair_execution: "scripted_fixture_candidate",
          terminal_status: "COMPLETED",
          result_http_status: 200,
          blocking_counts: [2, 0],
          quality_scores: [80, 100],
          repair_decisions: ["promote:candidate-r1"],
          selected_candidate_id: "candidate-r1",
        },
        {
          scenario_id: "high-risk-pptx",
          evidence_kind: "scripted_fixture_candidate",
          native_python_qa: true,
          repair_execution: "scripted_fixture_candidate",
          terminal_status: "NEEDS_REVIEW",
          result_http_status: 409,
          blocking_counts: [3, 2, 1],
          quality_scores: [70, 80, 90],
          repair_decisions: ["promote:candidate-r1", "promote:candidate-r2", "stop:budget_exhausted"],
          repair_rounds: 2,
          max_repair_rounds: 2,
          selected_candidate_id: "candidate-r2",
        },
      ],
    });
    expect(summary.scenarios[1]!.result_sha256).toBe(summary.scenarios[1]!.selected_candidate_sha256);
    expect(summary.scenarios[2]!.result_sha256).toBeNull();
    expect(summary.scenarios.every((scenario) => scenario.elapsed_ms >= 0)).toBe(true);

    const tracePath = path.join(outputDir, "demo-trace.json");
    const traceText = await readFile(tracePath, "utf8");
    const trace = JSON.parse(traceText) as Record<string, unknown>;
    expect(trace).toEqual(summary);
    expect(traceText).not.toMatch(/authorization|api[_-]?key|bearer|source_text|translated_text/i);
    expect(traceText).not.toContain(outputDir.replaceAll("\\", "/"));
    const repeatDir = await mkdtemp(path.join(tmpdir(), "pi-agent-p1a-repeat-"));
    cleanup.push(repeatDir);
    const repeat = await runP1aDemo({ outputDir: repeatDir });
    const stableProjection = (value: typeof summary) => value.scenarios.map((scenario) => ({
      scenario_id: scenario.scenario_id,
      terminal_status: scenario.terminal_status,
      blocking_counts: scenario.blocking_counts,
      selected_candidate_sha256: scenario.selected_candidate_sha256,
      result_sha256: scenario.result_sha256,
    }));

    expect(stableProjection(repeat)).toEqual(stableProjection(summary));
    expect(summary.scenarios[0]!.selected_candidate_sha256).toBe(summary.scenarios[0]!.result_sha256);
  }, 240_000);
});
