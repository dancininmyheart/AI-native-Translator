import path from "node:path";

function integer(name: string, fallback: number, minimum: number, maximum: number): number {
  const raw = process.env[name];
  const value = raw === undefined ? fallback : Number(raw);
  if (!Number.isInteger(value) || value < minimum || value > maximum) {
    throw new Error(`${name} must be an integer in [${minimum}, ${maximum}]`);
  }
  return value;
}

function boolean(name: string, fallback: boolean): boolean {
  const raw = process.env[name];
  if (raw === undefined) return fallback;
  if (raw === "true" || raw === "1") return true;
  if (raw === "false" || raw === "0") return false;
  throw new Error(`${name} must be true/false or 1/0`);
}

function safePythonModule(value: string): string {
  if (!/^[A-Za-z_][A-Za-z0-9_.]*$/.test(value)) {
    throw new Error("PI_AGENT_PYTHON_MODULE must be a dotted Python module name");
  }
  return value;
}

export interface AppConfig {
  host: string;
  port: number;
  bearerToken?: string;
  fixtureMode: boolean;
  dataRoot: string;
  jobsRoot: string;
  uploadsRoot: string;
  sourceRegistrationRoot?: string;
  skillDir: string;
  redisUrl: string;
  queueName: string;
  jobConcurrency: number;
  rateLimitMax: number;
  rateLimitDurationMs: number;
  translationConcurrency: number;
  providerMaxConcurrency: number;
  semanticQaEnabled: boolean;
  processingRepairEnabled: boolean;
  softTimeoutMs: number;
  hardTimeoutMs: number;
  defaultRepairRounds: number;
  hardMaxRepairRounds: number;
  maxInputBytes: number;
  pythonExecutable: string;
  pythonModule: string;
  pythonToolTimeoutMs: number;
  sofficeExecutable: string;
  piAgentDir: string;
  piModelProvider: string;
  piModelId?: string;
  piThinkingLevel: "off" | "minimal" | "low" | "medium" | "high";
  openaiBaseUrl?: string;
  openaiModel?: string;
}

export function loadConfig(): AppConfig {
  const fixtureMode = boolean("PI_AGENT_FIXTURE_MODE", false);
  const dataRoot = path.resolve(process.env.PI_AGENT_DATA_ROOT ?? path.join(process.cwd(), "var"));
  const sourceRegistration = process.env.PI_AGENT_SOURCE_REGISTRATION_ROOT;
  const bearerToken = process.env.PI_AGENT_API_BEARER_TOKEN;
  if (!fixtureMode && !bearerToken) {
    throw new Error("PI_AGENT_API_BEARER_TOKEN is required outside fixture mode");
  }
  const softTimeoutMs = integer("PI_AGENT_SOFT_TIMEOUT_SECONDS", 27 * 60, 30, 24 * 60 * 60) * 1000;
  const hardTimeoutMs = integer("PI_AGENT_HARD_TIMEOUT_SECONDS", 35 * 60, 30, 24 * 60 * 60) * 1000;
  if (hardTimeoutMs <= softTimeoutMs) {
    throw new Error("PI_AGENT_HARD_TIMEOUT_SECONDS must exceed PI_AGENT_SOFT_TIMEOUT_SECONDS");
  }
  const hardMaxRepairRounds = integer("PI_AGENT_HARD_MAX_REPAIR_ROUNDS", 3, 0, 3);
  const defaultRepairRounds = integer("PI_AGENT_DEFAULT_REPAIR_ROUNDS", 2, 0, hardMaxRepairRounds);
  const translationConcurrency = integer("PI_AGENT_TRANSLATION_CONCURRENCY", 4, 1, 8);
  const providerMaxConcurrency = integer("PI_AGENT_PROVIDER_MAX_CONCURRENCY", 20, 1, 10000);
  if (providerMaxConcurrency < translationConcurrency) {
    throw new Error("PI_AGENT_PROVIDER_MAX_CONCURRENCY must be at least PI_AGENT_TRANSLATION_CONCURRENCY");
  }
  const thinking = process.env.PI_AGENT_THINKING_LEVEL ?? "low";
  if (!new Set(["off", "minimal", "low", "medium", "high"]).has(thinking)) {
    throw new Error("PI_AGENT_THINKING_LEVEL is invalid");
  }
  return {
    host: process.env.PI_AGENT_HOST ?? "0.0.0.0",
    port: integer("PI_AGENT_PORT", 8080, 1, 65535),
    ...(bearerToken ? { bearerToken } : {}),
    fixtureMode,
    dataRoot,
    jobsRoot: path.resolve(process.env.PI_AGENT_JOBS_ROOT ?? path.join(dataRoot, "jobs")),
    uploadsRoot: path.resolve(process.env.PI_AGENT_UPLOADS_ROOT ?? path.join(dataRoot, "uploads")),
    ...(sourceRegistration ? { sourceRegistrationRoot: path.resolve(sourceRegistration) } : {}),
    skillDir: path.resolve(
      process.env.DOCUMENT_TRANSLATION_SKILL_DIR ?? path.join(process.cwd(), "..", "..", "document-translation"),
    ),
    redisUrl: process.env.REDIS_URL ?? "redis://127.0.0.1:6379/0",
    queueName: process.env.PI_AGENT_QUEUE_NAME ?? "format-translator-pi-agent",
    jobConcurrency: integer("PI_AGENT_JOB_CONCURRENCY", 20, 1, 20),
    rateLimitMax: integer("PI_AGENT_RATE_LIMIT_MAX", 20, 1, 10000),
    rateLimitDurationMs: integer("PI_AGENT_RATE_LIMIT_DURATION_MS", 1000, 100, 3_600_000),
    translationConcurrency,
    providerMaxConcurrency,
    semanticQaEnabled: boolean("PI_AGENT_SEMANTIC_QA_ENABLED", true),
    processingRepairEnabled: boolean("PI_AGENT_PROCESSING_REPAIR_ENABLED", true),
    softTimeoutMs,
    hardTimeoutMs,
    defaultRepairRounds,
    hardMaxRepairRounds,
    maxInputBytes: integer("PI_AGENT_MAX_INPUT_BYTES", 50 * 1024 * 1024, 1, 50 * 1024 * 1024),
    pythonExecutable: process.env.PI_AGENT_PYTHON_EXECUTABLE ?? (process.platform === "win32" ? "python" : "python3"),
    pythonModule: safePythonModule(process.env.PI_AGENT_PYTHON_MODULE ?? "format_translator.agent_tool_cli"),
    pythonToolTimeoutMs: integer("PI_AGENT_PYTHON_TOOL_TIMEOUT_SECONDS", 20 * 60, 5, 24 * 60 * 60) * 1000,
    sofficeExecutable: process.env.FORMAT_TRANSLATOR_SOFFICE_PATH ?? "soffice",
    piAgentDir: path.resolve(process.env.PI_AGENT_CONFIG_DIR ?? path.join(dataRoot, "pi-config")),
    piModelProvider: process.env.PI_MODEL_PROVIDER ?? "translation-openai",
    ...(process.env.PI_MODEL_ID ? { piModelId: process.env.PI_MODEL_ID } : {}),
    piThinkingLevel: thinking as AppConfig["piThinkingLevel"],
    ...(process.env.OPENAI_BASE_URL ? { openaiBaseUrl: process.env.OPENAI_BASE_URL } : {}),
    ...(process.env.OPENAI_MODEL ? { openaiModel: process.env.OPENAI_MODEL } : {}),
  };
}
