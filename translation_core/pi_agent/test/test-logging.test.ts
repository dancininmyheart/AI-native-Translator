import { rm } from "node:fs/promises";
import { afterEach, expect, it } from "vitest";
import { API_LOG_REDACT_PATHS, buildApi } from "../src/api.js";
import { FixturePythonToolBridge } from "../src/fixture-python-bridge.js";
import { AgentJobRunner } from "../src/job-runner.js";
import { NoopProviderLimiter } from "../src/provider-limiter.js";
import { InlineQueueAdapter } from "../src/queue.js";
import { InMemoryJobRegistry } from "../src/registry.js";
import { StorageService } from "../src/storage.js";
import { testConfig } from "./helpers.js";

const cleanup: string[] = [];

afterEach(async () => {
  await Promise.all(cleanup.splice(0).map((root) => rm(root, { recursive: true, force: true })));
});

it("keeps deterministic fixture API runs silent by default", async () => {
  const config = await testConfig();
  cleanup.push(config.dataRoot);
  const registry = new InMemoryJobRegistry();
  const storage = new StorageService(config);
  await storage.initialize();
  const runner = new AgentJobRunner(
    config,
    registry,
    new FixturePythonToolBridge(config),
    null,
    new NoopProviderLimiter(),
  );
  const queue = new InlineQueueAdapter((jobId) => runner.run(jobId));

  const app = await buildApi({ config, registry, storage, runner, queue });
  try {
    expect(app.log.info.name).toBe("noop");
    expect((await app.inject({ method: "GET", url: "/healthz" })).statusCode).toBe(200);
  } finally {
    await app.close();
  }
});

it("keeps production request logging and sensitive-field redaction enabled", async () => {
  const config = await testConfig({ fixtureMode: false, bearerToken: "production-token" });
  cleanup.push(config.dataRoot);
  const registry = new InMemoryJobRegistry();
  const storage = new StorageService(config);
  await storage.initialize();
  const runner = new AgentJobRunner(
    config,
    registry,
    new FixturePythonToolBridge(config),
    null,
    new NoopProviderLimiter(),
  );
  const queue = new InlineQueueAdapter((jobId) => runner.run(jobId));

  const app = await buildApi({ config, registry, storage, runner, queue });
  try {
    expect(app.log.info.name).not.toBe("noop");
    expect(app.log.level).toBe("info");
    expect(API_LOG_REDACT_PATHS).toEqual(["req.headers.authorization", "req.body"]);
  } finally {
    await app.close();
  }

  const silentApp = await buildApi({ config, registry, storage, runner, queue, logger: false });
  try {
    expect(silentApp.log.info.name).toBe("noop");
  } finally {
    await silentApp.close();
  }
});
