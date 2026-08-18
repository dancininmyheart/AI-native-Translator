import { createHash } from "node:crypto";
import type { AppConfig } from "./config.js";
import {
  TERMINAL_JOB_STATUSES,
  type JobRegistry,
  type JobStatus,
  type PythonToolEnvelope,
  type QaIssueCounts,
  type TranslationJobRecord,
} from "./contracts.js";
import { SafeEventLog } from "./event-log.js";

const MAX_BLOCKING_ISSUE_FINGERPRINTS = 128;

interface QaSnapshot {
  qualityScore: number;
  issueCounts: QaIssueCounts;
  blockingIssueFingerprints: string[];
  blockingIssueFingerprintOverflow: boolean;
  reportRelpath: string | null;
}

export class JobExecutionContext {
  private _job: TranslationJobRecord;
  private _qaIssues = 0;
  private _verificationIssues = 0;
  private _verified = false;
  private _softExpired = false;
  private _reportRelpath: string | undefined;

  constructor(
    job: TranslationJobRecord,
    private readonly registry: JobRegistry,
    private readonly events: SafeEventLog,
    readonly config: AppConfig,
  ) {
    this._job = job;
    this._qaIssues = totalIssues(job.qaIssueCounts);
    this._reportRelpath = job.reportRelpath ?? undefined;
  }

  get job(): TranslationJobRecord {
    return this._job;
  }

  get qaIssues(): number {
    return this._qaIssues;
  }

  get qaBlockingIssues(): number {
    return this._job.qaIssueCounts?.error ?? 0;
  }

  get verificationIssues(): number {
    return this._verificationIssues;
  }

  get verified(): boolean {
    return this._verified;
  }

  get reportRelpath(): string | undefined {
    return this._reportRelpath;
  }

  get softExpired(): boolean {
    return this._softExpired || Date.now() >= Date.parse(this._job.softDeadlineAt);
  }

  expireSoftDeadline(): void {
    this._softExpired = true;
  }

  async transition(status: JobStatus, patch: Partial<TranslationJobRecord> = {}): Promise<void> {
    if (TERMINAL_JOB_STATUSES.has(this._job.status)) throw new Error(`job is already terminal: ${this._job.status}`);
    this._job = await this.registry.updateJob(this._job.id, {
      ...patch,
      status,
      updatedAt: new Date().toISOString(),
      ...(TERMINAL_JOB_STATUSES.has(status) ? { completedAt: new Date().toISOString() } : {}),
    });
    await this.events.append(this._job.id, { at: new Date().toISOString(), type: "status", status });
  }

  async recordQa(envelope: PythonToolEnvelope): Promise<void> {
    const snapshot = qaSnapshot(envelope);
    this._qaIssues = totalIssues(snapshot.issueCounts);
    this._verificationIssues = 0;
    this._verified = false;
    this._reportRelpath = snapshot.reportRelpath ?? undefined;

    const parentCandidateId = this._job.repairParentCandidateId;
    if (!parentCandidateId) {
      await this.persistQaSnapshot(snapshot);
      return;
    }

    const parent = parentQaSnapshot(this._job);
    if (!parent || !strictlyImprovesRepair(snapshot, parent)) {
      this._qaIssues = parent ? totalIssues(parent.issueCounts) : 0;
      this._reportRelpath = parent?.reportRelpath ?? undefined;
      await this.transition("NEEDS_REVIEW", {
        candidateId: parentCandidateId,
        qaQualityScore: parent?.qualityScore ?? null,
        qaIssueCounts: parent?.issueCounts ?? null,
        qaBlockingIssueFingerprints: parent?.blockingIssueFingerprints ?? null,
        qaBlockingIssueFingerprintOverflow:
          parent?.blockingIssueFingerprintOverflow ?? null,
        reportRelpath: parent?.reportRelpath ?? null,
        repairParentCandidateId: null,
        repairParentQaQualityScore: null,
        repairParentQaIssueCounts: null,
        repairParentQaBlockingIssueFingerprints: null,
        repairParentQaBlockingIssueFingerprintOverflow: null,
        repairParentReportRelpath: null,
        errorCode: "repair_not_improved",
        errorSummary: `Repair candidate ${this._job.candidateId ?? "unknown"} did not strictly improve full QA; retained ${parentCandidateId}`,
      });
      return;
    }

    await this.persistQaSnapshot(snapshot, true);
  }

  recordVerification(envelope: PythonToolEnvelope): void {
    const counts = qaIssueCounts(envelope);
    this._verificationIssues = totalIssues(counts);
    this._verified =
      envelope.status.toUpperCase() === "COMPLETED" &&
      envelope.error === null &&
      counts.error === 0;
  }

  async selectCandidate(
    candidateId: string,
    patch: Partial<TranslationJobRecord> = {},
  ): Promise<void> {
    const nextRepairRound = patch.repairRound;
    const isRepairSelection =
      typeof nextRepairRound === "number" && nextRepairRound > this._job.repairRound;
    const parentCandidateId = isRepairSelection ? this._job.candidateId : undefined;
    if (
      isRepairSelection &&
      (!parentCandidateId ||
        this._job.qaQualityScore == null ||
        this._job.qaIssueCounts == null ||
        this._job.qaBlockingIssueFingerprints == null)
    ) {
      throw new Error("repair candidate cannot be selected without a persisted QA baseline");
    }
    this._qaIssues = 0;
    this._verificationIssues = 0;
    this._verified = false;
    this._reportRelpath = undefined;
    await this.transition("QA", {
      ...patch,
      candidateId,
      qaQualityScore: null,
      qaIssueCounts: null,
      qaBlockingIssueFingerprints: null,
      qaBlockingIssueFingerprintOverflow: null,
      reportRelpath: null,
      repairParentCandidateId: parentCandidateId ?? null,
      repairParentQaQualityScore: isRepairSelection
        ? (this._job.qaQualityScore ?? null)
        : null,
      repairParentQaIssueCounts: isRepairSelection
        ? (this._job.qaIssueCounts ?? null)
        : null,
      repairParentQaBlockingIssueFingerprints: isRepairSelection
        ? (this._job.qaBlockingIssueFingerprints ?? null)
        : null,
      repairParentQaBlockingIssueFingerprintOverflow: isRepairSelection
        ? (this._job.qaBlockingIssueFingerprintOverflow ?? false)
        : null,
      repairParentReportRelpath: isRepairSelection
        ? (this._job.reportRelpath ?? null)
        : null,
    });
  }

  async recordTranslationRouting(envelope: PythonToolEnvelope): Promise<void> {
    const requestedTarget = this._job.requestedTargetLanguage ?? this._job.targetLanguage;
    const envelopeRequested = envelope.summary.requested_target_language;
    if (
      envelopeRequested !== undefined &&
      (typeof envelopeRequested !== "string" || envelopeRequested !== requestedTarget)
    ) {
      throw new Error("translation summary requested target does not match the job");
    }

    const sourceValue =
      envelope.summary.resolved_source_language ??
      envelope.summary.source_language ??
      (this._job.sourceLanguage === "auto" ? undefined : this._job.sourceLanguage);
    const targetValue =
      envelope.summary.resolved_target_language ??
      envelope.summary.target_language ??
      (this._job.targetLanguage === "auto" ? undefined : this._job.targetLanguage);
    const resolvedSource = validatedResolvedSource(sourceValue);
    const resolvedTarget = validatedResolvedTarget(targetValue);
    const expectedTarget = requestedTarget === "auto"
      ? (resolvedSource.split("-", 1)[0] === "zh" ? "en" : "zh")
      : validatedResolvedTarget(requestedTarget);
    if (resolvedTarget !== expectedTarget) {
      throw new Error("translation summary resolved target violates the requested route");
    }
    this._job = await this.registry.updateJob(this._job.id, {
      resolvedSourceLanguage: resolvedSource,
      resolvedTargetLanguage: resolvedTarget,
      updatedAt: new Date().toISOString(),
    });
  }

  private async persistQaSnapshot(snapshot: QaSnapshot, clearRepairParent = false): Promise<void> {
    this._job = await this.registry.updateJob(this._job.id, {
      qaQualityScore: snapshot.qualityScore,
      qaIssueCounts: snapshot.issueCounts,
      qaBlockingIssueFingerprints: snapshot.blockingIssueFingerprints,
      qaBlockingIssueFingerprintOverflow: snapshot.blockingIssueFingerprintOverflow,
      reportRelpath: snapshot.reportRelpath,
      ...(clearRepairParent
        ? {
            repairParentCandidateId: null,
            repairParentQaQualityScore: null,
            repairParentQaIssueCounts: null,
            repairParentQaBlockingIssueFingerprints: null,
            repairParentQaBlockingIssueFingerprintOverflow: null,
            repairParentReportRelpath: null,
          }
        : {}),
      updatedAt: new Date().toISOString(),
    });
  }

  async markFailed(code: string, summary: string): Promise<void> {
    if (TERMINAL_JOB_STATUSES.has(this._job.status)) return;
    await this.transition("FAILED", { errorCode: code, errorSummary: summary.slice(0, 500) });
  }

  async markCancelled(): Promise<void> {
    if (TERMINAL_JOB_STATUSES.has(this._job.status)) return;
    await this.transition("CANCELLED", { errorCode: "cancelled", errorSummary: "Job cancelled" });
  }

  async markNeedsReview(code: string, summary: string): Promise<void> {
    if (TERMINAL_JOB_STATUSES.has(this._job.status)) return;
    await this.transition("NEEDS_REVIEW", { errorCode: code, errorSummary: summary.slice(0, 500) });
  }
}

function validatedResolvedSource(value: unknown): string {
  if (
    typeof value !== "string" ||
    value.length < 2 ||
    value.length > 32 ||
    value === "auto" ||
    !/^[a-z]{2,8}(?:-[a-z0-9]{1,8})*$/.test(value)
  ) {
    throw new Error("translation summary resolved source is invalid");
  }
  return value;
}

function validatedResolvedTarget(value: unknown): string {
  if (typeof value !== "string") {
    throw new Error("translation summary resolved target is invalid");
  }
  const canonical = value.toLowerCase();
  if (!/^[a-z]{2,8}(?:-[a-z0-9]{1,8})*$/.test(canonical)) {
    throw new Error("translation summary resolved target is invalid");
  }
  const primary = canonical.split("-", 1)[0]!;
  if (!new Set(["en", "zh", "ja", "ko", "fr", "de", "ru"]).has(primary)) {
    throw new Error("translation summary resolved target is invalid");
  }
  return primary;
}

function qaSnapshot(envelope: PythonToolEnvelope): QaSnapshot {
  const issueCounts = qaIssueCounts(envelope);
  const providedScore = envelope.summary.quality_score;
  const calculatedScore = qualityScoreFromCounts(issueCounts);
  const qualityScore =
    typeof providedScore === "number" &&
    Number.isFinite(providedScore) &&
    providedScore >= 0 &&
    providedScore <= 100 &&
    Math.abs(providedScore - calculatedScore) < 0.05
      ? providedScore
      : calculatedScore;
  const fingerprints = blockingIssueFingerprints(envelope.issues);
  const summarizedBlockingIssues = envelope.issues.filter(
    (issue) => normalizedSeverity(issue.severity) === "error",
  ).length;
  return {
    qualityScore,
    issueCounts,
    blockingIssueFingerprints: fingerprints.values,
    blockingIssueFingerprintOverflow:
      fingerprints.overflow || issueCounts.error > summarizedBlockingIssues,
    reportRelpath:
      envelope.artifacts.find((artifact) => artifact.kind === "qa_report")?.relpath ?? null,
  };
}

function parentQaSnapshot(job: TranslationJobRecord): QaSnapshot | null {
  if (
    job.repairParentQaQualityScore == null ||
    job.repairParentQaIssueCounts == null ||
    job.repairParentQaBlockingIssueFingerprints == null
  ) {
    return null;
  }
  return {
    qualityScore: job.repairParentQaQualityScore,
    issueCounts: job.repairParentQaIssueCounts,
    blockingIssueFingerprints: job.repairParentQaBlockingIssueFingerprints,
    blockingIssueFingerprintOverflow:
      job.repairParentQaBlockingIssueFingerprintOverflow ?? false,
    reportRelpath: job.repairParentReportRelpath ?? null,
  };
}

function strictlyImprovesRepair(candidate: QaSnapshot, parent: QaSnapshot): boolean {
  if (candidate.qualityScore <= parent.qualityScore) return false;
  if (candidate.issueCounts.error > parent.issueCounts.error) return false;
  if (
    candidate.issueCounts.error === parent.issueCounts.error &&
    candidate.issueCounts.warning > parent.issueCounts.warning
  ) return false;
  if (
    candidate.issueCounts.error === parent.issueCounts.error &&
    candidate.issueCounts.warning === parent.issueCounts.warning &&
    candidate.issueCounts.info > parent.issueCounts.info
  ) return false;
  if (
    candidate.blockingIssueFingerprintOverflow ||
    (parent.blockingIssueFingerprintOverflow && candidate.issueCounts.error > 0)
  ) {
    return false;
  }
  const parentBlocking = new Set(parent.blockingIssueFingerprints);
  return candidate.blockingIssueFingerprints.every((value) => parentBlocking.has(value));
}

function qaIssueCounts(envelope: PythonToolEnvelope): QaIssueCounts {
  const derived: QaIssueCounts = { error: 0, warning: 0, info: 0 };
  for (const issue of envelope.issues) {
    const severity = normalizedSeverity(issue.severity);
    derived[severity] += 1;
  }
  const supplied = envelope.summary.issue_counts;
  if (
    isIssueCounts(supplied) &&
    supplied.error >= derived.error &&
    supplied.warning >= derived.warning &&
    supplied.info >= derived.info
  ) return supplied;
  return derived;
}

function isIssueCounts(value: unknown): value is QaIssueCounts {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const record = value as Record<string, unknown>;
  return (["error", "warning", "info"] as const).every(
    (name) => Number.isInteger(record[name]) && Number(record[name]) >= 0,
  );
}

function normalizedSeverity(value: unknown): keyof QaIssueCounts {
  const severity = typeof value === "string" ? value.toLowerCase() : "error";
  return severity === "warning" || severity === "info" ? severity : "error";
}

function totalIssues(counts: QaIssueCounts | null | undefined): number {
  return counts ? counts.error + counts.warning + counts.info : 0;
}

function qualityScoreFromCounts(counts: QaIssueCounts): number {
  const penalty = Math.min(70, counts.error * 10) +
    Math.min(25, counts.warning * 2) +
    Math.min(5, counts.info * 0.5);
  return Math.round(Math.max(0, 100 - penalty) * 10) / 10;
}

function blockingIssueFingerprints(issues: Array<Record<string, unknown>>): {
  values: string[];
  overflow: boolean;
} {
  const values = new Set<string>();
  let overflow = false;
  for (const issue of issues) {
    if (normalizedSeverity(issue.severity) !== "error") continue;
    const hasStableLocation = [issue.block_id, issue.page, issue.slide].some(
      (value) => normalizeFingerprintPart(value) !== "",
    );
    const identity = (hasStableLocation
      ? ["error", issue.stage, issue.block_id, issue.page, issue.slide]
      : ["error", issue.stage, issue.code])
      .map((value) => normalizeFingerprintPart(value))
      .join("\u001f");
    const fingerprint = createHash("sha256").update(identity).digest("hex");
    if (values.has(fingerprint)) continue;
    if (values.size >= MAX_BLOCKING_ISSUE_FINGERPRINTS) {
      overflow = true;
      continue;
    }
    values.add(fingerprint);
  }
  return { values: [...values].sort(), overflow };
}

function normalizeFingerprintPart(value: unknown): string {
  if (typeof value === "number" && Number.isFinite(value)) return String(value);
  if (typeof value !== "string") return "";
  return value.trim().toLowerCase().slice(0, 256);
}
