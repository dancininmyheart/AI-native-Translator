import path from "node:path";
import { resolveEvalRepositoryRoot, runPublicEvalCommand } from "./eval-command.js";

async function main(): Promise<void> {
  const repoRoot = resolveEvalRepositoryRoot();
  const outputDir = path.resolve(
    process.argv[2] ?? path.join(repoRoot, "translation_core", "tmp", "public-agent-eval"),
  );
  const result = await runPublicEvalCommand({ repoRoot, outputDir });
  process.stdout.write("OfficeGuard public Agent eval: offline policy replay, pi_session=false, model_calls=0\n");
  for (const { strategy, metrics } of result.summary.strategies) {
    process.stdout.write(
      `${strategy}: release_coverage=${percent(metrics.release_coverage)}, conditional_risk=${nullablePercent(metrics.conditional_risk)}, repair_uplift=${percent(metrics.repair_uplift_rate)}, new_regression_rate=${percent(metrics.new_regression_rate)}\n`,
    );
  }
  process.stdout.write(`Summary: ${result.summaryPath}\n`);
  process.stdout.write(`Report: ${result.reportPath}\n`);
  process.stdout.write(`Generated corpus index: ${result.fixtureIndexPath}\n`);
}

function percent(value: number): string {
  return `${(value * 100).toFixed(1)}%`;
}

function nullablePercent(value: number | null): string {
  return value === null ? "N/A" : percent(value);
}

main().catch((error: unknown) => {
  const message = error instanceof Error ? error.message : "Public Agent eval failed";
  process.stderr.write(`${message}\n`);
  process.exitCode = 1;
});
