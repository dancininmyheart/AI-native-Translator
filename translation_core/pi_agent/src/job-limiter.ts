import { randomUUID } from "node:crypto";
import { Redis } from "ioredis";

export const HARD_ACTIVE_JOB_LIMIT = 20;

export interface ActiveJobLease {
  /** Aborted when the lease can no longer be renewed and execution must stop. */
  readonly lostSignal: AbortSignal;
  release(): Promise<void>;
}

export interface ActiveJobLimiter {
  acquire(jobId: string, signal?: AbortSignal): Promise<ActiveJobLease>;
  close(): Promise<void>;
}

const NEVER_LOST_SIGNAL = new AbortController().signal;

export class NoopActiveJobLimiter implements ActiveJobLimiter {
  async acquire(_jobId: string, _signal?: AbortSignal): Promise<ActiveJobLease> {
    return {
      lostSignal: NEVER_LOST_SIGNAL,
      release: async () => undefined,
    };
  }

  async close(): Promise<void> {}
}

/**
 * A deployment-wide leased semaphore. Every running document owns exactly one
 * unique sorted-set member. Redis server time avoids clock skew between
 * replicas, while lease expiry releases capacity after a crashed process.
 */
export class RedisActiveJobLimiter implements ActiveJobLimiter {
  private readonly key: string;
  private readonly ownerKey: string;

  constructor(
    private readonly redis: Redis,
    private readonly maximum: number,
    queueName: string,
    private readonly leaseMs = 120_000,
    private readonly retryMs = 250,
  ) {
    if (!Number.isInteger(maximum) || maximum < 1 || maximum > HARD_ACTIVE_JOB_LIMIT) {
      throw new Error(`active job limit must be an integer in [1, ${HARD_ACTIVE_JOB_LIMIT}]`);
    }
    if (!Number.isInteger(leaseMs) || leaseMs < 3_000) {
      throw new Error("active job lease must be at least 3000ms");
    }
    if (!Number.isInteger(retryMs) || retryMs < 10) {
      throw new Error("active job retry interval must be at least 10ms");
    }
    this.key = `${queueName}:active-job-leases`;
    this.ownerKey = `${queueName}:active-job-lease-owners`;
  }

  async acquire(jobId: string, signal?: AbortSignal): Promise<ActiveJobLease> {
    if (!jobId) throw new Error("active job lease requires a job id");
    const ownerToken = randomUUID();
    const acquireScript = `
      local clock = redis.call('TIME')
      local now = (tonumber(clock[1]) * 1000) + math.floor(tonumber(clock[2]) / 1000)
      local expired = redis.call('ZRANGEBYSCORE', KEYS[1], '-inf', now)
      if #expired > 0 then
        redis.call('ZREM', KEYS[1], unpack(expired))
        redis.call('HDEL', KEYS[2], unpack(expired))
      end
      if redis.call('ZSCORE', KEYS[1], ARGV[3]) then return 0 end
      if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[1]) then return 0 end
      redis.call('ZADD', KEYS[1], now + tonumber(ARGV[2]), ARGV[3])
      redis.call('HSET', KEYS[2], ARGV[3], ARGV[4])
      return 1
    `;
    const releaseScript = `
      if redis.call('HGET', KEYS[2], ARGV[1]) ~= ARGV[2] then return 0 end
      redis.call('ZREM', KEYS[1], ARGV[1])
      redis.call('HDEL', KEYS[2], ARGV[1])
      return 1
    `;
    const releaseOwnedMember = async (): Promise<void> => {
      await this.redis.eval(
        releaseScript,
        2,
        this.key,
        this.ownerKey,
        jobId,
        ownerToken,
      );
    };

    while (true) {
      throwIfAborted(signal);
      const acquired = await this.redis.eval(
        acquireScript,
        2,
        this.key,
        this.ownerKey,
        this.maximum,
        this.leaseMs,
        jobId,
        ownerToken,
      );
      if (Number(acquired) === 1) break;
      await abortableDelay(this.retryMs, signal);
    }

    // Cancellation may win immediately after the atomic acquisition. Remove
    // the member before reporting the aborted wait so capacity cannot leak.
    if (signal?.aborted) {
      await releaseOwnedMember();
      throw new Error("active job lease wait aborted");
    }

    const lostController = new AbortController();
    let released = false;
    let renewal: Promise<void> | undefined;
    const renewScript = `
      if redis.call('HGET', KEYS[2], ARGV[2]) ~= ARGV[3] then return 0 end
      if not redis.call('ZSCORE', KEYS[1], ARGV[2]) then return 0 end
      local clock = redis.call('TIME')
      local now = (tonumber(clock[1]) * 1000) + math.floor(tonumber(clock[2]) / 1000)
      redis.call('ZADD', KEYS[1], 'XX', now + tonumber(ARGV[1]), ARGV[2])
      return 1
    `;
    const renewLease = async (): Promise<void> => {
      const renewed = await this.redis.eval(
        renewScript,
        2,
        this.key,
        this.ownerKey,
        this.leaseMs,
        jobId,
        ownerToken,
      );
      if (Number(renewed) !== 1) throw new Error("active job lease was lost");
    };
    const renewTimer = setInterval(() => {
      if (released || renewal) return;
      renewal = renewLease()
        .catch(() => {
          if (!released) lostController.abort();
        })
        .finally(() => {
          renewal = undefined;
        });
    }, Math.floor(this.leaseMs / 3));
    renewTimer.unref();

    return {
      lostSignal: lostController.signal,
      release: async () => {
        if (released) return;
        released = true;
        clearInterval(renewTimer);
        // Await an already-issued renewal before ZREM. Combined with ZADD XX,
        // this prevents a late renewal from resurrecting a released member.
        await renewal?.catch(() => undefined);
        await releaseOwnedMember();
      },
    };
  }

  async close(): Promise<void> {
    await this.redis.quit();
  }
}

function throwIfAborted(signal?: AbortSignal): void {
  if (signal?.aborted) throw new Error("active job lease wait aborted");
}

function abortableDelay(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(new Error("active job lease wait aborted"));
      return;
    }
    const timer = setTimeout(() => {
      signal?.removeEventListener("abort", onAbort);
      resolve();
    }, ms);
    const onAbort = () => {
      clearTimeout(timer);
      reject(new Error("active job lease wait aborted"));
    };
    signal?.addEventListener("abort", onAbort, { once: true });
    timer.unref();
  });
}
