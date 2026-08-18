import { Queue, Worker } from "bullmq";
import { Redis } from "ioredis";
import type { AppConfig } from "./config.js";
import type { QueueAdapter, QueuePayload } from "./contracts.js";
import type { AgentJobRunner } from "./job-runner.js";

const RUNNABLE_JOB_STATES = new Set([
  "active",
  "delayed",
  "prioritized",
  "waiting",
  "waiting-children",
]);
const RETAINED_TERMINAL_JOB_STATES = new Set(["completed", "failed"]);

export interface RecoverableQueueJob {
  getState(): Promise<string>;
  remove(): Promise<void>;
}

export interface RecoverableQueueBackend {
  get(jobId: string): Promise<RecoverableQueueJob | null | undefined>;
  add(payload: QueuePayload): Promise<void>;
}

function connection(url: string): Redis {
  return new Redis(url, { maxRetriesPerRequest: null, enableReadyCheck: true });
}

export class BullQueueAdapter implements QueueAdapter {
  private readonly redis: Redis;
  private readonly queue: Queue<QueuePayload>;

  constructor(private readonly config: AppConfig) {
    this.redis = connection(config.redisUrl);
    this.queue = new Queue<QueuePayload>(config.queueName, { connection: this.redis });
  }

  async add(payload: QueuePayload): Promise<void> {
    await this.queue.add("translate", payload, {
      jobId: payload.jobId,
      attempts: 1,
      removeOnComplete: 1000,
      removeOnFail: 1000,
    });
  }

  async ensureRunnable(payload: QueuePayload): Promise<void> {
    await ensureRunnableQueueJob(
      {
        get: (jobId) => this.queue.getJob(jobId),
        add: (next) => this.add(next),
      },
      payload,
    );
  }

  async cancel(jobId: string): Promise<boolean> {
    const job = await this.queue.getJob(jobId);
    if (!job) return false;
    try {
      await job.remove();
      return true;
    } catch {
      return false;
    }
  }

  async ping(): Promise<void> {
    await this.redis.ping();
  }

  async close(): Promise<void> {
    await this.queue.close();
    await this.redis.quit();
  }
}

export class InlineQueueAdapter implements QueueAdapter {
  constructor(private readonly handler: (jobId: string) => Promise<void>) {}

  async add(payload: QueuePayload): Promise<void> {
    queueMicrotask(() => void this.handler(payload.jobId));
  }

  async ensureRunnable(payload: QueuePayload): Promise<void> {
    await this.add(payload);
  }

  async cancel(): Promise<boolean> {
    return false;
  }

  async close(): Promise<void> {}
}

export function startBullWorker(config: AppConfig, runner: AgentJobRunner): Worker<QueuePayload> {
  return new Worker<QueuePayload>(config.queueName, async (job) => runner.run(job.data.jobId), {
    connection: connection(config.redisUrl),
    concurrency: config.jobConcurrency,
    limiter: { max: config.rateLimitMax, duration: config.rateLimitDurationMs },
  });
}

export async function ensureRunnableQueueJob(
  backend: RecoverableQueueBackend,
  payload: QueuePayload,
): Promise<void> {
  for (let attempt = 0; attempt < 5; attempt += 1) {
    let existing = await backend.get(payload.jobId);
    if (!existing) {
      await backend.add(payload);
      existing = await backend.get(payload.jobId);
      if (!existing) continue;
    }
    const state = await existing.getState();
    if (RUNNABLE_JOB_STATES.has(state)) return;
    if (state === "unknown") continue;
    if (!RETAINED_TERMINAL_JOB_STATES.has(state)) {
      throw new Error(`BullMQ job is not recoverable from state: ${state}`);
    }
    try {
      await existing.remove();
    } catch {
      // Another replica may have removed/re-enqueued the same deterministic ID.
      // Re-read state before deciding whether recovery failed.
      continue;
    }
    await backend.add(payload);
    const replacement = await backend.get(payload.jobId);
    if (replacement && RUNNABLE_JOB_STATES.has(await replacement.getState())) return;
  }
  throw new Error("BullMQ job state changed repeatedly during queued-job recovery");
}
