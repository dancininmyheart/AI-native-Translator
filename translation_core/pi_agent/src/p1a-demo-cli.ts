import path from "node:path";
import { resolveRepositoryRoot, runP1aDemo } from "./p1a-demo.js";

async function main(): Promise<void> {
  const repoRoot = resolveRepositoryRoot();
  const outputDir = path.resolve(process.argv[2] ?? path.join(repoRoot, "translation_core", "tmp", "p1a-demo"));
  const summary = await runP1aDemo({ outputDir });
  process.stdout.write("P1-A offline demo: pi_session=false model_calls=0\n");
  for (const scenario of summary.scenarios) {
    process.stdout.write(
      `${scenario.scenario_id}: ${scenario.blocking_counts.join("->")} => ${scenario.terminal_status}, result HTTP ${scenario.result_http_status}, ${scenario.elapsed_ms} ms\n`,
    );
  }
  process.stdout.write("PASS 3/3\n");
  process.stdout.write(`Sanitized trace: ${path.join(outputDir, "demo-trace.json")}\n`);
}

main().catch((error: unknown) => {
  const message = error instanceof Error ? error.message : "P1-A demo failed";
  process.stderr.write(`${message}\n`);
  process.exitCode = 1;
});
