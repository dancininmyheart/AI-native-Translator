export const SUPPORTED_EXTENSIONS = [".doc", ".docx", ".ppt", ".pptx"] as const;

export type QualityProfile = "fast" | "standard" | "strict";
export type ExecutionMode = "pi_agent_live" | "pretranslated_qa";

export type JobStatus =
  | "QUEUED"
  | "PREFLIGHT"
  | "TRANSLATING"
  | "QA"
  | "REPAIRING"
  | "VERIFYING"
  | "COMPLETED"
  | "COMPLETED_WITH_WARNINGS"
  | "NEEDS_REVIEW"
  | "FAILED"
  | "CANCELLED";

export const TERMINAL_JOB_STATUSES = new Set<JobStatus>([
  "COMPLETED",
  "COMPLETED_WITH_WARNINGS",
  "NEEDS_REVIEW",
  "FAILED",
  "CANCELLED",
]);

export interface SourceRecord {
  id: string;
  filename: string;
  storedPath: string;
  size: number;
  sha256: string;
  createdAt: string;
}

export interface TranslationJobRequest {
  sourceId: string;
  executionMode: ExecutionMode;
  candidateSourceId?: string;
  sourceLanguage: string;
  targetLanguage: string;
  /** Immutable target label supplied by the caller (including `auto`). */
  requestedTargetLanguage?: string;
  /** Source language detected or normalized by the Python pipeline. */
  resolvedSourceLanguage?: string;
  /** Concrete provider target selected by the Python pipeline. */
  resolvedTargetLanguage?: string;
  qualityProfile: QualityProfile;
  maxRepairRounds: number;
}

export interface QaIssueCounts {
  error: number;
  warning: number;
  info: number;
}

export interface TranslationJobRecord extends TranslationJobRequest {
  id: string;
  /** SHA-256 of the normalized create-job request bound to its idempotency key. */
  requestFingerprint?: string;
  status: JobStatus;
  sourceFilename: string;
  inputRelpath: string;
  candidateSourceFilename?: string;
  existingTranslationRelpath?: string;
  candidateId?: string;
  repairRound: number;
  /** Last full-QA snapshot for candidateId. Null means the selected candidate has not been QA'd yet. */
  qaQualityScore?: number | null;
  qaIssueCounts?: QaIssueCounts | null;
  /** SHA-256 fingerprints of bounded, non-text blocking issue identity fields. */
  qaBlockingIssueFingerprints?: string[] | null;
  qaBlockingIssueFingerprintOverflow?: boolean | null;
  /** Immutable baseline retained while a repair candidate awaits full semantic QA. */
  repairParentCandidateId?: string | null;
  repairParentQaQualityScore?: number | null;
  repairParentQaIssueCounts?: QaIssueCounts | null;
  repairParentQaBlockingIssueFingerprints?: string[] | null;
  repairParentQaBlockingIssueFingerprintOverflow?: boolean | null;
  repairParentReportRelpath?: string | null;
  softDeadlineAt: string;
  hardDeadlineAt: string;
  createdAt: string;
  updatedAt: string;
  startedAt?: string;
  cancellationRequestedAt?: string;
  completedAt?: string;
  resultRelpath?: string;
  reportRelpath?: string | null;
  errorCode?: string;
  errorSummary?: string;
  warnings?: string[];
}

export interface PythonToolEnvelope {
  tool: PythonToolName;
  job_id: string;
  status: string;
  summary: Record<string, unknown>;
  artifacts: Array<{
    artifact_id: string;
    kind: string;
    relpath: string;
    filename: string;
    size_bytes: number;
    sha256: string;
    media_type?: string | null;
  }>;
  issues: Array<Record<string, unknown>>;
  error: { code?: string; message?: string } | null;
}

export const PYTHON_TOOL_NAMES = [
  "preflight_document",
  "translate_once",
  "run_document_qa",
  "apply_repair",
  "verify_candidate",
  "finalize_result",
] as const;

export type PythonToolName = (typeof PYTHON_TOOL_NAMES)[number];

export interface QueuePayload {
  jobId: string;
}

export interface QueueAdapter {
  add(payload: QueuePayload): Promise<void>;
  /** Ensure a durable QUEUED record has a BullMQ job that can still run. */
  ensureRunnable(payload: QueuePayload): Promise<void>;
  cancel(jobId: string): Promise<boolean>;
  close(): Promise<void>;
}

export interface JobRegistry {
  putSource(source: SourceRecord): Promise<void>;
  getSource(id: string): Promise<SourceRecord | null>;
  createJob(job: TranslationJobRecord, idempotencyKey: string): Promise<TranslationJobRecord>;
  getJob(id: string): Promise<TranslationJobRecord | null>;
  listQueuedJobs(): Promise<TranslationJobRecord[]>;
  updateJob(id: string, patch: Partial<TranslationJobRecord>): Promise<TranslationJobRecord>;
  findByIdempotencyKey(key: string): Promise<TranslationJobRecord | null>;
  close(): Promise<void>;
}

export interface JobEvent {
  at: string;
  type: string;
  status?: JobStatus;
  tool?: PythonToolName;
  outcome?: string;
  code?: string;
}
