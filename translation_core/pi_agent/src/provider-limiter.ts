import { randomUUID } from "node:crypto";
import { Redis } from "ioredis";

export interface PermitLease {
  release(): Promise<void>;
}

export interface ProviderLimiter {
  acquire(count: number, signal?: AbortSignal): Promise<PermitLease>;
  close(): Promise<void>;
}

export class NoopProviderLimiter implements ProviderLimiter {
  async acquire(): Promise<PermitLease> {
    return { release: async () => undefined };
  }
  async close(): Promise<void> {}
}

export class RedisProviderLimiter implements ProviderLimiter {
  private readonly key: string;

  constructor(
    private readonly redis: Redis,
    private readonly maximum: number,
    queueName: string,
    private readonly leaseMs = 120_000,
  ) {
    this.key = `${queueName}:provider-permits`;
  }

  async acquire(count: number, signal?: AbortSignal): Promise<PermitLease> {
    if (!Number.isInteger(count) || count < 1 || count > this.maximum) {
      throw new Error(`provider permit count must be in [1, ${this.maximum}]`);
    }
    const leaseId = randomUUID();
    const members = Array.from({ length: count }, (_, index) => `${leaseId}:${index}`);
    const acquireScript = `
      redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[1])
      local current = redis.call('ZCARD', KEYS[1])
      local requested = tonumber(ARGV[2])
      local maximum = tonumber(ARGV[3])
      if current + requested > maximum then return 0 end
      for i = 5, #ARGV do redis.call('ZADD', KEYS[1], ARGV[1] + ARGV[4], ARGV[i]) end
      return 1
    `;
    while (true) {
      if (signal?.aborted) throw new Error("provider permit wait aborted");
      const now = Date.now();
      const acquired = await this.redis.eval(
        acquireScript,
        1,
        this.key,
        now,
        count,
        this.maximum,
        this.leaseMs,
        ...members,
      );
      if (Number(acquired) === 1) break;
      await abortableDelay(250, signal);
    }
    let released = false;
    const renew = setInterval(() => {
      const score = Date.now() + this.leaseMs;
      const args = members.flatMap((member) => [score, member]);
      void this.redis.zadd(this.key, ...args).catch(() => undefined);
    }, Math.floor(this.leaseMs / 3));
    renew.unref();
    return {
      release: async () => {
        if (released) return;
        released = true;
        clearInterval(renew);
        await this.redis.zrem(this.key, ...members);
      },
    };
  }

  async close(): Promise<void> {
    await this.redis.quit();
  }
}

function abortableDelay(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) return reject(new Error("aborted"));
    const timer = setTimeout(() => {
      signal?.removeEventListener("abort", onAbort);
      resolve();
    }, ms);
    const onAbort = () => {
      clearTimeout(timer);
      reject(new Error("aborted"));
    };
    signal?.addEventListener("abort", onAbort, { once: true });
    timer.unref();
  });
}
