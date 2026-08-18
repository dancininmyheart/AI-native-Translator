import { Redis } from "ioredis";
import { buildApi } from "./api.js";
import { loadConfig } from "./config.js";
import { FixturePythonToolBridge } from "./fixture-python-bridge.js";
import { NoopActiveJobLimiter, RedisActiveJobLimiter } from "./job-limiter.js";
import { AgentJobRunner } from "./job-runner.js";
import { PiSessionFactory } from "./pi-session.js";
import { NoopProviderLimiter, RedisProviderLimiter } from "./provider-limiter.js";
import { PythonCliToolBridge } from "./python-tool-bridge.js";
import { BullQueueAdapter, InlineQueueAdapter, startBullWorker } from "./queue.js";
import { reconcileQueuedJobs } from "./reconcile-queued-jobs.js";
import { InMemoryJobRegistry, RedisJobRegistry } from "./registry.js";
import { probeDocumentRuntime } from "./runtime-readiness.js";
import { StorageService } from "./storage.js";

async function main(): Promise<void> {
  const config = loadConfig();
  const storage = new StorageService(config);
  await storage.initialize();

  if (config.fixtureMode) {
    const registry = new InMemoryJobRegistry();
    const limiter = new NoopProviderLimiter();
    const activeJobLimiter = new NoopActiveJobLimiter();
    const runner = new AgentJobRunner(
      config,
      registry,
      new FixturePythonToolBridge(config),
      null,
      limiter,
      activeJobLimiter,
    );
    const queue = new InlineQueueAdapter((jobId) => runner.run(jobId));
    const app = await buildApi({ config, registry, queue, runner, storage });
    await app.listen({ host: config.host, port: config.port });
    return;
  }

  const registry = new RedisJobRegistry(new Redis(config.redisUrl, { maxRetriesPerRequest: null }));
  const queue = new BullQueueAdapter(config);
  await reconcileQueuedJobs({ registry, storage, queue });
  const limiter = new RedisProviderLimiter(
    new Redis(config.redisUrl, { maxRetriesPerRequest: null }),
    config.providerMaxConcurrency,
    config.queueName,
  );
  const activeJobLimiter = new RedisActiveJobLimiter(
    new Redis(config.redisUrl, { maxRetriesPerRequest: null }),
    config.jobConcurrency,
    config.queueName,
  );
  const sessionFactory = new PiSessionFactory(config);
  await sessionFactory.probe();
  const pythonBridge = new PythonCliToolBridge(config);
  const runner = new AgentJobRunner(
    config,
    registry,
    pythonBridge,
    sessionFactory,
    limiter,
    activeJobLimiter,
  );
  const worker = startBullWorker(config, runner);
  const app = await buildApi({
    config,
    registry,
    queue,
    runner,
    storage,
    readiness: async () => {
      await queue.ping();
      await sessionFactory.probe();
      await storage.probeWritable();
      await probeDocumentRuntime(config);
      if (!worker.isRunning()) throw new Error("worker is not running");
    },
  });
  const shutdown = async () => {
    await app.close();
    await worker.close();
    await queue.close();
    await activeJobLimiter.close();
    await limiter.close();
    await registry.close();
  };
  process.once("SIGTERM", () => void shutdown());
  process.once("SIGINT", () => void shutdown());
  await app.listen({ host: config.host, port: config.port });
}

main().catch((error: unknown) => {
  const message = error instanceof Error ? error.message : "startup failed";
  process.stderr.write(`Pi Agent sidecar failed to start: ${message}\n`);
  process.exitCode = 1;
});
