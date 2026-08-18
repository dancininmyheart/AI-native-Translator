import { describe, expect, it } from "vitest";
import {
  ensureRunnableQueueJob,
  type RecoverableQueueBackend,
  type RecoverableQueueJob,
} from "../src/queue.js";

describe("BullMQ retained-job recovery", () => {
  it.each(["failed", "completed"])("removes a retained %s job before re-enqueueing", async (state) => {
    const events: string[] = [];
    let current: RecoverableQueueJob | null = job(state, async () => {
      events.push(`remove:${state}`);
      current = null;
    });
    const backend = queueBackend(
      () => current,
      async () => {
        events.push("add");
        current = job("waiting");
      },
    );

    await ensureRunnableQueueJob(backend, { jobId: "recover-terminal" });

    expect(events).toEqual([`remove:${state}`, "add"]);
    await expect(current?.getState()).resolves.toBe("waiting");
  });

  it("leaves an existing runnable job untouched", async () => {
    const events: string[] = [];
    const backend = queueBackend(
      () => job("active", async () => {
        events.push("remove");
      }),
      async () => {
        events.push("add");
      },
    );

    await ensureRunnableQueueJob(backend, { jobId: "already-active" });

    expect(events).toEqual([]);
  });

  it("rechecks state when another replica wins the terminal-job replacement race", async () => {
    const events: string[] = [];
    let current: RecoverableQueueJob | null = job("failed", async () => {
      events.push("raced-remove");
      current = job("waiting");
      throw new Error("already removed by peer");
    });
    const backend = queueBackend(
      () => current,
      async () => {
        events.push("unexpected-add");
      },
    );

    await ensureRunnableQueueJob(backend, { jobId: "replacement-race" });

    expect(events).toEqual(["raced-remove"]);
  });

  it("does not claim recovery when deterministic add keeps resolving to a retained terminal job", async () => {
    let current: RecoverableQueueJob | null = job("failed", async () => {
      current = null;
    });
    const backend = queueBackend(
      () => current,
      async () => {
        current = job("failed", async () => {
          current = null;
        });
      },
    );

    await expect(ensureRunnableQueueJob(backend, { jobId: "stuck-terminal" })).rejects.toThrow(
      /state changed repeatedly/,
    );
  });
});

function job(state: string, remove: () => Promise<void> = async () => undefined): RecoverableQueueJob {
  return {
    getState: async () => state,
    remove,
  };
}

function queueBackend(
  get: () => RecoverableQueueJob | null,
  add: () => Promise<void>,
): RecoverableQueueBackend {
  return {
    get: async () => get(),
    add: async () => add(),
  };
}
