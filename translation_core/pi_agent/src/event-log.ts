import path from "node:path";
import { appendFile, mkdir } from "node:fs/promises";
import type { JobEvent } from "./contracts.js";
import { assertSafeId } from "./path-policy.js";

const ALLOWED_KEYS = new Set(["at", "type", "status", "tool", "outcome", "code"]);

export class SafeEventLog {
  constructor(private readonly jobsRoot: string) {}

  async append(jobId: string, event: JobEvent): Promise<void> {
    assertSafeId(jobId, "job_id");
    const sanitized = Object.fromEntries(Object.entries(event).filter(([key]) => ALLOWED_KEYS.has(key)));
    const directory = path.join(this.jobsRoot, jobId);
    await mkdir(directory, { recursive: true });
    await appendFile(path.join(directory, "agent-events.jsonl"), `${JSON.stringify(sanitized)}\n`, "utf8");
  }
}
