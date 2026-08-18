import path from "node:path";
import { mkdtemp, mkdir, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import type { AppConfig } from "../src/config.js";

export async function testConfig(overrides: Partial<AppConfig> = {}): Promise<AppConfig> {
  const dataRoot = await mkdtemp(path.join(tmpdir(), "pi-agent-test-"));
  const skillDir = path.join(dataRoot, "skill");
  await mkdir(path.join(skillDir, "references"), { recursive: true });
  await writeFile(
    path.join(skillDir, "SKILL.md"),
    "---\nname: document-translation\ndescription: Controlled translation test skill.\n---\nUse only registered tools.\n",
    "utf8",
  );
  for (const filename of [
    "current-entrypoints.md",
    "execution-modes.md",
    "p0-p1-convergence.md",
    "runtime-repair-policy.md",
    "timing-and-resume.md",
  ]) {
    await writeFile(path.join(skillDir, "references", filename), `# ${filename}\n\nTrusted ${filename} policy.\n`, "utf8");
  }
  const config: AppConfig = {
    host: "127.0.0.1",
    port: 0,
    fixtureMode: true,
    dataRoot,
    jobsRoot: path.join(dataRoot, "jobs"),
    uploadsRoot: path.join(dataRoot, "uploads"),
    skillDir,
    redisUrl: "redis://127.0.0.1:6379/15",
    queueName: "test-queue",
    jobConcurrency: 20,
    rateLimitMax: 20,
    rateLimitDurationMs: 1000,
    translationConcurrency: 4,
    providerMaxConcurrency: 20,
    semanticQaEnabled: true,
    processingRepairEnabled: true,
    softTimeoutMs: 27 * 60 * 1000,
    hardTimeoutMs: 35 * 60 * 1000,
    defaultRepairRounds: 2,
    hardMaxRepairRounds: 3,
    maxInputBytes: 50 * 1024 * 1024,
    pythonExecutable: "python",
    pythonModule: "format_translator.agent_tool_cli",
    pythonToolTimeoutMs: 10000,
    sofficeExecutable: "soffice",
    piAgentDir: path.join(dataRoot, "pi-config"),
    piModelProvider: "translation-openai",
    piThinkingLevel: "off",
  };
  return { ...config, ...overrides };
}
