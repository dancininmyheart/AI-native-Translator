import { spawn } from "node:child_process";
import type { AppConfig } from "./config.js";
import { pythonEnvironment } from "./python-tool-bridge.js";

export async function probeDocumentRuntime(config: AppConfig): Promise<void> {
  await runProbe(
    config.pythonExecutable,
    ["-m", config.pythonModule, "--help"],
    config.jobsRoot,
    pythonEnvironment(),
    10_000,
    "Python agent tool CLI",
  );
  await runProbe(
    config.sofficeExecutable,
    ["--version"],
    config.jobsRoot,
    pythonEnvironment(),
    10_000,
    "LibreOffice",
  );
}

async function runProbe(
  executable: string,
  argv: string[],
  cwd: string,
  env: NodeJS.ProcessEnv,
  timeoutMs: number,
  label: string,
): Promise<void> {
  await new Promise<void>((resolve, reject) => {
    let settled = false;
    let timedOut = false;
    const finish = (error?: Error) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      if (error) reject(error);
      else resolve();
    };
    const child = spawn(executable, argv, {
      cwd,
      env,
      shell: false,
      windowsHide: true,
      stdio: "ignore",
    });
    const timer = setTimeout(() => {
      timedOut = true;
      child.kill();
    }, timeoutMs);
    child.once("error", () => finish(new Error(`${label} is unavailable`)));
    child.once("close", (code) => {
      if (timedOut) finish(new Error(`${label} readiness probe timed out`));
      else if (code === 0) finish();
      else finish(new Error(`${label} readiness probe failed`));
    });
  });
}
