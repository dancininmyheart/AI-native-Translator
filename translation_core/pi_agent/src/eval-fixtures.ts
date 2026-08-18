import path from "node:path";
import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { readFile } from "node:fs/promises";

export interface PublicEvalFixtureIndex {
  schema_version: 1;
  corpus_id: string;
  corpus_version: string;
  license: "CC0-1.0";
  pi_session: false;
  model_calls: 0;
  native_office_qa: false;
  qa_annotations: "frozen_synthetic";
  office_artifacts: "runtime_generated_carriers";
  generated_by: "translation_core/tools/eval_public_corpus.py";
  manifest_sha256: string;
  files: Array<{ relpath: string; sha256: string }>;
}

export interface GeneratePublicEvalFixturesOptions {
  repoRoot: string;
  manifestPath: string;
  outputDir: string;
  pythonExecutable?: string;
}

export async function generatePublicEvalFixtures(
  options: GeneratePublicEvalFixturesOptions,
): Promise<PublicEvalFixtureIndex> {
  const pythonExecutable = options.pythonExecutable ?? defaultPythonExecutable(options.repoRoot);
  const script = path.join(options.repoRoot, "translation_core", "tools", "eval_public_corpus.py");
  await runProcess(pythonExecutable, [
    script,
    "--manifest",
    path.resolve(options.manifestPath),
    "--output-dir",
    path.resolve(options.outputDir),
  ], path.join(options.repoRoot, "translation_core"));
  const value = JSON.parse(await readFile(path.join(options.outputDir, "fixture-index.json"), "utf8")) as unknown;
  if (
    !isRecord(value)
    || value.schema_version !== 1
    || value.license !== "CC0-1.0"
    || value.pi_session !== false
    || value.model_calls !== 0
    || value.native_office_qa !== false
    || value.qa_annotations !== "frozen_synthetic"
    || value.office_artifacts !== "runtime_generated_carriers"
  ) {
    throw new Error("Generated public eval fixture index is invalid");
  }
  return value as unknown as PublicEvalFixtureIndex;
}

function defaultPythonExecutable(repoRoot: string): string {
  const virtualenv = process.platform === "win32"
    ? path.join(repoRoot, "translation_core", ".venv", "Scripts", "python.exe")
    : path.join(repoRoot, "translation_core", ".venv", "bin", "python");
  if (existsSync(virtualenv)) return virtualenv;
  return process.platform === "win32" ? "python" : "python3";
}

async function runProcess(executable: string, argv: string[], cwd: string): Promise<void> {
  const child = spawn(executable, argv, {
    cwd,
    shell: false,
    windowsHide: true,
    env: { ...process.env, PYTHONUNBUFFERED: "1" },
  });
  let stderr = "";
  child.stderr.on("data", (chunk: Buffer) => {
    stderr = `${stderr}${chunk.toString("utf8")}`.slice(-4_000);
  });
  const exitCode = await new Promise<number | null>((resolve, reject) => {
    child.once("error", reject);
    child.once("close", resolve);
  });
  if (exitCode !== 0) {
    throw new Error(`Public eval fixture generation failed (exit ${String(exitCode)}): ${stderr.trim()}`);
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}
