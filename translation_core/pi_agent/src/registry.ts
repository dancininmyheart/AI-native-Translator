import {
  TERMINAL_JOB_STATUSES,
  type JobRegistry,
  type SourceRecord,
  type TranslationJobRecord,
} from "./contracts.js";
import { Redis } from "ioredis";

export class InMemoryJobRegistry implements JobRegistry {
  private readonly sources = new Map<string, SourceRecord>();
  private readonly jobs = new Map<string, TranslationJobRecord>();
  private readonly idempotency = new Map<string, string>();

  async putSource(source: SourceRecord): Promise<void> {
    this.sources.set(source.id, structuredClone(source));
  }

  async getSource(id: string): Promise<SourceRecord | null> {
    const source = this.sources.get(id);
    return source ? structuredClone(source) : null;
  }

  async createJob(job: TranslationJobRecord, idempotencyKey: string): Promise<TranslationJobRecord> {
    const existingId = this.idempotency.get(idempotencyKey);
    if (existingId) return structuredClone(this.jobs.get(existingId)!);
    this.idempotency.set(idempotencyKey, job.id);
    this.jobs.set(job.id, structuredClone(job));
    return structuredClone(job);
  }

  async getJob(id: string): Promise<TranslationJobRecord | null> {
    const job = this.jobs.get(id);
    return job ? structuredClone(job) : null;
  }

  async listQueuedJobs(): Promise<TranslationJobRecord[]> {
    return [...this.jobs.values()]
      .filter((job) => job.status === "QUEUED")
      .map((job) => structuredClone(job));
  }

  async updateJob(id: string, patch: Partial<TranslationJobRecord>): Promise<TranslationJobRecord> {
    const current = this.jobs.get(id);
    if (!current) throw new Error("job not found");
    assertTerminalStateIsNotReopened(current, patch);
    const updated = { ...current, ...structuredClone(patch), id: current.id };
    this.jobs.set(id, updated);
    return structuredClone(updated);
  }

  async findByIdempotencyKey(key: string): Promise<TranslationJobRecord | null> {
    const id = this.idempotency.get(key);
    return id ? this.getJob(id) : null;
  }

  async close(): Promise<void> {}
}

export class RedisJobRegistry implements JobRegistry {
  constructor(
    private readonly redis: Redis,
    private readonly prefix = "format-translator",
  ) {}

  private sourceKey(id: string): string {
    return `${this.prefix}:source:${id}`;
  }

  private jobKey(id: string): string {
    return `${this.prefix}:job:${id}`;
  }

  private idemKey(key: string): string {
    return `${this.prefix}:idem:${key}`;
  }

  async putSource(source: SourceRecord): Promise<void> {
    await this.redis.set(this.sourceKey(source.id), JSON.stringify(source));
  }

  async getSource(id: string): Promise<SourceRecord | null> {
    const raw = await this.redis.get(this.sourceKey(id));
    return raw ? (JSON.parse(raw) as SourceRecord) : null;
  }

  async createJob(job: TranslationJobRecord, idempotencyKey: string): Promise<TranslationJobRecord> {
    const script = `
      local existing = redis.call('GET', KEYS[1])
      if existing then return existing end
      redis.call('SET', KEYS[1], ARGV[1])
      redis.call('SET', KEYS[2], ARGV[2])
      return ARGV[1]
    `;
    const selectedId = (await this.redis.eval(
      script,
      2,
      this.idemKey(idempotencyKey),
      this.jobKey(job.id),
      job.id,
      JSON.stringify(job),
    )) as string;
    const selected = await this.getJob(selectedId);
    if (!selected) throw new Error("idempotent job record is missing");
    return selected;
  }

  async getJob(id: string): Promise<TranslationJobRecord | null> {
    const raw = await this.redis.get(this.jobKey(id));
    return raw ? (JSON.parse(raw) as TranslationJobRecord) : null;
  }

  async listQueuedJobs(): Promise<TranslationJobRecord[]> {
    const queued: TranslationJobRecord[] = [];
    let cursor = "0";
    do {
      const [nextCursor, keys] = await this.redis.scan(cursor, "MATCH", this.jobKey("*"), "COUNT", 100);
      cursor = nextCursor;
      if (keys.length === 0) continue;
      const records = await this.redis.mget(...keys);
      for (const raw of records) {
        if (!raw) continue;
        const job = JSON.parse(raw) as TranslationJobRecord;
        if (job.status === "QUEUED") queued.push(job);
      }
    } while (cursor !== "0");
    return queued;
  }

  async updateJob(id: string, patch: Partial<TranslationJobRecord>): Promise<TranslationJobRecord> {
    const isolated = this.redis.duplicate();
    try {
      for (let attempt = 0; attempt < 5; attempt += 1) {
        await isolated.watch(this.jobKey(id));
        const raw = await isolated.get(this.jobKey(id));
        if (!raw) {
          await isolated.unwatch();
          throw new Error("job not found");
        }
        const current = JSON.parse(raw) as TranslationJobRecord;
        assertTerminalStateIsNotReopened(current, patch);
        const updated = { ...current, ...patch, id: current.id };
        const result = await isolated.multi().set(this.jobKey(id), JSON.stringify(updated)).exec();
        if (result !== null) return updated;
      }
      throw new Error("job update conflicted repeatedly");
    } finally {
      await isolated.quit();
    }
  }

  async findByIdempotencyKey(key: string): Promise<TranslationJobRecord | null> {
    const id = await this.redis.get(this.idemKey(key));
    return id ? this.getJob(id) : null;
  }

  async close(): Promise<void> {
    await this.redis.quit();
  }
}

function assertTerminalStateIsNotReopened(
  current: TranslationJobRecord,
  patch: Partial<TranslationJobRecord>,
): void {
  if (TERMINAL_JOB_STATUSES.has(current.status) && Object.keys(patch).length > 0) {
    throw new Error(`terminal job state is immutable: ${current.status}`);
  }
}
