import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import type { AppConfig } from "./config.js";
import {
  PYTHON_TOOL_NAMES,
  type PythonToolEnvelope,
  type PythonToolName,
} from "./contracts.js";
import { assertSafeId } from "./path-policy.js";

export type PythonToolPayload = Record<string, unknown>;

export interface PythonToolBridge {
  execute(
    tool: PythonToolName,
    jobId: string,
    payload: PythonToolPayload,
    signal?: AbortSignal,
  ): Promise<PythonToolEnvelope>;
}

export interface SpawnInvocation {
  executable: string;
  argv: string[];
  options: {
    cwd: string;
    env: NodeJS.ProcessEnv;
    shell: false;
    windowsHide: true;
    detached: boolean;
  };
}

export type SpawnFactory = (invocation: SpawnInvocation) => ChildProcessWithoutNullStreams;

const defaultSpawner: SpawnFactory = ({ executable, argv, options }) => spawn(executable, argv, options);

export class PythonCliToolBridge implements PythonToolBridge {
  constructor(
    private readonly config: AppConfig,
    private readonly spawner: SpawnFactory = defaultSpawner,
  ) {}

  async execute(
    tool: PythonToolName,
    jobId: string,
    payload: PythonToolPayload,
    signal?: AbortSignal,
  ): Promise<PythonToolEnvelope> {
    if (!PYTHON_TOOL_NAMES.includes(tool)) throw new Error("unsupported Python tool");
    assertSafeId(jobId, "job_id");
    if (signal?.aborted) throw new Error("tool execution aborted");
    const argv = [
      "-m",
      this.config.pythonModule,
      tool,
      "--job-root",
      this.config.jobsRoot,
      "--job-id",
      jobId,
    ];
    const child = this.spawner({
      executable: this.config.pythonExecutable,
      argv,
      options: {
        cwd: this.config.jobsRoot,
        env: pythonEnvironment(),
        shell: false,
        windowsHide: true,
        detached: process.platform !== "win32",
      },
    });
    let stdout = "";
    let stderr = "";
    const capture = (current: string, chunk: Buffer): string => {
      const next = current + chunk.toString("utf8");
      return next.length > 1_000_000 ? next.slice(-1_000_000) : next;
    };
    child.stdout.on("data", (chunk: Buffer) => (stdout = capture(stdout, chunk)));
    child.stderr.on("data", (chunk: Buffer) => (stderr = capture(stderr, chunk)));
    let terminationRequested = false;
    let forcedKillTimer: NodeJS.Timeout | undefined;
    const abort = () => {
      if (terminationRequested) return;
      terminationRequested = true;
      signalProcessTree(child, "SIGTERM");
      forcedKillTimer = setTimeout(() => signalProcessTree(child, "SIGKILL"), 5_000);
      forcedKillTimer.unref();
    };
    signal?.addEventListener("abort", abort, { once: true });
    const timeout = setTimeout(abort, this.config.pythonToolTimeoutMs);
    child.stdin.end(`${JSON.stringify(payload)}\n`, "utf8");
    const { code } = await new Promise<{ code: number | null }>((resolve, reject) => {
      child.once("error", reject);
      child.once("close", (code) => resolve({ code }));
    }).finally(() => {
      clearTimeout(timeout);
      if (forcedKillTimer) clearTimeout(forcedKillTimer);
      signal?.removeEventListener("abort", abort);
    });
    if (signal?.aborted || terminationRequested) throw new Error("Python tool was aborted or timed out");
    const lines = stdout.trim().split(/\r?\n/).filter(Boolean);
    if (code !== 0) {
      if (lines.length === 1) {
        try {
          const envelope = validateEnvelope(JSON.parse(lines[0]!), tool, jobId);
          if (isStructuredFailureEnvelope(envelope)) return envelope;
        } catch {
          // Preserve the fail-closed exit-code error for malformed envelopes.
        }
      }
      throw new Error(`Python tool ${tool} exited with controlled code ${String(code)}`);
    }
    if (lines.length !== 1) throw new Error(`Python tool ${tool} did not return one JSON envelope`);
    let envelope: unknown;
    try {
      envelope = JSON.parse(lines[0]!);
    } catch {
      throw new Error(`Python tool ${tool} returned invalid JSON`);
    }
    return validateEnvelope(envelope, tool, jobId);
  }
}

function isStructuredFailureEnvelope(envelope: PythonToolEnvelope): boolean {
  return envelope.status.toUpperCase() === "FAILED" &&
    envelope.error !== null &&
    typeof envelope.error.code === "string" &&
    /^[A-Z][A-Z0-9_]{0,159}$/.test(envelope.error.code) &&
    typeof envelope.error.message === "string" &&
    envelope.error.message.length > 0 &&
    envelope.error.message.length <= 500;
}

function signalProcessTree(child: ChildProcessWithoutNullStreams, signal: NodeJS.Signals): void {
  if (process.platform !== "win32" && child.pid !== undefined) {
    try {
      process.kill(-child.pid, signal);
      return;
    } catch {
      // The process may have exited between the timeout/cancel signal and this call.
    }
  }
  try {
    child.kill(signal);
  } catch {
    // Closing is best-effort; the child close event remains authoritative.
  }
}

function validateEnvelope(value: unknown, tool: PythonToolName, jobId: string): PythonToolEnvelope {
  if (!value || typeof value !== "object") throw new Error("Python envelope must be an object");
  const raw = value as Record<string, unknown>;
  if (
    raw.tool !== tool ||
    raw.job_id !== jobId ||
    typeof raw.status !== "string" ||
    !raw.summary ||
    typeof raw.summary !== "object" ||
    Array.isArray(raw.summary)
  ) {
    throw new Error("Python envelope identity or status is invalid");
  }
  if (!Array.isArray(raw.artifacts) || !Array.isArray(raw.issues)) {
    throw new Error("Python envelope artifacts/issues are invalid");
  }
  return {
    tool,
    job_id: jobId,
    status: raw.status,
    summary: raw.summary as Record<string, unknown>,
    artifacts: raw.artifacts as PythonToolEnvelope["artifacts"],
    issues: raw.issues as Array<Record<string, unknown>>,
    error: raw.error && typeof raw.error === "object" ? (raw.error as PythonToolEnvelope["error"]) : null,
  };
}

export function pythonEnvironment(source: NodeJS.ProcessEnv = process.env): NodeJS.ProcessEnv {
  const exact = new Set([
    "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "LANG", "LC_ALL",
    "HOME", "USERPROFILE", "LOCALAPPDATA", "APPDATA", "PYTHONPATH", "VIRTUAL_ENV",
    "OPENAI_BASE_URL", "OPENAI_API_KEY", "OPENAI_MODEL", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
    "NO_PROXY", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE",
  ]);
  const result: NodeJS.ProcessEnv = { PYTHONUNBUFFERED: "1" };
  for (const [key, value] of Object.entries(source)) {
    if (value !== undefined && (exact.has(key.toUpperCase()) || key.startsWith("FORMAT_TRANSLATOR_"))) {
      result[key] = value;
    }
  }
  return result;
}
