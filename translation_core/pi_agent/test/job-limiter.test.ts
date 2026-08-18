import path from "node:path";
import { mkdir, rm, writeFile } from "node:fs/promises";
import type { AgentSession } from "@earendil-works/pi-coding-agent";
import type { Redis } from "ioredis";
import { afterEach, expect, it, vi } from "vitest";
import type { TranslationJobRecord } from "../src/contracts.js";
import { FixturePythonToolBridge } from "../src/fixture-python-bridge.js";
import {
  NoopActiveJobLimiter,
  RedisActiveJobLimiter,
  type ActiveJobLease,
  type ActiveJobLimiter,
} from "../src/job-limiter.js";
import { AgentJobRunner } from "../src/job-runner.js";
import type { SessionFactory } from "../src/pi-session.js";
import { NoopProviderLimiter } from "../src/provider-limiter.js";
import type { PythonToolBridge } from "../src/python-tool-bridge.js";
import { InMemoryJobRegistry } from "../src/registry.js";
import { testConfig } from "./helpers.js";

const cleanup: string[] = [];
afterEach(async () => Promise.all(cleanup.splice(0).map((value) => rm(value, { recursive: true, force: true }))));

it("acquires one deployment lease for the complete fixture execution and releases it", async () => {
  const config = await testConfig();
  cleanup.push(config.dataRoot);
  const registry = new InMemoryJobRegistry();
  const job = queuedJob("lease-success");
  await registry.createJob(job, "lease-success-idempotency");
  const input = path.join(config.jobsRoot, job.id, job.inputRelpath);
  await mkdir(path.dirname(input), { recursive: true });
  await writeFile(input, "fixture-docx", "utf8");

  const release = vi.fn(async () => undefined);
  const acquire = vi.fn(async (_jobId: string, _signal?: AbortSignal): Promise<ActiveJobLease> => ({
    lostSignal: new AbortController().signal,
    release,
  }));
  const activeJobLimiter: ActiveJobLimiter = { acquire, close: async () => undefined };
  const runner = new AgentJobRunner(
    config,
    registry,
    new FixturePythonToolBridge(config),
    null,
    new NoopProviderLimiter(),
    activeJobLimiter,
  );

  await runner.run(job.id);

  expect((await registry.getJob(job.id))?.status).toBe("COMPLETED");
  expect(acquire).toHaveBeenCalledTimes(1);
  expect(acquire.mock.calls[0]?.[0]).toBe(job.id);
  expect(release).toHaveBeenCalledTimes(1);
});

it("releases the deployment lease when execution fails", async () => {
  const config = await testConfig({ fixtureMode: false });
  cleanup.push(config.dataRoot);
  const registry = new InMemoryJobRegistry();
  const job = queuedJob("lease-failure");
  await registry.createJob(job, "lease-failure-idempotency");
  const release = vi.fn(async () => undefined);
  const activeJobLimiter: ActiveJobLimiter = {
    acquire: async () => ({ lostSignal: new AbortController().signal, release }),
    close: async () => undefined,
  };
  const sessionFactory: SessionFactory = {
    probe: async () => undefined,
    create: async () => { throw new Error("session setup failed"); },
  };
  const bridge: PythonToolBridge = {
    execute: async () => { throw new Error("not used"); },
  };
  const runner = new AgentJobRunner(
    config,
    registry,
    bridge,
    sessionFactory,
    new NoopProviderLimiter(),
    activeJobLimiter,
  );

  await runner.run(job.id);

  expect((await registry.getJob(job.id))?.status).toBe("FAILED");
  expect(release).toHaveBeenCalledTimes(1);
});

it("durably cancels while waiting and releases a lease returned by an acquisition race", async () => {
  const config = await testConfig({ fixtureMode: false });
  cleanup.push(config.dataRoot);
  const registry = new InMemoryJobRegistry();
  const job = queuedJob("lease-wait-cancel");
  await registry.createJob(job, "lease-wait-cancel-idempotency");
  const release = vi.fn(async () => undefined);
  let acquisitionStarted!: () => void;
  const started = new Promise<void>((resolve) => { acquisitionStarted = resolve; });
  const activeJobLimiter: ActiveJobLimiter = {
    acquire: async (_jobId, signal) => {
      acquisitionStarted();
      await new Promise<void>((resolve) => {
        if (signal?.aborted) resolve();
        else signal?.addEventListener("abort", () => resolve(), { once: true });
      });
      // Exercise the runner's post-acquire cancellation check even if an
      // implementation reports a lease at the same instant cancellation wins.
      return { lostSignal: new AbortController().signal, release };
    },
    close: async () => undefined,
  };
  const bridge: PythonToolBridge = {
    execute: async () => { throw new Error("not used"); },
  };
  const runner = new AgentJobRunner(
    config,
    registry,
    bridge,
    null,
    new NoopProviderLimiter(),
    activeJobLimiter,
  );

  const running = runner.run(job.id);
  await started;
  expect(await runner.cancel(job.id)).toBe(true);
  await running;

  const cancelled = await registry.getJob(job.id);
  expect(cancelled?.cancellationRequestedAt).toBeDefined();
  expect(cancelled?.status).toBe("CANCELLED");
  expect(release).toHaveBeenCalledTimes(1);
});

it("fails closed and releases capacity when renewal reports a lost lease", async () => {
  const config = await testConfig({ fixtureMode: false });
  cleanup.push(config.dataRoot);
  const registry = new InMemoryJobRegistry();
  const job = queuedJob("lease-lost");
  await registry.createJob(job, "lease-lost-idempotency");
  const lost = new AbortController();
  const release = vi.fn(async () => undefined);
  const activeJobLimiter: ActiveJobLimiter = {
    acquire: async () => ({ lostSignal: lost.signal, release }),
    close: async () => undefined,
  };
  let promptStarted!: () => void;
  const started = new Promise<void>((resolve) => { promptStarted = resolve; });
  let finishPrompt!: () => void;
  const promptFinished = new Promise<void>((resolve) => { finishPrompt = resolve; });
  const sessionFactory: SessionFactory = {
    probe: async () => undefined,
    create: async () => {
      const session = {
        messages: [],
        subscribe: () => () => undefined,
        prompt: async () => {
          promptStarted();
          await promptFinished;
        },
        waitForIdle: async () => undefined,
        abort: async () => finishPrompt(),
        dispose: () => undefined,
      } as unknown as AgentSession;
      return { session, dispose: () => undefined };
    },
  };
  const bridge: PythonToolBridge = {
    execute: async () => { throw new Error("not used"); },
  };
  const runner = new AgentJobRunner(
    config,
    registry,
    bridge,
    sessionFactory,
    new NoopProviderLimiter(),
    activeJobLimiter,
  );

  const running = runner.run(job.id);
  await started;
  lost.abort();
  await running;

  const result = await registry.getJob(job.id);
  expect(result?.status).toBe("NEEDS_REVIEW");
  expect(result?.errorCode).toBe("active_job_lease_lost");
  expect(release).toHaveBeenCalledTimes(1);
});

it("hard-caps the Redis semaphore at 20 and keeps fixture limiting inert", async () => {
  const unusedRedis = {} as Redis;
  expect(() => new RedisActiveJobLimiter(unusedRedis, 21, "test-queue")).toThrow(/\[1, 20\]/);
  expect(() => new RedisActiveJobLimiter(unusedRedis, 20, "test-queue")).not.toThrow();

  const noopLimiter: ActiveJobLimiter = new NoopActiveJobLimiter();
  const lease = await noopLimiter.acquire("fixture-job");
  expect(lease.lostSignal.aborted).toBe(false);
  await lease.release();
  await lease.release();
});

it("isolates no-op lease loss signals across concurrent jobs", async () => {
  const limiter = new NoopActiveJobLimiter();
  const leases = await Promise.all(
    Array.from({ length: 20 }, (_, index) => limiter.acquire(`fixture-job-${index}`)),
  );

  expect(new Set(leases.map((lease) => lease.lostSignal))).toHaveLength(leases.length);
  expect(leases.every((lease) => !lease.lostSignal.aborted)).toBe(true);

  await Promise.all(leases.map((lease) => lease.release()));
});

it("uses the job id as a distributed single-flight key", async () => {
  const fakeRedis = new FakeLeaseRedis();
  const firstLimiter = new RedisActiveJobLimiter(fakeRedis as unknown as Redis, 20, "shared", 3_000, 10);
  const secondLimiter = new RedisActiveJobLimiter(fakeRedis as unknown as Redis, 20, "shared", 3_000, 10);
  const first = await firstLimiter.acquire("same-job");
  const waitingAbort = new AbortController();
  let secondSettled = false;
  const waiting = secondLimiter.acquire("same-job", waitingAbort.signal).finally(() => {
    secondSettled = true;
  });

  await new Promise((resolve) => setTimeout(resolve, 30));
  expect(secondSettled).toBe(false);
  expect(fakeRedis.activeCount).toBe(1);
  waitingAbort.abort();
  await expect(waiting).rejects.toThrow(/aborted/);

  await first.release();
  const next = await secondLimiter.acquire("same-job");
  expect(fakeRedis.activeCount).toBe(1);
  await next.release();
  expect(fakeRedis.activeCount).toBe(0);
});

function queuedJob(id: string): TranslationJobRecord {
  const now = new Date();
  return {
    id,
    sourceId: "source",
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

class FakeLeaseRedis {
  private readonly owners = new Map<string, string>();

  get activeCount(): number {
    return this.owners.size;
  }

  async eval(script: string, _keyCount: number, ...args: Array<string | number>): Promise<number> {
    if (script.includes("ZCARD")) {
      const maximum = Number(args[2]);
      const jobId = String(args[4]);
      const ownerToken = String(args[5]);
      if (this.owners.has(jobId) || this.owners.size >= maximum) return 0;
      this.owners.set(jobId, ownerToken);
      return 1;
    }
    if (script.includes("HGET") && !script.includes("TIME")) {
      const jobId = String(args[2]);
      const ownerToken = String(args[3]);
      if (this.owners.get(jobId) !== ownerToken) return 0;
      this.owners.delete(jobId);
      return 1;
    }
    if (script.includes("HGET") && script.includes("TIME")) {
      const jobId = String(args[3]);
      const ownerToken = String(args[4]);
      return this.owners.get(jobId) === ownerToken ? 1 : 0;
    }
    throw new Error("unexpected Redis script");
  }
}
