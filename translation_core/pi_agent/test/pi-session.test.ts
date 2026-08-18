import path from "node:path";
import { readFile, rm } from "node:fs/promises";
import { afterEach, expect, it } from "vitest";
import { Type } from "typebox";
import { defineTool } from "@earendil-works/pi-coding-agent";
import {
  buildForcedSkillPrompt,
  loadPiRetrySettings,
  PiSessionFactory,
} from "../src/pi-session.js";
import { testConfig } from "./helpers.js";

const cleanup: string[] = [];
afterEach(async () => Promise.all(cleanup.splice(0).map((value) => rm(value, { recursive: true, force: true }))));

it("uses resilient bounded retry defaults", () => {
  expect(loadPiRetrySettings({})).toEqual({
    enabled: true,
    maxRetries: 4,
    baseDelayMs: 10_000,
    provider: {
      timeoutMs: 120_000,
      maxRetries: 3,
      maxRetryDelayMs: 60_000,
    },
  });
});

it("requires a gate-backed terminal outcome instead of a textual early stop", () => {
  const prompt = buildForcedSkillPrompt("pi_agent_live", "PREFLIGHT");
  expect(prompt).toContain("A text response is not a terminal outcome");
  expect(prompt).toContain("do not repeat a durably completed effect");
  expect(prompt).toContain("QA and verification gates");
});

it("accepts retry environment overrides", () => {
  expect(loadPiRetrySettings({
    PI_AGENT_RETRY_MAX_RETRIES: "2",
    PI_AGENT_RETRY_BASE_DELAY_MS: "5000",
    PI_AGENT_PROVIDER_TIMEOUT_MS: "180000",
    PI_AGENT_PROVIDER_RETRY_MAX_RETRIES: "1",
    PI_AGENT_PROVIDER_MAX_RETRY_DELAY_MS: "0",
  })).toEqual({
    enabled: true,
    maxRetries: 2,
    baseDelayMs: 5_000,
    provider: {
      timeoutMs: 180_000,
      maxRetries: 1,
      maxRetryDelayMs: 0,
    },
  });
});

it.each([
  ["PI_AGENT_RETRY_MAX_RETRIES", "NaN"],
  ["PI_AGENT_RETRY_MAX_RETRIES", "1.5"],
  ["PI_AGENT_RETRY_MAX_RETRIES", "-1"],
  ["PI_AGENT_RETRY_BASE_DELAY_MS", "99"],
  ["PI_AGENT_PROVIDER_TIMEOUT_MS", "Infinity"],
  ["PI_AGENT_PROVIDER_TIMEOUT_MS", "3600001"],
  ["PI_AGENT_PROVIDER_RETRY_MAX_RETRIES", "11"],
  ["PI_AGENT_PROVIDER_MAX_RETRY_DELAY_MS", " 60000"],
])("rejects invalid integer retry setting %s=%s", (name, value) => {
  expect(() => loadPiRetrySettings({ [name]: value })).toThrow(`${name} must be an integer`);
});

it("injects only the controlled skill/tools and writes no API key to models.json", async () => {
  const previousKey = process.env.OPENAI_API_KEY;
  process.env.OPENAI_API_KEY = "test-secret-must-not-be-written";
  try {
    const config = await testConfig({
      fixtureMode: false,
      openaiBaseUrl: "https://models.example.test/v1/chat/completions",
      openaiModel: "test-model",
      piModelId: "test-model",
    });
    cleanup.push(config.dataRoot);
    const factory = new PiSessionFactory(config);
    await factory.probe();
    const statusTool = defineTool({
      name: "status_only",
      label: "Status",
      description: "Test status",
      parameters: Type.Object({}),
      execute: async () => ({ content: [{ type: "text", text: "ok" }], details: {} }),
    });
    const handle = await factory.create(path.join(config.dataRoot, "job"), [statusTool]);
    expect(handle.session.getActiveToolNames()).toEqual(["status_only"]);
    const skills = handle.session.resourceLoader.getSkills().skills;
    expect(skills.map((skill) => skill.name)).toEqual(["document-translation"]);
    const compiledSkill = await readFile(skills[0]!.filePath, "utf8");
    expect(compiledSkill).toContain("Bundled references (already read)");
    expect(compiledSkill).toContain("p0-p1-convergence.md");
    expect(compiledSkill).toContain("runtime-repair-policy.md");
    expect(compiledSkill).toContain("timing-and-resume.md");
    handle.dispose();
    const models = await readFile(path.join(config.piAgentDir, "models.json"), "utf8");
    expect(models).toContain("$OPENAI_API_KEY");
    expect(models).not.toContain("test-secret-must-not-be-written");
    expect(JSON.parse(models).providers[config.piModelProvider].baseUrl).toBe("https://models.example.test/v1");
  } finally {
    if (previousKey === undefined) delete process.env.OPENAI_API_KEY;
    else process.env.OPENAI_API_KEY = previousKey;
  }
});

it("adds the conventional v1 path for a provider origin", async () => {
  const previousKey = process.env.OPENAI_API_KEY;
  process.env.OPENAI_API_KEY = "test-secret";
  try {
    const config = await testConfig({
      fixtureMode: false,
      openaiBaseUrl: "https://models.example.test",
      openaiModel: "test-model",
      piModelId: "test-model",
    });
    cleanup.push(config.dataRoot);
    await new PiSessionFactory(config).probe();
    const models = JSON.parse(await readFile(path.join(config.piAgentDir, "models.json"), "utf8"));
    expect(models.providers[config.piModelProvider].baseUrl).toBe("https://models.example.test/v1");
  } finally {
    if (previousKey === undefined) delete process.env.OPENAI_API_KEY;
    else process.env.OPENAI_API_KEY = previousKey;
  }
});
