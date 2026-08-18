import { randomUUID } from "node:crypto";
import path from "node:path";
import { readFile, rm } from "node:fs/promises";
import { Readable } from "node:stream";
import { afterEach, describe, expect, it } from "vitest";
import { Queue } from "bullmq";
import { Redis } from "ioredis";
import type { PythonToolName, QueuePayload, TranslationJobRecord } from "../src/contracts.js";
import { FixturePythonToolBridge } from "../src/fixture-python-bridge.js";
import { AgentJobRunner } from "../src/job-runner.js";
import { NoopProviderLimiter } from "../src/provider-limiter.js";
import type { PythonToolBridge } from "../src/python-tool-bridge.js";
import { BullQueueAdapter, startBullWorker } from "../src/queue.js";
import { reconcileQueuedJobs } from "../src/reconcile-queued-jobs.js";
import { RedisJobRegistry } from "../src/registry.js";
import { StorageService } from "../src/storage.js";
import { testConfig } from "./helpers.js";

const redisIntegrationUrl = process.env.REDIS_INTEGRATION_URL;
const integration = describe.skipIf(!redisIntegrationUrl);
const cleanup: Array<() => Promise<void>> = [];

afterEach(async () => {
  const errors: unknown[] = [];
  for (const release of cleanup.splice(0).reverse()) {
    try {
      await release();
    } catch (error) {
      errors.push(error);
    }
  }
  if (errors.length > 0) throw new AggregateError(errors, "Redis integration cleanup failed");
});

integration("Redis registry and BullMQ reliability", () => {
  it("atomically selects one business job for concurrent uses of one idempotency key", async () => {
    const prefix = integrationName("idempotency");
    deferRedisKeyCleanup(`${prefix}:*`);
    const registries = Array.from({ length: 12 }, () => trackedRegistry(prefix));
    const candidates = registries.map((_registry, index) => queuedJob(`candidate-${index}-${randomUUID()}`));

    const selected = await Promise.all(
      registries.map((registry, index) => registry.createJob(candidates[index]!, "same-request")),
    );

    const selectedIds = new Set(selected.map((job) => job.id));
    expect(selectedIds.size).toBe(1);
    await expect(registries[0]!.findByIdempotencyKey("same-request")).resolves.toMatchObject({
      id: selected[0]!.id,
      status: "QUEUED",
    });
    const persistedCandidates = await Promise.all(
      candidates.map((candidate) => registries[0]!.getJob(candidate.id)),
    );
    expect(persistedCandidates.filter((candidate) => candidate !== null)).toHaveLength(1);
    await expect(registries[0]!.listQueuedJobs()).resolves.toHaveLength(1);
  });

  it("does not let a stale replica reopen a terminal business job", async () => {
    const prefix = integrationName("terminal");
    deferRedisKeyCleanup(`${prefix}:*`);
    const finishingReplica = trackedRegistry(prefix);
    const staleReplica = trackedRegistry(prefix);
    const job = queuedJob(`terminal-${randomUUID()}`);
    await finishingReplica.createJob(job, "terminal-request");
    const completedAt = new Date().toISOString();

    await finishingReplica.updateJob(job.id, {
      status: "COMPLETED",
      resultRelpath: "result/translated.docx",
      updatedAt: completedAt,
      completedAt,
    });

    await expect(
      staleReplica.updateJob(job.id, { status: "QA", updatedAt: new Date().toISOString() }),
    ).rejects.toThrow(/terminal job state is immutable: COMPLETED/);
    await expect(staleReplica.getJob(job.id)).resolves.toMatchObject({
      status: "COMPLETED",
      resultRelpath: "result/translated.docx",
      completedAt,
    });
  });

  it("reconciles one BullMQ job and keeps terminal publication effectively-once on redelivery", async () => {
    const prefix = integrationName("reconciliation");
    const queueName = integrationName("queue");
    deferRedisKeyCleanup(`${prefix}:*`, `bull:${queueName}:*`);
    const config = await testConfig({
      fixtureMode: true,
      redisUrl: requiredRedisUrl(),
      queueName,
      jobConcurrency: 2,
    });
    cleanup.push(() => rm(config.dataRoot, { recursive: true, force: true }));
    const storage = new StorageService(config);
    await storage.initialize();
    const registry = trackedRegistry(prefix);
    const source = await storage.ingest("source.docx", Readable.from(["durable-source"]));
    await registry.putSource(source);
    const job = { ...queuedJob(`reconciled-${randomUUID()}`), sourceId: source.id };
    await registry.createJob(job, "reconciled-request");
    const adapter = new BullQueueAdapter(config);
    cleanup.push(() => adapter.close());
    const inspectorRedis = new Redis(requiredRedisUrl(), {
      enableReadyCheck: true,
      maxRetriesPerRequest: null,
    });
    inspectorRedis.on("error", () => undefined);
    cleanup.push(() => closeRedis(inspectorRedis));
    const inspector = new Queue<QueuePayload>(queueName, { connection: inspectorRedis });
    cleanup.push(() => inspector.close());

    const reconciliations = await Promise.all([
      reconcileQueuedJobs({ registry, storage, queue: adapter }),
      reconcileQueuedJobs({ registry, storage, queue: adapter }),
    ]);

    expect(reconciliations).toEqual([
      { inspected: 1, enqueued: 1, failed: 0 },
      { inspected: 1, enqueued: 1, failed: 0 },
    ]);
    await expect(inspector.getWaiting()).resolves.toHaveLength(1);
    await expect(inspector.getJob(job.id).then((queued) => queued?.getState())).resolves.toBe("waiting");

    const toolCalls: PythonToolName[] = [];
    const fixtureBridge = new FixturePythonToolBridge(config);
    const bridge: PythonToolBridge = {
      execute: async (tool, jobId, payload, signal) => {
        toolCalls.push(tool);
        return fixtureBridge.execute(tool, jobId, payload, signal);
      },
    };
    const runner = new AgentJobRunner(
      config,
      registry,
      bridge,
      null,
      new NoopProviderLimiter(),
    );
    const worker = startBullWorker(config, runner);
    cleanup.push(async () => {
      await worker.close(true);
    });
    await worker.waitUntilReady();

    const completed = await waitForJobStatus(registry, job.id, "COMPLETED");
    const firstToolCalls = [...toolCalls];
    expect(firstToolCalls).toEqual([
      "preflight_document",
      "translate_once",
      "run_document_qa",
      "verify_candidate",
      "finalize_result",
    ]);
    expect(completed.resultRelpath).toBe("result/translated.docx");
    expect(
      await readFile(path.join(config.jobsRoot, job.id, completed.resultRelpath!), "utf8"),
    ).toBe("durable-source");
    await waitForBullState(inspector, job.id, "completed");

    // BullMQ is at-least-once. Force a retained-job replay and verify that the
    // durable terminal guard makes publication/state effects effectively-once.
    await worker.pause();
    await adapter.ensureRunnable({ jobId: job.id });
    await expect(inspector.getJob(job.id).then((queued) => queued?.getState())).resolves.toBe("waiting");
    await worker.resume();
    await waitForBullState(inspector, job.id, "completed");

    expect(toolCalls).toEqual(firstToolCalls);
    await expect(registry.getJob(job.id)).resolves.toEqual(completed);
  }, 20_000);

  it("recovers the durable job after Redis drops the registry connection", async () => {
    const prefix = integrationName("disconnect");
    deferRedisKeyCleanup(`${prefix}:*`);
    const redis = new Redis(requiredRedisUrl(), {
      enableReadyCheck: true,
      maxRetriesPerRequest: null,
    });
    redis.on("error", () => undefined);
    cleanup.push(() => closeRedis(redis));
    const registry = new RedisJobRegistry(redis, prefix);
    const job = queuedJob(`disconnect-${randomUUID()}`);
    await registry.createJob(job, "disconnect-request");
    const connectionId = await redis.call("CLIENT", "ID");
    const admin = trackedRedis();
    cleanup.push(() => closeRedis(admin));
    const recovered = waitForNextRedisReady(redis);

    await expect(admin.call("CLIENT", "KILL", "ID", String(connectionId))).resolves.toBe(1);
    await recovered;

    await expect(registry.getJob(job.id)).resolves.toEqual(job);
  });
});

function integrationName(label: string): string {
  return `format-translator-it-${label}-${randomUUID()}`;
}

function requiredRedisUrl(): string {
  if (!redisIntegrationUrl) throw new Error("REDIS_INTEGRATION_URL is required");
  return redisIntegrationUrl;
}

function trackedRegistry(prefix: string): RedisJobRegistry {
  const registry = new RedisJobRegistry(trackedRedis(), prefix);
  cleanup.push(() => registry.close());
  return registry;
}

function trackedRedis(): Redis {
  if (!redisIntegrationUrl) throw new Error("REDIS_INTEGRATION_URL is required");
  const redis = new Redis(redisIntegrationUrl, {
    enableReadyCheck: true,
    maxRetriesPerRequest: 1,
  });
  redis.on("error", () => undefined);
  return redis;
}

function deferRedisKeyCleanup(...patterns: string[]): void {
  cleanup.push(async () => {
    const redis = trackedRedis();
    try {
      for (const pattern of patterns) await unlinkMatching(redis, pattern);
    } finally {
      await closeRedis(redis);
    }
  });
}

async function unlinkMatching(redis: Redis, pattern: string): Promise<void> {
  let cursor = "0";
  do {
    const [nextCursor, keys] = await redis.scan(cursor, "MATCH", pattern, "COUNT", 100);
    cursor = nextCursor;
    if (keys.length > 0) await redis.unlink(...keys);
  } while (cursor !== "0");
}

async function closeRedis(redis: Redis): Promise<void> {
  if (redis.status === "end") return;
  if (redis.status === "wait") {
    redis.disconnect();
    return;
  }
  try {
    await redis.quit();
  } catch {
    redis.disconnect();
  }
}

function queuedJob(id: string): TranslationJobRecord {
  const now = new Date();
  return {
    id,
    sourceId: `source-${id}`,
    executionMode: "pi_agent_live",
    sourceFilename: "source.docx",
    inputRelpath: "input/source.docx",
    sourceLanguage: "en",
    targetLanguage: "zh",
    qualityProfile: "standard",
    maxRepairRounds: 2,
    repairRound: 0,
    status: "QUEUED",
    softDeadlineAt: new Date(now.getTime() + 60_000).toISOString(),
    hardDeadlineAt: new Date(now.getTime() + 120_000).toISOString(),
    createdAt: now.toISOString(),
    updatedAt: now.toISOString(),
  };
}

async function waitForJobStatus(
  registry: RedisJobRegistry,
  jobId: string,
  expected: TranslationJobRecord["status"],
  timeoutMs = 10_000,
): Promise<TranslationJobRecord> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const job = await registry.getJob(jobId);
    if (job?.status === expected) return job;
    await delay(25);
  }
  const job = await registry.getJob(jobId);
  throw new Error(`job ${jobId} did not reach ${expected}; last status was ${job?.status ?? "missing"}`);
}

async function waitForBullState(
  queue: Queue<QueuePayload>,
  jobId: string,
  expected: string,
  timeoutMs = 10_000,
): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const job = await queue.getJob(jobId);
    if (job && await job.getState() === expected) return;
    await delay(25);
  }
  const job = await queue.getJob(jobId);
  throw new Error(`BullMQ job ${jobId} did not reach ${expected}; last state was ${await job?.getState() ?? "missing"}`);
}

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function waitForNextRedisReady(redis: Redis, timeoutMs = 5_000): Promise<void> {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      redis.removeListener("ready", onReady);
      reject(new Error(`Redis client did not reconnect; last status was ${redis.status}`));
    }, timeoutMs);
    timer.unref();
    const onReady = () => {
      clearTimeout(timer);
      resolve();
    };
    redis.once("ready", onReady);
  });
}
