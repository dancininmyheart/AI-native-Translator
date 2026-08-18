import path from "node:path";
import { createHash, randomUUID } from "node:crypto";
import { createReadStream, createWriteStream } from "node:fs";
import { copyFile, lstat, mkdir, readFile, realpath, rename, rm, stat, writeFile } from "node:fs/promises";
import { pipeline } from "node:stream/promises";
import { Transform, type Readable } from "node:stream";
import type { AppConfig } from "./config.js";
import type { SourceRecord, TranslationJobRecord } from "./contracts.js";
import {
  assertSafeId,
  assertSupportedFilename,
  resolveContained,
  resolveExistingContained,
  safeArtifactPath,
} from "./path-policy.js";

export class IrrecoverableJobPreparationError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "IrrecoverableJobPreparationError";
  }
}

export class StorageService {
  constructor(private readonly config: AppConfig) {}

  async initialize(): Promise<void> {
    await Promise.all([
      mkdir(this.config.dataRoot, { recursive: true }),
      mkdir(this.config.jobsRoot, { recursive: true }),
      mkdir(this.config.uploadsRoot, { recursive: true }),
    ]);
  }

  async ingest(filename: string, stream: Readable): Promise<SourceRecord> {
    const safeName = assertSupportedFilename(filename);
    const id = randomUUID();
    const directory = path.join(this.config.uploadsRoot, id);
    await mkdir(directory, { recursive: false });
    const temporary = path.join(directory, "upload.part");
    const storedPath = path.join(directory, safeName);
    let size = 0;
    const hash = createHash("sha256");
    const meter = new Transform({
      transform: (chunk: Buffer, _encoding, callback) => {
        size += chunk.length;
        if (size > this.config.maxInputBytes) return callback(new Error("input exceeds 50 MB limit"));
        hash.update(chunk);
        callback(null, chunk);
      },
    });
    try {
      await pipeline(stream, meter, createWriteStream(temporary, { flags: "wx", mode: 0o600 }));
      await rename(temporary, storedPath);
    } catch (error) {
      await rm(directory, { recursive: true, force: true });
      throw error;
    }
    return {
      id,
      filename: safeName,
      storedPath,
      size,
      sha256: hash.digest("hex"),
      createdAt: new Date().toISOString(),
    };
  }

  async discardSource(source: Pick<SourceRecord, "id">): Promise<void> {
    const id = assertSafeId(source.id, "source_id");
    const directory = resolveContained(this.config.uploadsRoot, id);
    await rm(directory, { recursive: true, force: true });
  }

  async registerConfiguredSource(sourceKey: string): Promise<SourceRecord> {
    if (!this.config.sourceRegistrationRoot) throw new Error("source registration is disabled");
    const source = await resolveExistingContained(this.config.sourceRegistrationRoot, sourceKey);
    const info = await stat(source);
    if (info.size > this.config.maxInputBytes) throw new Error("input exceeds 50 MB limit");
    return this.ingest(assertSupportedFilename(path.basename(source)), createReadStream(source));
  }

  async prepareJob(job: TranslationJobRecord, source: SourceRecord, candidate?: SourceRecord): Promise<void> {
    try {
      assertSafeId(job.id, "job_id");
    } catch {
      throw new IrrecoverableJobPreparationError("queued job identifier is invalid");
    }
    const workspace = path.join(this.config.jobsRoot, job.id);
    const staging = path.join(this.config.jobsRoot, `.${job.id}.prepare-${randomUUID()}`);
    try {
      await buildPreparedWorkspace(staging, job, source, candidate);
      try {
        await rename(staging, workspace);
      } catch (error) {
        if (!isExistingPathError(error)) throw error;
        await validatePreparedWorkspace(workspace, job, source, candidate);
      }
    } finally {
      await rm(staging, { recursive: true, force: true });
    }
  }

  async probeWritable(): Promise<void> {
    for (const root of [this.config.dataRoot, this.config.jobsRoot, this.config.uploadsRoot, this.config.piAgentDir]) {
      await mkdir(root, { recursive: true });
      const marker = path.join(root, `.readiness-${randomUUID()}`);
      await writeFile(marker, "ready", { encoding: "utf8", flag: "wx", mode: 0o600 });
      await rm(marker, { force: true });
    }
  }

  async artifact(jobId: string, relpath: string): Promise<{ path: string; size: number }> {
    const candidate = safeArtifactPath(this.config.jobsRoot, jobId, relpath);
    const workspace = await realpath(path.join(this.config.jobsRoot, jobId));
    const resolved = await realpath(candidate);
    const relative = path.relative(workspace, resolved);
    if (relative.startsWith("..") || path.isAbsolute(relative)) throw new Error("artifact escapes job workspace");
    const info = await lstat(candidate);
    if (!info.isFile() || info.isSymbolicLink()) throw new Error("artifact is not a regular file");
    return { path: resolved, size: info.size };
  }
}

async function buildPreparedWorkspace(
  workspace: string,
  job: TranslationJobRecord,
  source: SourceRecord,
  candidate?: SourceRecord,
): Promise<void> {
  await mkdir(workspace, { recursive: false });
  const inputDirectory = path.join(workspace, "input");
  await mkdir(inputDirectory, { recursive: false });
  const destination = resolveContained(workspace, job.inputRelpath);
  await mkdir(path.dirname(destination), { recursive: true });
  await copyFile(source.storedPath, destination);
  await assertFileDigest(destination, source.sha256, "registered source changed before job preparation");
  if (job.executionMode === "pretranslated_qa") {
    if (!candidate || !job.existingTranslationRelpath) {
      throw new IrrecoverableJobPreparationError("pretranslated_qa requires a registered candidate source");
    }
    const candidateDestination = resolveContained(workspace, job.existingTranslationRelpath);
    await mkdir(path.dirname(candidateDestination), { recursive: true });
    await copyFile(candidate.storedPath, candidateDestination);
    await assertFileDigest(
      candidateDestination,
      candidate.sha256,
      "registered candidate changed before job preparation",
    );
  } else if (candidate || job.existingTranslationRelpath) {
    throw new IrrecoverableJobPreparationError("pi_agent_live cannot prepare an existing translation");
  }
  await writeFile(
    path.join(workspace, "job-request.json"),
    `${JSON.stringify(jobRequestMetadata(job, source, candidate), null, 2)}\n`,
    { encoding: "utf8", flag: "wx", mode: 0o600 },
  );
}

async function validatePreparedWorkspace(
  workspace: string,
  job: TranslationJobRecord,
  source: SourceRecord,
  candidate?: SourceRecord,
): Promise<void> {
  const workspaceInfo = await lstat(workspace);
  if (!workspaceInfo.isDirectory() || workspaceInfo.isSymbolicLink()) {
    throw new IrrecoverableJobPreparationError("existing job workspace is not a directory");
  }
  if (job.executionMode === "pretranslated_qa") {
    if (!candidate || !job.existingTranslationRelpath) {
      throw new IrrecoverableJobPreparationError("pretranslated_qa requires a registered candidate source");
    }
  } else if (candidate || job.existingTranslationRelpath) {
    throw new IrrecoverableJobPreparationError("pi_agent_live cannot prepare an existing translation");
  }
  const request = await readJsonFile(path.join(workspace, "job-request.json"));
  if (
    !sameJsonObject(request, jobRequestMetadata(job, source, candidate)) &&
    !sameJsonObject(request, legacyJobRequestMetadata(job, source, candidate))
  ) {
    throw new IrrecoverableJobPreparationError("existing job workspace is not bound to this request");
  }
  const destination = resolveContained(workspace, job.inputRelpath);
  await assertFileDigest(destination, source.sha256, "existing job workspace source digest does not match request");
  if (job.executionMode === "pretranslated_qa") {
    await assertFileDigest(
      resolveContained(workspace, job.existingTranslationRelpath!),
      candidate!.sha256,
      "existing job workspace candidate digest does not match request",
    );
  }
}

function jobRequestMetadata(
  job: TranslationJobRecord,
  source: SourceRecord,
  candidate?: SourceRecord,
): Record<string, unknown> {
  return {
    job_id: job.id,
    source_id: job.sourceId,
    ...(job.candidateSourceId ? { candidate_source_id: job.candidateSourceId } : {}),
    input_relpath: job.inputRelpath,
    execution_mode: job.executionMode,
    ...(job.existingTranslationRelpath ? { existing_translation_relpath: job.existingTranslationRelpath } : {}),
    source_sha256: source.sha256,
    ...(candidate ? { candidate_source_sha256: candidate.sha256 } : {}),
    source_language: job.sourceLanguage,
    target_language: job.targetLanguage,
    quality_profile: job.qualityProfile,
    max_repair_rounds: job.maxRepairRounds,
  };
}

function legacyJobRequestMetadata(
  job: TranslationJobRecord,
  source: SourceRecord,
  candidate?: SourceRecord,
): Record<string, unknown> {
  const metadata = jobRequestMetadata(job, source, candidate);
  delete metadata.source_id;
  delete metadata.candidate_source_id;
  return metadata;
}

async function readJsonFile(filePath: string): Promise<Record<string, unknown>> {
  const info = await lstat(filePath);
  if (!info.isFile() || info.isSymbolicLink()) {
    throw new IrrecoverableJobPreparationError("existing job request is not a regular file");
  }
  const serialized = await readFile(filePath, "utf8");
  let parsed: unknown;
  try {
    parsed = JSON.parse(serialized) as unknown;
  } catch {
    throw new IrrecoverableJobPreparationError("existing job request is not valid JSON");
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new IrrecoverableJobPreparationError("existing job request is not a JSON object");
  }
  return parsed as Record<string, unknown>;
}

function sameJsonObject(actual: Record<string, unknown>, expected: Record<string, unknown>): boolean {
  const actualKeys = Object.keys(actual).sort();
  const expectedKeys = Object.keys(expected).sort();
  if (actualKeys.length !== expectedKeys.length) return false;
  return expectedKeys.every((key, index) => actualKeys[index] === key && actual[key] === expected[key]);
}

function isExistingPathError(error: unknown): boolean {
  return typeof error === "object" &&
    error !== null &&
    "code" in error &&
    new Set(["EEXIST", "ENOTEMPTY", "EPERM"]).has(String((error as { code?: unknown }).code));
}

async function assertFileDigest(filePath: string, expected: string, message: string): Promise<void> {
  const info = await lstat(filePath);
  if (!info.isFile() || info.isSymbolicLink()) throw new IrrecoverableJobPreparationError(message);
  const hash = createHash("sha256");
  for await (const chunk of createReadStream(filePath)) hash.update(chunk as Buffer);
  if (hash.digest("hex") !== expected) throw new IrrecoverableJobPreparationError(message);
}
