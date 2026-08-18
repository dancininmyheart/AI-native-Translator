import path from "node:path";
import { createHash } from "node:crypto";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { afterEach, describe, expect, it } from "vitest";
import { runPublicEvalCommand } from "../src/eval-command.js";
import { generatePublicEvalFixtures } from "../src/eval-fixtures.js";
import { runPublicAgentEval } from "../src/eval-runner.js";

const cleanup: string[] = [];

afterEach(async () => {
  await Promise.all(cleanup.splice(0).map((value) => rm(value, { recursive: true, force: true })));
});

describe("public Agent evaluation", () => {
  it("makes all three repair strategies observable on the same frozen scenario", async () => {
    const repoRoot = path.resolve(import.meta.dirname, "../../..");
    const outputDir = await mkdtemp(path.join(tmpdir(), "officeguard-eval-"));
    cleanup.push(outputDir);

    const summary = await runPublicAgentEval({
      manifestPath: path.join(repoRoot, "evals", "corpus", "manifest.json"),
      outputDir,
    });

    const resultFor = (strategy: string) => summary.strategies
      .find((entry) => entry.strategy === strategy)!
      .scenarios.find((entry) => entry.scenario_id === "early-pass-then-regression-pptx")!;

    expect(resultFor("no_repair")).toMatchObject({
      selected_candidate_id: "candidate-r0",
      terminal_status: "NEEDS_REVIEW",
      result_http_status: 409,
      repair_attempts: 0,
    });
    expect(resultFor("fixed_two_round")).toMatchObject({
      selected_candidate_id: "candidate-r2",
      terminal_status: "NEEDS_REVIEW",
      result_http_status: 409,
      repair_attempts: 2,
    });
    expect(resultFor("bounded_agent")).toMatchObject({
      selected_candidate_id: "candidate-r1",
      terminal_status: "RELEASED",
      result_http_status: 200,
      repair_attempts: 1,
    });
  });

  it("rejects a manifest whose candidate rounds are not consecutive", async () => {
    const repoRoot = path.resolve(import.meta.dirname, "../../..");
    const outputDir = await mkdtemp(path.join(tmpdir(), "officeguard-eval-invalid-"));
    cleanup.push(outputDir);
    const manifest = JSON.parse(await readFile(path.join(repoRoot, "evals", "corpus", "manifest.json"), "utf8")) as {
      scenarios: Array<{ rounds: Array<{ candidate_id: string }> }>;
    };
    manifest.scenarios[0]!.rounds[1]!.candidate_id = "candidate-r9";
    const manifestPath = path.join(outputDir, "manifest.json");
    await writeFile(manifestPath, JSON.stringify(manifest), "utf8");

    await expect(runPublicAgentEval({ manifestPath, outputDir }))
      .rejects.toThrow("candidate-r1");
  });

  it("writes byte-stable versioned JSON and Markdown reports", async () => {
    const repoRoot = path.resolve(import.meta.dirname, "../../..");
    const firstOutput = await mkdtemp(path.join(tmpdir(), "officeguard-eval-first-"));
    const secondOutput = await mkdtemp(path.join(tmpdir(), "officeguard-eval-second-"));
    cleanup.push(firstOutput, secondOutput);
    const manifestPath = path.join(repoRoot, "evals", "corpus", "manifest.json");

    const summary = await runPublicAgentEval({ manifestPath, outputDir: firstOutput });
    await runPublicAgentEval({ manifestPath, outputDir: secondOutput });

    const firstJson = await readFile(path.join(firstOutput, "eval-summary.json"), "utf8");
    const secondJson = await readFile(path.join(secondOutput, "eval-summary.json"), "utf8");
    const firstMarkdown = await readFile(path.join(firstOutput, "eval-report.md"), "utf8");
    const secondMarkdown = await readFile(path.join(secondOutput, "eval-report.md"), "utf8");
    expect(JSON.parse(firstJson)).toEqual(summary);
    expect(summary.eval_version).toBe("1.0.0");
    expect(firstJson).toBe(secondJson);
    expect(firstMarkdown).toBe(secondMarkdown);
    expect(firstMarkdown).toContain("model_calls=0");
    expect(firstMarkdown).toContain("offline orchestration-policy replay");
    expect(firstMarkdown).toContain("not native Office QA evidence");
    expect(summary.evidence_boundary).toMatchObject({
      corpus_provenance: "synthetic_cc0",
      qa_annotations: "frozen_synthetic",
      office_artifacts: "runtime_generated_carriers",
      replay_latency: "deterministic_fixture_value_not_wall_clock",
    });
  });

  it("reports coverage, conditional risk, repair uplift, regressions, rounds, latency, and zero model cost", async () => {
    const repoRoot = path.resolve(import.meta.dirname, "../../..");
    const outputDir = await mkdtemp(path.join(tmpdir(), "officeguard-eval-metrics-"));
    cleanup.push(outputDir);
    const manifest = JSON.parse(await readFile(path.join(repoRoot, "evals", "corpus", "manifest.json"), "utf8")) as {
      scenarios: Array<{ scenario_id: string }>;
    };
    manifest.scenarios = manifest.scenarios.filter((scenario) => scenario.scenario_id === "early-pass-then-regression-pptx");
    const manifestPath = path.join(outputDir, "metrics-manifest.json");
    await writeFile(manifestPath, JSON.stringify(manifest), "utf8");
    const summary = await runPublicAgentEval({
      manifestPath,
      outputDir,
    });
    const metricsFor = (strategy: string) => summary.strategies.find((entry) => entry.strategy === strategy)!.metrics;

    expect(metricsFor("no_repair")).toMatchObject({
      scenario_count: 1,
      released_count: 0,
      release_coverage: 0,
      conditional_risk: null,
      repair_uplift_rate: 0,
      new_regression_rate: 0,
      average_repair_attempts: 0,
      replay_latency_ms_p50: 8,
      replay_latency_ms_p95: 8,
      model_calls: 0,
      total_tokens: 0,
      estimated_cost_usd: 0,
    });
    expect(metricsFor("fixed_two_round")).toMatchObject({
      release_coverage: 0,
      conditional_risk: null,
      new_regression_rate: 1,
      average_repair_attempts: 2,
      replay_latency_ms_p50: 19,
    });
    expect(metricsFor("bounded_agent")).toMatchObject({
      released_count: 1,
      release_coverage: 1,
      conditional_risk: 0,
      repair_uplift_rate: 1,
      new_regression_rate: 0,
      average_repair_attempts: 1,
      replay_latency_ms_p50: 13,
    });
  });

  it("rejects corpus paths that escape the public fixture root", async () => {
    const repoRoot = path.resolve(import.meta.dirname, "../../..");
    const outputDir = await mkdtemp(path.join(tmpdir(), "officeguard-eval-path-"));
    cleanup.push(outputDir);
    const manifest = JSON.parse(await readFile(path.join(repoRoot, "evals", "corpus", "manifest.json"), "utf8")) as {
      scenarios: Array<{ source_relpath: string }>;
    };
    manifest.scenarios[0]!.source_relpath = "../private.pptx";
    const manifestPath = path.join(outputDir, "manifest.json");
    await writeFile(manifestPath, JSON.stringify(manifest), "utf8");

    await expect(runPublicAgentEval({ manifestPath, outputDir }))
      .rejects.toThrow("source_relpath");
  });

  it("rejects duplicate scenario identities", async () => {
    const repoRoot = path.resolve(import.meta.dirname, "../../..");
    const outputDir = await mkdtemp(path.join(tmpdir(), "officeguard-eval-duplicate-"));
    cleanup.push(outputDir);
    const manifest = JSON.parse(await readFile(path.join(repoRoot, "evals", "corpus", "manifest.json"), "utf8")) as {
      scenarios: Array<Record<string, unknown>>;
    };
    manifest.scenarios.push(structuredClone(manifest.scenarios[0]!));
    const manifestPath = path.join(outputDir, "manifest.json");
    await writeFile(manifestPath, JSON.stringify(manifest), "utf8");

    await expect(runPublicAgentEval({ manifestPath, outputDir }))
      .rejects.toThrow("duplicate scenario_id");
  });

  it("generates byte-stable licensed Office fixtures at runtime", async () => {
    const repoRoot = path.resolve(import.meta.dirname, "../../..");
    const firstOutput = await mkdtemp(path.join(tmpdir(), "officeguard-corpus-first-"));
    const secondOutput = await mkdtemp(path.join(tmpdir(), "officeguard-corpus-second-"));
    cleanup.push(firstOutput, secondOutput);
    const manifestPath = path.join(repoRoot, "evals", "corpus", "manifest.json");

    const first = await generatePublicEvalFixtures({ repoRoot, manifestPath, outputDir: firstOutput });
    const second = await generatePublicEvalFixtures({ repoRoot, manifestPath, outputDir: secondOutput });

    expect(first).toEqual(second);
    expect(first).toMatchObject({
      schema_version: 1,
      corpus_id: "officeguard-public-policy-eval",
      corpus_version: "1.0.0",
      license: "CC0-1.0",
      model_calls: 0,
    });
    const manifest = JSON.parse(await readFile(manifestPath, "utf8")) as {
      scenarios: Array<{ rounds: unknown[] }>;
    };
    const expectedFileCount = manifest.scenarios.reduce((total, scenario) => total + 1 + scenario.rounds.length, 0);
    expect(manifest.scenarios).toHaveLength(20);
    expect(first.files).toHaveLength(expectedFileCount);
    for (const file of first.files) {
      const firstBytes = await readFile(path.join(firstOutput, file.relpath));
      expect(firstBytes).toEqual(await readFile(path.join(secondOutput, file.relpath)));
      expect(createHash("sha256").update(firstBytes).digest("hex")).toBe(file.sha256);
    }
  });

  it("ships a mixed-format corpus where bounded repair improves coverage without selecting new regressions", async () => {
    const repoRoot = path.resolve(import.meta.dirname, "../../..");
    const outputDir = await mkdtemp(path.join(tmpdir(), "officeguard-corpus-contract-"));
    cleanup.push(outputDir);
    const summary = await runPublicAgentEval({
      manifestPath: path.join(repoRoot, "evals", "corpus", "manifest.json"),
      outputDir,
    });
    const noRepair = summary.strategies.find((entry) => entry.strategy === "no_repair")!;
    const fixed = summary.strategies.find((entry) => entry.strategy === "fixed_two_round")!;
    const bounded = summary.strategies.find((entry) => entry.strategy === "bounded_agent")!;

    expect(bounded.metrics.scenario_count).toBe(20);
    expect(new Set(bounded.scenarios.map((scenario) => scenario.document_format))).toEqual(new Set(["docx", "pptx"]));
    expect(bounded.metrics.release_coverage).toBeGreaterThan(noRepair.metrics.release_coverage);
    expect(bounded.metrics.new_regression_rate).toBe(0);
    expect(fixed.metrics.new_regression_rate).toBeGreaterThan(bounded.metrics.new_regression_rate);
    expect(bounded.metrics.average_repair_attempts).toBeLessThan(fixed.metrics.average_repair_attempts);
  });

  it("runs fixture generation and evaluation behind one command interface", async () => {
    const repoRoot = path.resolve(import.meta.dirname, "../../..");
    const outputDir = await mkdtemp(path.join(tmpdir(), "officeguard-eval-command-"));
    cleanup.push(outputDir);

    const result = await runPublicEvalCommand({ repoRoot, outputDir });

    expect(result.summary.model_calls).toBe(0);
    expect(result.summary.strategies[0]!.metrics.scenario_count).toBe(20);
    await expect(readFile(result.summaryPath, "utf8")).resolves.toContain('"eval_version": "1.0.0"');
    await expect(readFile(result.reportPath, "utf8")).resolves.toContain("model_calls=0");
    const fixtureIndexText = await readFile(result.fixtureIndexPath, "utf8");
    expect(fixtureIndexText).toContain('"license": "CC0-1.0"');
    const fixtureIndex = JSON.parse(fixtureIndexText) as { manifest_sha256: string };
    expect(result.summary.corpus_manifest_sha256).toBe(fixtureIndex.manifest_sha256);
  });

  it("rejects non-numeric frozen QA annotations instead of coercing them", async () => {
    const repoRoot = path.resolve(import.meta.dirname, "../../..");
    const outputDir = await mkdtemp(path.join(tmpdir(), "officeguard-eval-annotation-"));
    cleanup.push(outputDir);
    const manifest = JSON.parse(await readFile(path.join(repoRoot, "evals", "corpus", "manifest.json"), "utf8")) as {
      scenarios: Array<{ rounds: Array<{ blocking_count: unknown }> }>;
    };
    manifest.scenarios[0]!.rounds[0]!.blocking_count = "2";
    const manifestPath = path.join(outputDir, "manifest.json");
    await writeFile(manifestPath, JSON.stringify(manifest), "utf8");

    await expect(runPublicAgentEval({ manifestPath, outputDir }))
      .rejects.toThrow("blocking_count");
  });
});
