import path from "node:path";
import { createReadStream } from "node:fs";
import { createHash, randomUUID, timingSafeEqual } from "node:crypto";
import Fastify, { type FastifyInstance } from "fastify";
import multipart from "@fastify/multipart";
import type { AppConfig } from "./config.js";
import {
  TERMINAL_JOB_STATUSES,
  type ExecutionMode,
  type JobRegistry,
  type QualityProfile,
  type QueueAdapter,
  type TranslationJobRecord,
} from "./contracts.js";
import type { AgentJobRunner } from "./job-runner.js";
import { assertSafeId, assertSupportedFilename } from "./path-policy.js";
import { StorageService } from "./storage.js";

export interface ApiDependencies {
  config: AppConfig;
  registry: JobRegistry;
  queue: QueueAdapter;
  runner: AgentJobRunner;
  storage: StorageService;
  readiness?: () => Promise<void>;
}

export async function buildApi(deps: ApiDependencies): Promise<FastifyInstance> {
  const { config, registry, queue, runner, storage } = deps;
  const app = Fastify({ logger: { redact: ["req.headers.authorization", "req.body"] }, bodyLimit: config.maxInputBytes });
  await app.register(multipart, {
    limits: { files: 1, fileSize: config.maxInputBytes, fields: 4, parts: 5 },
  });

  app.addHook("onRequest", async (request, reply) => {
    if (request.url === "/healthz" || request.url === "/readyz") return;
    if (!authorized(request.headers.authorization, config.bearerToken, config.fixtureMode)) {
      return reply.code(401).send({ error: "unauthorized" });
    }
  });

  app.get("/healthz", async () => ({ status: "ok" }));
  app.get("/readyz", async (_request, reply) => {
    try {
      await deps.readiness?.();
      return { status: "ready" };
    } catch {
      return reply.code(503).send({ status: "not_ready" });
    }
  });

  app.post("/v1/files", async (request, reply) => {
    const part = await request.file();
    if (!part) return reply.code(400).send({ error: "one multipart file is required" });
    const source = await storage.ingest(assertSupportedFilename(part.filename), part.file);
    if (part.file.truncated) {
      await storage.discardSource(source);
      return reply.code(413).send({ error: "input exceeds 50 MB limit" });
    }
    try {
      await registry.putSource(source);
    } catch (error) {
      await storage.discardSource(source);
      throw error;
    }
    return reply.code(201).send(publicSource(source));
  });

  app.post("/v1/sources/register", async (request, reply) => {
    const body = asObject(request.body);
    const sourceKey = requiredString(body.source_key, "source_key", 512);
    const source = await storage.registerConfiguredSource(sourceKey);
    try {
      await registry.putSource(source);
    } catch (error) {
      await storage.discardSource(source);
      throw error;
    }
    return reply.code(201).send(publicSource(source));
  });

  app.post("/v1/translation-jobs", async (request, reply) => {
    const idempotencyKey = request.headers["idempotency-key"];
    if (typeof idempotencyKey !== "string" || !/^[A-Za-z0-9._:-]{8,128}$/.test(idempotencyKey)) {
      return reply.code(400).send({ error: "valid Idempotency-Key is required" });
    }
    const body = asObject(request.body);
    const sourceId = requiredString(body.source_id, "source_id", 128);
    const executionMode = executionModeValue(body.execution_mode);
    let candidateSourceId: string | undefined;
    if (executionMode === "pretranslated_qa") {
      candidateSourceId = requiredString(body.candidate_source_id, "candidate_source_id", 128);
      if (candidateSourceId === sourceId) throw new Error("source_id and candidate_source_id must differ");
    } else if (body.candidate_source_id !== undefined) {
      throw new Error("candidate_source_id is allowed only for pretranslated_qa");
    }
    const requestedTarget = body.target_language ?? (executionMode === "pi_agent_live" ? "auto" : undefined);
    const targetLanguage = targetLanguageValue(
      requestedTarget,
      executionMode === "pi_agent_live",
    );
    const sourceLanguage = body.source_language === undefined
      ? "auto"
      : sourceLanguageValue(body.source_language);
    if (
      sourceLanguage !== "auto" &&
      targetLanguage !== "auto" &&
      sourceLanguage.split("-", 1)[0] === targetLanguage
    ) {
      throw new Error("source_language and target_language must differ");
    }
    const qualityProfile = quality(body.quality_profile);
    const maxRepairRounds = optionalInteger(
      body.max_repair_rounds,
      config.defaultRepairRounds,
      0,
      config.hardMaxRepairRounds,
    );
    const requestFingerprint = fingerprintRequest({
      sourceId,
      executionMode,
      ...(candidateSourceId ? { candidateSourceId } : {}),
      sourceLanguage,
      targetLanguage,
      qualityProfile,
      maxRepairRounds,
    });
    const prior = await registry.findByIdempotencyKey(idempotencyKey);
    if (prior) {
      if (prior.requestFingerprint !== requestFingerprint) {
        return reply.code(409).send({ error: "idempotency_key_reused_with_different_request" });
      }
      return reply.code(200).send(publicJob(prior));
    }
    const source = await registry.getSource(sourceId);
    if (!source) return reply.code(404).send({ error: "source not found" });
    let candidateSource = null;
    let candidateSourceFilename: string | undefined;
    let existingTranslationRelpath: string | undefined;
    if (candidateSourceId) {
      candidateSource = await registry.getSource(candidateSourceId);
      if (!candidateSource) return reply.code(404).send({ error: "candidate source not found" });
      candidateSourceFilename = assertSupportedFilename(candidateSource.filename);
      const candidateExtension = path.extname(candidateSourceFilename).toLowerCase();
      if (candidateExtension !== ".docx" && candidateExtension !== ".pptx") {
        throw new Error("pretranslated candidate must be DOCX or PPTX");
      }
      if (officeFamily(source.filename) !== officeFamily(candidateSourceFilename)) {
        throw new Error("source and pretranslated candidate must belong to the same Office family");
      }
      existingTranslationRelpath = `input/existing-translation${candidateExtension}`;
    }
    const id = randomUUID();
    const createdAt = new Date();
    const filename = assertSupportedFilename(source.filename);
    const job: TranslationJobRecord = {
      id,
      requestFingerprint,
      sourceId,
      executionMode,
      ...(candidateSourceId ? { candidateSourceId } : {}),
      sourceFilename: filename,
      inputRelpath: `input/${filename}`,
      ...(candidateSourceFilename ? { candidateSourceFilename } : {}),
      ...(existingTranslationRelpath ? { existingTranslationRelpath } : {}),
      sourceLanguage,
      targetLanguage,
      requestedTargetLanguage: targetLanguage,
      ...(sourceLanguage !== "auto" ? { resolvedSourceLanguage: sourceLanguage } : {}),
      ...(targetLanguage !== "auto" ? { resolvedTargetLanguage: targetLanguage } : {}),
      qualityProfile,
      maxRepairRounds,
      repairRound: 0,
      status: "QUEUED",
      softDeadlineAt: new Date(createdAt.getTime() + config.softTimeoutMs).toISOString(),
      hardDeadlineAt: new Date(createdAt.getTime() + config.hardTimeoutMs).toISOString(),
      createdAt: createdAt.toISOString(),
      updatedAt: createdAt.toISOString(),
    };
    const selected = await registry.createJob(job, idempotencyKey);
    if (selected.id !== id) {
      if (selected.requestFingerprint !== requestFingerprint) {
        return reply.code(409).send({ error: "idempotency_key_reused_with_different_request" });
      }
      return reply.code(200).send(publicJob(selected));
    }
    try {
      await storage.prepareJob(job, source, candidateSource ?? undefined);
    } catch {
      const failed = await failBeforeQueue(registry, id, "job_preparation_failed", "Immutable job preparation failed");
      return reply.code(422).send(publicJob(failed));
    }
    try {
      await queue.add({ jobId: id });
    } catch {
      const failed = await failBeforeQueue(registry, id, "queue_submission_failed", "Durable queue submission failed");
      return reply.code(503).send(publicJob(failed));
    }
    return reply.code(202).send(publicJob(selected));
  });

  app.get<{ Params: { jobId: string } }>("/v1/translation-jobs/:jobId", async (request, reply) => {
    assertSafeId(request.params.jobId, "job_id");
    const job = await registry.getJob(request.params.jobId);
    return job ? publicJob(job) : reply.code(404).send({ error: "job not found" });
  });

  app.post<{ Params: { jobId: string } }>("/v1/translation-jobs/:jobId/cancel", async (request, reply) => {
    assertSafeId(request.params.jobId, "job_id");
    const job = await registry.getJob(request.params.jobId);
    if (!job) return reply.code(404).send({ error: "job not found" });
    if (TERMINAL_JOB_STATUSES.has(job.status)) return reply.code(409).send({ error: "job is already terminal" });
    const cancellationRequested = await runner.cancel(job.id);
    const queued = await queue.cancel(job.id);
    if (queued) {
      const now = new Date().toISOString();
      await registry.updateJob(job.id, {
        status: "CANCELLED",
        updatedAt: now,
        completedAt: now,
        errorCode: "cancelled",
        errorSummary: "Job cancelled before execution",
      });
    }
    return reply.code(202).send({ job_id: job.id, cancellation_requested: cancellationRequested || queued });
  });

  app.get<{ Params: { jobId: string } }>("/v1/translation-jobs/:jobId/report", async (request, reply) => {
    const job = await registry.getJob(assertSafeId(request.params.jobId, "job_id"));
    if (!job) return reply.code(404).send({ error: "job not found" });
    if (!job.reportRelpath) return reply.code(409).send({ error: "report is not available" });
    const artifact = await storage.artifact(job.id, job.reportRelpath);
    reply.header("content-type", "application/json").header("content-length", artifact.size);
    return reply.send(createReadStream(artifact.path));
  });

  app.get<{ Params: { jobId: string } }>("/v1/translation-jobs/:jobId/result", async (request, reply) => {
    const job = await registry.getJob(assertSafeId(request.params.jobId, "job_id"));
    if (!job) return reply.code(404).send({ error: "job not found" });
    if (!new Set(["COMPLETED", "COMPLETED_WITH_WARNINGS"]).has(job.status) || !job.resultRelpath) {
      return reply.code(409).send({ error: "deliverable result is not available" });
    }
    const artifact = await storage.artifact(job.id, job.resultRelpath);
    const extension = path.extname(artifact.path).toLowerCase();
    if (extension !== ".docx" && extension !== ".pptx") throw new Error("result extension is not deliverable");
    const contentType = extension === ".docx"
      ? "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
      : "application/vnd.openxmlformats-officedocument.presentationml.presentation";
    reply
      .header("content-type", contentType)
      .header("content-length", artifact.size)
      .header("content-disposition", `attachment; filename=translated${extension}`);
    return reply.send(createReadStream(artifact.path));
  });

  app.setErrorHandler((error, _request, reply) => {
    const message = error instanceof Error ? error.message : "invalid request";
    const status = /50 MB|too large/i.test(message) ? 413 : /not found/i.test(message) ? 404 : 400;
    void reply.code(status).send({ error: status === 400 ? "invalid_request" : message });
  });
  return app;
}

function authorized(value: string | undefined, expected: string | undefined, fixtureMode: boolean): boolean {
  if (fixtureMode && !expected) return true;
  if (!value?.startsWith("Bearer ") || !expected) return false;
  const supplied = Buffer.from(value.slice(7));
  const wanted = Buffer.from(expected);
  return supplied.length === wanted.length && timingSafeEqual(supplied, wanted);
}

function publicSource(source: { id: string; filename: string; size: number; sha256: string; createdAt: string }) {
  return { source_id: source.id, filename: source.filename, size: source.size, sha256: source.sha256, created_at: source.createdAt };
}

function publicJob(job: TranslationJobRecord) {
  return {
    job_id: job.id,
    status: job.status,
    source_id: job.sourceId,
    execution_mode: job.executionMode,
    ...(job.candidateSourceId ? { candidate_source_id: job.candidateSourceId } : {}),
    source_filename: job.sourceFilename,
    source_language: job.sourceLanguage,
    target_language: job.targetLanguage,
    requested_target_language: job.requestedTargetLanguage ?? job.targetLanguage,
    resolved_source_language:
      job.resolvedSourceLanguage ?? (job.sourceLanguage === "auto" ? null : job.sourceLanguage),
    resolved_target_language:
      job.resolvedTargetLanguage ?? (job.targetLanguage === "auto" ? null : job.targetLanguage),
    quality_profile: job.qualityProfile,
    repair_round: job.repairRound,
    max_repair_rounds: job.maxRepairRounds,
    soft_deadline_at: job.softDeadlineAt,
    hard_deadline_at: job.hardDeadlineAt,
    created_at: job.createdAt,
    updated_at: job.updatedAt,
    ...(job.completedAt ? { completed_at: job.completedAt } : {}),
    ...(job.errorCode ? { error: { code: job.errorCode, summary: job.errorSummary } } : {}),
    result_available: Boolean(job.resultRelpath && new Set(["COMPLETED", "COMPLETED_WITH_WARNINGS"]).has(job.status)),
    report_available: Boolean(job.reportRelpath),
  };
}

function asObject(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("JSON object body is required");
  return value as Record<string, unknown>;
}

function requiredString(value: unknown, name: string, max: number): string {
  if (typeof value !== "string" || value.length < 1 || value.length > max) throw new Error(`${name} is invalid`);
  return value;
}

function quality(value: unknown): QualityProfile {
  const selected = value ?? "standard";
  if (selected !== "fast" && selected !== "standard" && selected !== "strict") throw new Error("quality_profile is invalid");
  return selected;
}

function sourceLanguageValue(value: unknown): string {
  const selected = languageLabel(value, "source_language");
  if (selected.toLowerCase() === "auto") return "auto";
  return canonicalLanguage(selected, false);
}

function targetLanguageValue(value: unknown, allowAuto: boolean): string {
  const selected = languageLabel(value, "target_language");
  if (selected.toLowerCase() === "auto") {
    if (!allowAuto) throw new Error("target_language must be explicit");
    return "auto";
  }
  const canonical = canonicalLanguage(selected, true);
  if (!new Set(["en", "zh", "ja", "ko", "fr", "de", "ru"]).has(canonical)) {
    throw new Error("target_language is unsupported");
  }
  return canonical;
}

function languageLabel(value: unknown, name: string): string {
  const selected = requiredString(value, name, 32);
  if (!/^(?:auto|[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*)$/i.test(selected)) {
    throw new Error(`${name} is invalid`);
  }
  return selected;
}

function canonicalLanguage(selected: string, target: boolean): string {
  const names: Record<string, string> = {
    chinese: "zh",
    mandarin: "zh",
    english: "en",
    japanese: "ja",
    korean: "ko",
    french: "fr",
    german: "de",
    russian: "ru",
  };
  const lowered = selected.toLowerCase();
  const named = names[lowered];
  if (named) return named;
  const primary = lowered.split("-", 1)[0]!;
  if (target || primary === "en" || primary === "zh") return primary;
  return lowered;
}

function optionalInteger(value: unknown, fallback: number, minimum: number, maximum: number): number {
  const selected = value ?? fallback;
  if (!Number.isInteger(selected) || Number(selected) < minimum || Number(selected) > maximum) {
    throw new Error("max_repair_rounds is invalid");
  }
  return Number(selected);
}

function executionModeValue(value: unknown): ExecutionMode {
  const selected = value ?? "pi_agent_live";
  if (selected !== "pi_agent_live" && selected !== "pretranslated_qa") {
    throw new Error("execution_mode is invalid");
  }
  return selected;
}

function fingerprintRequest(request: {
  sourceId: string;
  executionMode: ExecutionMode;
  candidateSourceId?: string;
  sourceLanguage: string;
  targetLanguage: string;
  qualityProfile: QualityProfile;
  maxRepairRounds: number;
}): string {
  return createHash("sha256").update(JSON.stringify(request), "utf8").digest("hex");
}

function officeFamily(filename: string): "word" | "powerpoint" {
  const extension = path.extname(filename).toLowerCase();
  if (extension === ".doc" || extension === ".docx") return "word";
  if (extension === ".ppt" || extension === ".pptx") return "powerpoint";
  throw new Error("unsupported Office family");
}

async function failBeforeQueue(
  registry: JobRegistry,
  jobId: string,
  code: string,
  summary: string,
): Promise<TranslationJobRecord> {
  const now = new Date().toISOString();
  return registry.updateJob(jobId, {
    status: "FAILED",
    errorCode: code,
    errorSummary: summary,
    updatedAt: now,
    completedAt: now,
  });
}
