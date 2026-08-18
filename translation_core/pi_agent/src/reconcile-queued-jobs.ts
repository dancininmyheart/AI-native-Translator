import type {
  JobRegistry,
  QueueAdapter,
  SourceRecord,
  TranslationJobRecord,
} from "./contracts.js";
import { IrrecoverableJobPreparationError, StorageService } from "./storage.js";

export interface QueuedJobReconciliationResult {
  inspected: number;
  enqueued: number;
  failed: number;
}

export async function reconcileQueuedJobs(deps: {
  registry: JobRegistry;
  storage: Pick<StorageService, "prepareJob">;
  queue: QueueAdapter;
}): Promise<QueuedJobReconciliationResult> {
  const queued = await deps.registry.listQueuedJobs();
  const result: QueuedJobReconciliationResult = { inspected: queued.length, enqueued: 0, failed: 0 };
  for (const queuedJob of queued) {
    const job = await deps.registry.getJob(queuedJob.id);
    if (!job || job.status !== "QUEUED") continue;
    let source: SourceRecord;
    let candidate: SourceRecord | undefined;
    try {
      source = await requiredSource(deps.registry, job.sourceId, "Registered source record is missing");
      if (job.executionMode === "pretranslated_qa") {
        if (!job.candidateSourceId) throw new IrrecoverableQueuedJobError("Registered candidate record is missing");
        candidate = await requiredSource(
          deps.registry,
          job.candidateSourceId,
          "Registered candidate record is missing",
        );
      }
      await deps.storage.prepareJob(job, source, candidate);
    } catch (error) {
      if (!(error instanceof IrrecoverableQueuedJobError) &&
          !(error instanceof IrrecoverableJobPreparationError)) {
        throw error;
      }
      await failQueuedJob(deps.registry, job, reconciliationSummary(error));
      result.failed += 1;
      continue;
    }
    const current = await deps.registry.getJob(job.id);
    if (!current || current.status !== "QUEUED") continue;
    try {
      await deps.queue.ensureRunnable({ jobId: job.id });
    } catch (error) {
      const raced = await deps.registry.getJob(job.id);
      if (!raced || raced.status !== "QUEUED") continue;
      throw error;
    }
    result.enqueued += 1;
  }
  return result;
}

async function requiredSource(
  registry: JobRegistry,
  id: string,
  missingSummary: string,
): Promise<SourceRecord> {
  const source = await registry.getSource(id);
  if (!source) throw new IrrecoverableQueuedJobError(missingSummary);
  return source;
}

async function failQueuedJob(
  registry: JobRegistry,
  job: TranslationJobRecord,
  summary: string,
): Promise<void> {
  const current = await registry.getJob(job.id);
  if (!current || current.status !== "QUEUED") return;
  const now = new Date().toISOString();
  try {
    await registry.updateJob(job.id, {
      status: "FAILED",
      errorCode: "queued_job_reconciliation_failed",
      errorSummary: summary,
      updatedAt: now,
      completedAt: now,
    });
  } catch (error) {
    const raced = await registry.getJob(job.id);
    if (raced && raced.status !== "QUEUED") return;
    throw error;
  }
}

function reconciliationSummary(error: unknown): string {
  if (error instanceof IrrecoverableQueuedJobError || error instanceof IrrecoverableJobPreparationError) {
    return error.message;
  }
  return "Queued job workspace could not be prepared";
}

class IrrecoverableQueuedJobError extends Error {}
