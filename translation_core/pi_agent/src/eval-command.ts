import path from "node:path";
import { existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { generatePublicEvalFixtures } from "./eval-fixtures.js";
import { runPublicAgentEval, type PublicAgentEvalSummary } from "./eval-runner.js";

export interface RunPublicEvalCommandOptions {
  repoRoot: string;
  outputDir: string;
  pythonExecutable?: string;
}

export interface PublicEvalCommandResult {
  summary: PublicAgentEvalSummary;
  summaryPath: string;
  reportPath: string;
  fixtureIndexPath: string;
}

export async function runPublicEvalCommand(options: RunPublicEvalCommandOptions): Promise<PublicEvalCommandResult> {
  const repoRoot = path.resolve(options.repoRoot);
  const outputDir = path.resolve(options.outputDir);
  const manifestPath = path.join(repoRoot, "evals", "corpus", "manifest.json");
  const fixturesDir = path.join(outputDir, "corpus");
  await generatePublicEvalFixtures({
    repoRoot,
    manifestPath,
    outputDir: fixturesDir,
    ...(options.pythonExecutable ? { pythonExecutable: options.pythonExecutable } : {}),
  });
  const summary = await runPublicAgentEval({ manifestPath, outputDir });
  return {
    summary,
    summaryPath: path.join(outputDir, "eval-summary.json"),
    reportPath: path.join(outputDir, "eval-report.md"),
    fixtureIndexPath: path.join(fixturesDir, "fixture-index.json"),
  };
}

export function resolveEvalRepositoryRoot(start = path.dirname(fileURLToPath(import.meta.url))): string {
  let current = path.resolve(start);
  for (let depth = 0; depth < 8; depth += 1) {
    if (
      existsSync(path.join(current, "evals", "corpus", "manifest.json"))
      && existsSync(path.join(current, "translation_core", "pi_agent", "package.json"))
    ) return current;
    const parent = path.dirname(current);
    if (parent === current) break;
    current = parent;
  }
  throw new Error("Public Agent eval could not locate the repository root");
}
