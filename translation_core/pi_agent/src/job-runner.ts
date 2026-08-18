import path from "node:path";
import { createHash } from "node:crypto";
import { createReadStream } from "node:fs";
import { readFile, stat } from "node:fs/promises";
import type { ToolDefinition } from "@earendil-works/pi-coding-agent";
import type { AppConfig } from "./config.js";
import { createControlledTools } from "./controlled-tools.js";
import {
  TERMINAL_JOB_STATUSES,
  type JobRegistry,
  type PythonToolEnvelope,
  type QaIssueCounts,
} from "./contracts.js";
import { SafeEventLog } from "./event-log.js";
import { JobExecutionContext } from "./execution-context.js";
import {
  NoopActiveJobLimiter,
  type ActiveJobLease,
  type ActiveJobLimiter,
} from "./job-limiter.js";
import { buildForcedSkillPrompt, type PiSessionHandle, type SessionFactory } from "./pi-session.js";
import { resolveExistingContained } from "./path-policy.js";
import type { ProviderLimiter } from "./provider-limiter.js";
import type { PythonToolBridge } from "./python-tool-bridge.js";

interface ActiveExecution {
  abortController: AbortController;
  session?: PiSessionHandle;
  cancelled: boolean;
  leaseLost: boolean;
}

export class AgentJobRunner {
  private readonly active = new Map<string, ActiveExecution>();

  constructor(
    private readonly config: AppConfig,
    private readonly registry: JobRegistry,
    private readonly bridge: PythonToolBridge,
    private readonly sessionFactory: SessionFactory | null,
    private readonly providerLimiter: ProviderLimiter,
    private readonly activeJobLimiter: ActiveJobLimiter = new NoopActiveJobLimiter(),
    private readonly events = new SafeEventLog(config.jobsRoot),
  ) {}

  async run(jobId: string): Promise<void> {
    if (this.active.has(jobId)) throw new Error("job execution is already active");
    const active: ActiveExecution = {
      abortController: new AbortController(),
      cancelled: false,
      leaseLost: false,
    };
    this.active.set(jobId, active);
    let knownJob = false;
    let context: JobExecutionContext | undefined;
    let jobLease: ActiveJobLease | undefined;
    let softTimer: NodeJS.Timeout | undefined;
    let hardTimer: NodeJS.Timeout | undefined;
    let cancellationTimer: NodeJS.Timeout | undefined;
    let leaseLostListener: (() => void) | undefined;
    let tools: ToolDefinition[] | undefined;
    let candidateCreationFailureSummary: string | undefined;
    let hardExpired = false;
    let cancellationPollActive = false;

    const abortExecution = (): void => {
      active.abortController.abort();
      void active.session?.session.abort();
    };

    try {
      let job = await this.registry.getJob(jobId);
      if (!job || TERMINAL_JOB_STATUSES.has(job.status)) return;
      knownJob = true;
      if (job.cancellationRequestedAt) {
        await markJobTerminal(
          this.registry,
          this.events,
          jobId,
          "CANCELLED",
          "cancelled",
          "Job cancelled before execution",
        );
        return;
      }

      // Deadlines and durable cancellation remain observable while this
      // replica waits for deployment-wide execution capacity.
      hardTimer = setTimeout(() => {
        hardExpired = true;
        abortExecution();
      }, Math.max(1, Date.parse(job.hardDeadlineAt) - Date.now()));
      hardTimer.unref();
      cancellationTimer = setInterval(() => {
        if (cancellationPollActive || active.cancelled) return;
        cancellationPollActive = true;
        void this.registry.getJob(jobId).then((current) => {
          if (current?.cancellationRequestedAt && !active.cancelled) {
            active.cancelled = true;
            abortExecution();
          }
        }).catch(() => undefined).finally(() => {
          cancellationPollActive = false;
        });
      }, 250);
      cancellationTimer.unref();

      jobLease = await this.activeJobLimiter.acquire(jobId, active.abortController.signal);
      leaseLostListener = () => {
        active.leaseLost = true;
        abortExecution();
      };
      jobLease.lostSignal.addEventListener("abort", leaseLostListener, { once: true });
      if (jobLease.lostSignal.aborted) leaseLostListener();

      // State may have changed on another replica while capacity was full.
      job = await this.registry.getJob(jobId);
      if (!job || TERMINAL_JOB_STATUSES.has(job.status)) return;
      if (job.cancellationRequestedAt) {
        active.cancelled = true;
        throw new Error("job cancelled while waiting for active capacity");
      }
      if (active.abortController.signal.aborted) {
        throw new Error("job aborted while waiting for active capacity");
      }

      const now = new Date().toISOString();
      const resumeStatus = safeResumeStatus(job.status);
      context = new JobExecutionContext(
        await this.registry.updateJob(jobId, {
          status: resumeStatus,
          startedAt: job.startedAt ?? now,
          updatedAt: now,
        }),
        this.registry,
        this.events,
        this.config,
      );
      await this.events.append(jobId, { at: now, type: "status", status: resumeStatus });
      tools = createControlledTools(context, this.bridge, this.events, this.providerLimiter);
      observeCandidateCreationFailures(tools, (summary) => {
        candidateCreationFailureSummary = summary;
      });
      softTimer = setTimeout(() => {
        context?.expireSoftDeadline();
        void this.events.append(jobId, { at: new Date().toISOString(), type: "soft_deadline" });
      }, Math.max(1, Date.parse(job.softDeadlineAt) - Date.now()));
      softTimer.unref();

      if (this.config.fixtureMode) {
        await runFixtureWorkflow(tools, context, resumeStatus, active.abortController.signal);
      } else {
        if (!this.sessionFactory) throw new Error("production Pi session factory is unavailable");
        active.session = await this.sessionFactory.create(path.join(this.config.jobsRoot, jobId), tools);
        if (active.abortController.signal.aborted) {
          await active.session.session.abort();
          throw new Error("job aborted before Pi session prompt");
        }
        active.session.session.subscribe((event) => {
          const safeType = new Set(["agent_start", "agent_end", "agent_settled", "auto_retry_start", "auto_retry_end"]);
          if (safeType.has(event.type)) {
            void this.events.append(jobId, { at: new Date().toISOString(), type: `pi_${event.type}` });
          }
        });
        await active.session.session.prompt(buildForcedSkillPrompt(context.job.executionMode, resumeStatus), { expandPromptTemplates: true, source: "rpc" });
        await active.session.session.waitForIdle();
        const lastAssistant = [...active.session.session.messages]
          .reverse()
          .find((message) => message.role === "assistant");
        if (lastAssistant?.role === "assistant" && lastAssistant.stopReason === "error") {
          throw new Error("Pi orchestration model request failed");
        }
      }
      if (active.cancelled) throw new Error("job cancelled during execution");
      if (hardExpired) throw new Error("hard deadline reached during execution");
      if (active.leaseLost) throw new Error("active-job lease lost during execution");
      if (!TERMINAL_JOB_STATUSES.has(context.job.status)) {
        await settleAfterAgentStop(
          context,
          tools,
          path.join(this.config.jobsRoot, jobId),
          active.abortController.signal,
          candidateCreationFailureSummary,
        );
      }
    } catch (error) {
      const summary = error instanceof Error ? error.message : "unknown execution error";
      if (!knownJob) throw error;
      if (context) {
        if (active.cancelled) await context.markCancelled();
        else if (hardExpired) await context.markNeedsReview("hard_timeout", "Hard timeout reached before delivery");
        else if (active.leaseLost) {
          await context.markNeedsReview(
            "active_job_lease_lost",
            "Deployment-wide active-job lease was lost before delivery",
          );
        } else if (tools && !TERMINAL_JOB_STATUSES.has(context.job.status)) {
          try {
            await settleAfterAgentStop(
              context,
              tools,
              path.join(this.config.jobsRoot, jobId),
              active.abortController.signal,
              summary,
            );
          } catch (recoveryError) {
            const recoverySummary = recoveryError instanceof Error
              ? recoveryError.message
              : "unknown recovery error";
            await context.markNeedsReview(
              "agent_recovery_failed",
              `Pi orchestration failed (${summary}); bounded recovery failed: ${recoverySummary}`,
            );
          }
          if (!TERMINAL_JOB_STATUSES.has(context.job.status)) {
            await context.markFailed("agent_execution_failed", summary);
          }
        } else await context.markFailed("agent_execution_failed", summary);
      } else if (active.cancelled) {
        await markJobTerminal(this.registry, this.events, jobId, "CANCELLED", "cancelled", "Job cancelled while queued");
      } else if (hardExpired) {
        await markJobTerminal(
          this.registry,
          this.events,
          jobId,
          "NEEDS_REVIEW",
          "hard_timeout",
          "Hard timeout reached while waiting for active capacity",
        );
      } else if (active.leaseLost) {
        await markJobTerminal(
          this.registry,
          this.events,
          jobId,
          "NEEDS_REVIEW",
          "active_job_lease_lost",
          "Deployment-wide active-job lease was lost before execution",
        );
      } else {
        await markJobTerminal(this.registry, this.events, jobId, "FAILED", "job_start_failed", summary);
      }
    } finally {
      if (softTimer) clearTimeout(softTimer);
      if (hardTimer) clearTimeout(hardTimer);
      if (cancellationTimer) clearInterval(cancellationTimer);
      if (jobLease && leaseLostListener) {
        jobLease.lostSignal.removeEventListener("abort", leaseLostListener);
      }
      active.session?.dispose();
      try {
        await jobLease?.release();
      } catch {
        await this.events.append(jobId, {
          at: new Date().toISOString(),
          type: "active_job_lease_release_failed",
        }).catch(() => undefined);
      } finally {
        this.active.delete(jobId);
      }
    }
  }

  async cancel(jobId: string): Promise<boolean> {
    const job = await this.registry.getJob(jobId);
    if (!job || TERMINAL_JOB_STATUSES.has(job.status)) return false;
    await this.registry.updateJob(jobId, {
      cancellationRequestedAt: job.cancellationRequestedAt ?? new Date().toISOString(),
      updatedAt: new Date().toISOString(),
    });
    const active = this.active.get(jobId);
    if (active) {
      active.cancelled = true;
      active.abortController.abort();
      await active.session?.session.abort();
    }
    return true;
  }
}

async function markJobTerminal(
  registry: JobRegistry,
  events: SafeEventLog,
  jobId: string,
  status: "CANCELLED" | "NEEDS_REVIEW" | "FAILED",
  code: string,
  summary: string,
): Promise<void> {
  const current = await registry.getJob(jobId);
  if (!current || TERMINAL_JOB_STATUSES.has(current.status)) return;
  const completedAt = new Date().toISOString();
  try {
    await registry.updateJob(jobId, {
      status,
      updatedAt: completedAt,
      completedAt,
      errorCode: code,
      errorSummary: summary.slice(0, 500),
    });
  } catch (error) {
    const raced = await registry.getJob(jobId);
    if (raced && TERMINAL_JOB_STATUSES.has(raced.status)) return;
    throw error;
  }
  await events.append(jobId, { at: completedAt, type: "status", status });
}

function observeCandidateCreationFailures(
  tools: ToolDefinition[],
  recordFailure: (summary: string) => void,
): void {
  for (const name of ["preflight_document", "translate_once"] as const) {
    const tool = tools.find((candidate) => candidate.name === name);
    if (!tool) throw new Error(`controlled ${name} tool is unavailable`);
    const execute = tool.execute.bind(tool);
    tool.execute = async (toolCallId, params, signal, onUpdate, context) => {
      try {
        return await execute(toolCallId, params, signal, onUpdate, context);
      } catch (error) {
        recordFailure(`${name} failed: ${errorMessage(error)}`);
        throw error;
      }
    };
  }
}

async function runFixtureWorkflow(
  tools: ToolDefinition[],
  context: JobExecutionContext,
  resumeStatus: "PREFLIGHT" | "QA",
  signal: AbortSignal,
): Promise<void> {
  const call = async (name: string, params: Record<string, unknown>) => {
    const tool = tools.find((candidate) => candidate.name === name);
    if (!tool) throw new Error(`fixture tool missing: ${name}`);
    await tool.execute(`fixture-${name}`, params, signal, undefined, {} as never);
  };
  const currentCandidateId = (): string => {
    const candidateId = context.job.candidateId;
    if (!candidateId) throw new Error("fixture workflow has no selected candidate");
    return candidateId;
  };
  if (resumeStatus === "PREFLIGHT") {
    await call("preflight_document", {});
    if (context.job.executionMode === "pi_agent_live") await call("translate_once", {});
  } else if (!context.job.candidateId) {
    throw new Error("resumed QA job has no candidate identifier");
  }
  while (!TERMINAL_JOB_STATUSES.has(context.job.status)) {
    await call("run_document_qa", { candidate_id: currentCandidateId() });
    if (TERMINAL_JOB_STATUSES.has(context.job.status)) return;
    if (context.qaBlockingIssues === 0) break;
    if (context.softExpired) {
      await context.markNeedsReview(
        "qa_unresolved",
        `QA retained ${context.qaIssues} issue(s); the soft deadline prevented another repair`,
      );
      return;
    }
    if (
      context.job.repairRound >= context.job.maxRepairRounds ||
      context.job.repairRound >= context.config.hardMaxRepairRounds
    ) {
      await context.markNeedsReview(
        "qa_unresolved",
        `QA retained ${context.qaIssues} issue(s) after ${context.job.repairRound} repair round(s)`,
      );
      return;
    }
    await call("apply_repair", { candidate_id: currentCandidateId() });
    if (TERMINAL_JOB_STATUSES.has(context.job.status)) return;
  }
  const candidateId = currentCandidateId();
  await call("verify_candidate", { candidate_id: candidateId });
  await call("finalize_result", { candidate_id: candidateId });
}

export function safeResumeStatus(status: string): "PREFLIGHT" | "QA" {
  if (status === "QUEUED" || status === "PREFLIGHT" || status === "TRANSLATING") return "PREFLIGHT";
  if (status === "QA" || status === "REPAIRING" || status === "VERIFYING") return "QA";
  throw new Error(`job status cannot be resumed: ${status}`);
}

async function settleAfterAgentStop(
  context: JobExecutionContext,
  tools: ToolDefinition[],
  jobRoot: string,
  signal: AbortSignal,
  executionFailureSummary?: string,
): Promise<void> {
  await reconcileDurableState(context, jobRoot);
  if (!context.job.candidateId) {
    if (executionFailureSummary) {
      await context.markFailed("agent_execution_failed", executionFailureSummary);
    } else {
      await context.markNeedsReview(
        "no_candidate_produced",
        "No registered candidate is available after Pi stopped; the job is safe to retry",
      );
    }
    return;
  }
  if (context.job.status === "QA" && context.job.qaIssueCounts == null) {
    try {
      await invokeControlledTool(
        tools,
        "run_document_qa",
        { candidate_id: context.job.candidateId },
        signal,
      );
    } catch (error) {
      await reconcileDurableState(context, jobRoot);
      if (context.job.qaIssueCounts == null) {
        await context.markNeedsReview(
          "qa_recovery_failed",
          `QA recovery did not persist a usable result (${errorMessage(error)}); the job is safe to retry`,
        );
        return;
      }
    }
  }
  if (TERMINAL_JOB_STATUSES.has(context.job.status)) return;
  if (context.job.status === "QA" && context.qaBlockingIssues > 0) {
    while (
      context.job.status === "QA" &&
      context.qaBlockingIssues > 0 &&
      context.job.repairRound < context.job.maxRepairRounds &&
      !context.softExpired
    ) {
      const parentCandidate: string | undefined = context.job.candidateId;
      if (!parentCandidate) {
        await context.markNeedsReview(
          "repair_recovery_failed",
          "Bounded repair recovery lost the selected candidate identity",
        );
        return;
      }
      try {
        await invokeControlledTool(
          tools,
          "apply_repair",
          { candidate_id: parentCandidate },
          signal,
        );
      } catch (error) {
        await reconcileDurableState(context, jobRoot);
        if (TERMINAL_JOB_STATUSES.has(context.job.status)) return;
        await context.markNeedsReview(
          "repair_recovery_failed",
          `Bounded repair recovery failed (${errorMessage(error)}); retained ${parentCandidate}`,
        );
        return;
      }
      if (TERMINAL_JOB_STATUSES.has(context.job.status)) return;
      if (context.job.candidateId === parentCandidate) break;
      try {
        await invokeControlledTool(
          tools,
          "run_document_qa",
          { candidate_id: context.job.candidateId },
          signal,
        );
      } catch (error) {
        await reconcileDurableState(context, jobRoot);
        if (TERMINAL_JOB_STATUSES.has(context.job.status)) return;
        await context.markNeedsReview(
          "qa_recovery_failed",
          `Post-repair QA recovery failed (${errorMessage(error)}); retained ${context.job.candidateId}`,
        );
        return;
      }
      if (TERMINAL_JOB_STATUSES.has(context.job.status)) return;
    }
    if (TERMINAL_JOB_STATUSES.has(context.job.status)) return;
    await context.markNeedsReview(
      "qa_unresolved",
      `QA retained ${context.qaIssues} issue(s) when the Agent stopped`,
    );
    return;
  }
  if (context.job.status === "QA" && context.job.qaIssueCounts != null) {
    if (!context.verified) {
      try {
        await invokeControlledTool(
          tools,
          "verify_candidate",
          { candidate_id: context.job.candidateId },
          signal,
        );
      } catch (error) {
        await reconcileDurableState(context, jobRoot);
        if (!context.verified && !TERMINAL_JOB_STATUSES.has(context.job.status)) {
          await context.markNeedsReview(
            "verification_recovery_failed",
            `Verification recovery did not persist a passing result (${errorMessage(error)}); the job is safe to retry`,
          );
          return;
        }
      }
    }
    if (TERMINAL_JOB_STATUSES.has(context.job.status)) return;
    if (context.verified) {
      try {
        await invokeControlledTool(
          tools,
          "finalize_result",
          { candidate_id: context.job.candidateId },
          signal,
        );
      } catch (error) {
        await reconcileDurableState(context, jobRoot);
        if (!TERMINAL_JOB_STATUSES.has(context.job.status)) {
          await context.markNeedsReview(
            "finalization_recovery_failed",
            `Finalization recovery did not persist a usable result (${errorMessage(error)}); the job is safe to retry`,
          );
        }
      }
    } else {
      await context.markNeedsReview(
        "verification_unresolved",
        "The selected candidate did not pass the verification gate",
      );
    }
    return;
  }
  await context.markNeedsReview(
    "agent_incomplete",
    "Pi Agent stopped before the final delivery gate",
  );
}

interface DurableSourceState {
  input_relpath: string;
  input_sha256: string;
  size_bytes: number;
  source_format: "doc" | "docx" | "ppt" | "pptx";
  normalized_format: "docx" | "pptx" | null;
  normalized_relpath: string | null;
  normalized_sha256: string | null;
}

interface DurableQaState {
  status: "COMPLETED" | "NEEDS_REVIEW";
  candidate_sha256: string;
  source_sha256: string;
  report_relpath: string;
  issue_counts: QaIssueCounts;
  checked_at: string;
}

interface DurableCandidateState {
  candidate_id: string;
  relpath: string;
  sha256: string;
  size_bytes: number;
  document_format: "docx" | "pptx";
  source_relpath: string;
  source_sha256: string;
  source_language: string;
  target_language: string;
  requested_target_language?: string;
  resolved_target_language?: string;
  created_by: "translate_once" | "apply_repair" | "pretranslated_qa";
  parent_candidate_id: string | null;
  pipeline_manifest_relpath: string | null;
  pipeline_status: string | null;
  request_fingerprint: string | null;
  latest_qa: DurableQaState | null;
  verification: DurableQaState | null;
  created_at: string;
}

interface DurableResultState {
  candidate_id: string;
  relpath: string;
  sha256: string;
  size_bytes: number;
  finalized_at: string;
}

interface DurableToolState {
  source: DurableSourceState | null;
  candidates: Record<string, DurableCandidateState>;
  results: Record<string, DurableResultState>;
}

interface DurableReportEvidence {
  artifact: PythonToolEnvelope["artifacts"][number];
  issues: Array<Record<string, unknown>>;
}

async function reconcileDurableState(
  context: JobExecutionContext,
  jobRoot: string,
): Promise<boolean> {
  const statePath = await optionalExistingContained(jobRoot, "agent-tool-state.json");
  if (!statePath) return false;
  const state = parseDurableToolState(await readFile(statePath, "utf8"), context.job.id);
  let candidateId = context.job.candidateId;
  if (!candidateId) {
    const initial = state.candidates["candidate-r0"];
    if (!initial) return false;
    candidateId = "candidate-r0";
  }
  const candidate = state.candidates[candidateId];
  if (!candidate) return false;

  // Phase one: validate the complete durable trust chain before mutating the
  // registry. A bad late-stage report must not partially adopt routing or QA.
  await validateDurableCandidateEvidence(context, jobRoot, state.source, candidate);
  const routingEnvelope = durableRoutingEnvelope(context, candidate);
  const qaEvidence = candidate.latest_qa
    ? await validateDurableReport(
      jobRoot,
      context.job.id,
      candidate,
      candidate.latest_qa,
      "qa_report",
    )
    : null;
  const verificationEvidence = candidate.verification
    ? await validateDurableReport(
      jobRoot,
      context.job.id,
      candidate,
      candidate.verification,
      "verification_report",
    )
    : null;
  if (verificationEvidence && (!candidate.latest_qa || candidate.latest_qa.status !== "COMPLETED")) {
    throw new Error("durable verification exists without a completed full QA stage");
  }
  if (context.job.qaIssueCounts != null) {
    if (
      !candidate.latest_qa ||
      !sameIssueCounts(context.job.qaIssueCounts, candidate.latest_qa.issue_counts) ||
      context.job.reportRelpath !== candidate.latest_qa.report_relpath
    ) {
      throw new Error("durable QA state conflicts with the registered QA snapshot");
    }
  }
  const candidateResults = Object.entries(state.results).filter(
    ([, result]) => result.candidate_id === candidateId,
  );
  if (candidateResults.length > 1) throw new Error("durable finalized result is ambiguous");
  const durableResult = candidateResults[0];
  if (durableResult) {
    if (
      !qaEvidence ||
      !verificationEvidence ||
      candidate.latest_qa?.status !== "COMPLETED" ||
      candidate.verification?.status !== "COMPLETED"
    ) {
      throw new Error("durable finalized result is missing completed QA and verification stages");
    }
    await validateDurableResultEvidence(
      context,
      jobRoot,
      durableResult[0],
      candidate,
      durableResult[1],
    );
  }

  // Phase two: all disk evidence is bound and internally consistent; adoption
  // may now update the registry in gate order.
  await context.recordTranslationRouting(routingEnvelope);
  if (!context.job.candidateId || context.job.status !== "QA") {
    await context.selectCandidate(candidateId);
  }
  if (qaEvidence && candidate.latest_qa && context.job.qaIssueCounts == null) {
    await context.recordQa(durableReportEnvelope(
      context.job.id,
      "run_document_qa",
      candidate.latest_qa,
      qaEvidence,
    ));
  }
  if (TERMINAL_JOB_STATUSES.has(context.job.status)) return true;
  if (
    verificationEvidence &&
    candidate.verification &&
    context.job.qaIssueCounts != null &&
    context.qaBlockingIssues === 0
  ) {
    context.recordVerification(durableReportEnvelope(
      context.job.id,
      "verify_candidate",
      candidate.verification,
      verificationEvidence,
    ));
  }
  if (durableResult && context.verified) {
    await adoptDurableResult(context, durableResult[1], candidate.latest_qa!.report_relpath);
  }
  return true;
}

function parseDurableToolState(
  value: string,
  jobId: string,
): DurableToolState {
  let parsed: unknown;
  try {
    parsed = JSON.parse(value);
  } catch {
    throw new Error("durable tool state is not valid JSON");
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error("durable tool state must be an object");
  }
  const state = parsed as Record<string, unknown>;
  if (
    state.schema_version !== 1 ||
    state.job_id !== jobId ||
    !("source" in state) ||
    !isRecord(state.candidates) ||
    !isRecord(state.results) ||
    !isIsoTimestamp(state.created_at) ||
    !isIsoTimestamp(state.updated_at)
  ) {
    throw new Error("durable tool state identity is invalid");
  }
  const source = state.source === null ? null : parseDurableSource(state.source);
  const candidates: Record<string, DurableCandidateState> = {};
  for (const [candidateId, rawCandidate] of Object.entries(state.candidates)) {
    if (!/^candidate-r[0-3]$/.test(candidateId) || !isRecord(rawCandidate)) {
      throw new Error(`durable candidate state is invalid for ${candidateId}`);
    }
    if (
      rawCandidate.candidate_id !== candidateId ||
      typeof rawCandidate.relpath !== "string" ||
      !isSha256(rawCandidate.sha256) ||
      !Number.isSafeInteger(rawCandidate.size_bytes) ||
      Number(rawCandidate.size_bytes) < 0 ||
      !new Set(["docx", "pptx"]).has(String(rawCandidate.document_format)) ||
      typeof rawCandidate.source_relpath !== "string" ||
      !isSha256(rawCandidate.source_sha256) ||
      !safeLanguage(rawCandidate.source_language) ||
      !safeLanguage(rawCandidate.target_language) ||
      !new Set(["translate_once", "apply_repair", "pretranslated_qa"]).has(
        String(rawCandidate.created_by),
      ) ||
      !(rawCandidate.parent_candidate_id === null ||
        (typeof rawCandidate.parent_candidate_id === "string" &&
          /^candidate-r[0-3]$/.test(rawCandidate.parent_candidate_id))) ||
      !(rawCandidate.pipeline_manifest_relpath == null ||
        typeof rawCandidate.pipeline_manifest_relpath === "string") ||
      !(rawCandidate.pipeline_status == null || typeof rawCandidate.pipeline_status === "string") ||
      !(rawCandidate.request_fingerprint == null || isSha256(rawCandidate.request_fingerprint)) ||
      !isIsoTimestamp(rawCandidate.created_at)
    ) {
      throw new Error(`durable candidate state is invalid for ${candidateId}`);
    }
    const requestedTarget = rawCandidate.requested_target_language;
    const resolvedTarget = rawCandidate.resolved_target_language;
    if (
      !(requestedTarget == null || safeLanguage(requestedTarget)) ||
      !(resolvedTarget == null || safeLanguage(resolvedTarget))
    ) {
      throw new Error(`durable candidate routing state is invalid for ${candidateId}`);
    }
    candidates[candidateId] = {
      candidate_id: candidateId,
      relpath: rawCandidate.relpath,
      sha256: rawCandidate.sha256,
      size_bytes: Number(rawCandidate.size_bytes),
      document_format: rawCandidate.document_format as "docx" | "pptx",
      source_relpath: rawCandidate.source_relpath,
      source_sha256: rawCandidate.source_sha256,
      source_language: rawCandidate.source_language,
      target_language: rawCandidate.target_language,
      ...(requestedTarget != null ? { requested_target_language: requestedTarget } : {}),
      ...(resolvedTarget != null ? { resolved_target_language: resolvedTarget } : {}),
      created_by: rawCandidate.created_by as DurableCandidateState["created_by"],
      parent_candidate_id: rawCandidate.parent_candidate_id,
      pipeline_manifest_relpath: rawCandidate.pipeline_manifest_relpath ?? null,
      pipeline_status: rawCandidate.pipeline_status ?? null,
      request_fingerprint: rawCandidate.request_fingerprint ?? null,
      latest_qa: parseDurableQaState(rawCandidate.latest_qa, `QA/${candidateId}`),
      verification: parseDurableQaState(
        rawCandidate.verification,
        `verification/${candidateId}`,
      ),
      created_at: rawCandidate.created_at,
    };
  }
  const results: Record<string, DurableResultState> = {};
  for (const [name, rawResult] of Object.entries(state.results)) {
    if (!isRecord(rawResult)) continue;
    if (
      typeof rawResult.candidate_id !== "string" ||
      typeof rawResult.relpath !== "string" ||
      !isSha256(rawResult.sha256) ||
      !Number.isSafeInteger(rawResult.size_bytes) ||
      Number(rawResult.size_bytes) < 0 ||
      !isIsoTimestamp(rawResult.finalized_at)
    ) {
      throw new Error(`durable result state is invalid for ${name}`);
    }
    results[name] = {
      candidate_id: rawResult.candidate_id,
      relpath: rawResult.relpath,
      sha256: rawResult.sha256,
      size_bytes: Number(rawResult.size_bytes),
      finalized_at: rawResult.finalized_at,
    };
  }
  return { source, candidates, results };
}

function parseDurableSource(value: unknown): DurableSourceState {
  if (
    !isRecord(value) ||
    typeof value.input_relpath !== "string" ||
    !isSha256(value.input_sha256) ||
    !Number.isSafeInteger(value.size_bytes) ||
    Number(value.size_bytes) < 0 ||
    !new Set(["doc", "docx", "ppt", "pptx"]).has(String(value.source_format))
  ) {
    throw new Error("durable source state is invalid");
  }
  const normalizedValues = [
    value.normalized_format,
    value.normalized_relpath,
    value.normalized_sha256,
  ];
  const normalizedAbsent = normalizedValues.every((item) => item == null);
  const normalizedPresent =
    new Set(["docx", "pptx"]).has(String(value.normalized_format)) &&
    typeof value.normalized_relpath === "string" &&
    isSha256(value.normalized_sha256);
  if (!normalizedAbsent && !normalizedPresent) {
    throw new Error("durable normalized source state is incomplete");
  }
  return {
    input_relpath: value.input_relpath,
    input_sha256: value.input_sha256,
    size_bytes: Number(value.size_bytes),
    source_format: value.source_format as DurableSourceState["source_format"],
    normalized_format: normalizedPresent
      ? value.normalized_format as "docx" | "pptx"
      : null,
    normalized_relpath: normalizedPresent ? value.normalized_relpath as string : null,
    normalized_sha256: normalizedPresent ? value.normalized_sha256 as string : null,
  };
}

function parseDurableQaState(value: unknown, label: string): DurableQaState | null {
  if (value == null) return null;
  if (
    !isRecord(value) ||
    !new Set(["COMPLETED", "NEEDS_REVIEW"]).has(String(value.status)) ||
    !isSha256(value.candidate_sha256) ||
    !isSha256(value.source_sha256) ||
    typeof value.report_relpath !== "string" ||
    !isIssueCounts(value.issue_counts) ||
    !isIsoTimestamp(value.checked_at)
  ) {
    throw new Error(`durable ${label} state is invalid`);
  }
  return {
    status: value.status as DurableQaState["status"],
    candidate_sha256: value.candidate_sha256,
    source_sha256: value.source_sha256,
    report_relpath: value.report_relpath,
    issue_counts: value.issue_counts,
    checked_at: value.checked_at,
  };
}

async function validateDurableCandidateEvidence(
  context: JobExecutionContext,
  jobRoot: string,
  source: DurableSourceState | null,
  candidate: DurableCandidateState,
): Promise<void> {
  if (!source || !source.normalized_format || !source.normalized_relpath || !source.normalized_sha256) {
    throw new Error("durable candidate is missing complete source identity");
  }
  const sourceExtension = path.extname(context.job.sourceFilename).toLowerCase().slice(1);
  const expectedSourceFormat = new Set(["doc", "docx", "ppt", "pptx"]).has(sourceExtension)
    ? sourceExtension as DurableSourceState["source_format"]
    : null;
  const expectedDocumentFormat = expectedSourceFormat === "doc" || expectedSourceFormat === "docx"
    ? "docx"
    : expectedSourceFormat === "ppt" || expectedSourceFormat === "pptx"
      ? "pptx"
      : null;
  if (
    !expectedSourceFormat ||
    !expectedDocumentFormat ||
    source.input_relpath !== context.job.inputRelpath ||
    source.source_format !== expectedSourceFormat ||
    source.normalized_format !== expectedDocumentFormat ||
    candidate.document_format !== expectedDocumentFormat ||
    candidate.source_sha256 !== source.normalized_sha256 ||
    path.posix.extname(candidate.relpath).toLowerCase() !== `.${expectedDocumentFormat}` ||
    path.posix.extname(candidate.source_relpath).toLowerCase() !== `.${expectedDocumentFormat}`
  ) {
    throw new Error("durable source/candidate format identity is invalid");
  }
  await validateDurableFile(
    jobRoot,
    source.input_relpath,
    source.input_sha256,
    source.size_bytes,
    "input source",
  );
  await validateDurableFile(
    jobRoot,
    source.normalized_relpath,
    source.normalized_sha256,
    undefined,
    "normalized source",
  );
  await validateDurableFile(
    jobRoot,
    candidate.source_relpath,
    candidate.source_sha256,
    undefined,
    "candidate source",
  );
  await validateDurableFile(
    jobRoot,
    candidate.relpath,
    candidate.sha256,
    candidate.size_bytes,
    "candidate",
  );

  const round = Number(candidate.candidate_id.slice("candidate-r".length));
  if (context.job.candidateId && round !== context.job.repairRound) {
    throw new Error("durable candidate repair round conflicts with the registered job");
  }
  if (candidate.candidate_id === "candidate-r0") {
    const expectedCreator = context.job.executionMode === "pretranslated_qa"
      ? "pretranslated_qa"
      : "translate_once";
    const hasExpectedPipelineStatus = context.job.executionMode === "pretranslated_qa"
      ? candidate.pipeline_status === "PRETRANSLATED"
      : isRecoverableCandidatePipelineStatus(candidate.pipeline_status);
    if (
      candidate.created_by !== expectedCreator ||
      candidate.parent_candidate_id !== null ||
      !hasExpectedPipelineStatus
    ) {
      throw new Error("durable initial candidate phase identity is invalid");
    }
  } else if (
    candidate.created_by !== "apply_repair" ||
    candidate.parent_candidate_id !== `candidate-r${round - 1}` ||
    !isRecoverableCandidatePipelineStatus(candidate.pipeline_status)
  ) {
    throw new Error("durable repair candidate phase identity is invalid");
  }
}

function isRecoverableCandidatePipelineStatus(status: string | null): boolean {
  return status === "COMPLETED" || status === "NEEDS_REVIEW";
}

function durableRoutingEnvelope(
  context: JobExecutionContext,
  candidate: DurableCandidateState,
): PythonToolEnvelope {
  const source = candidate.source_language.toLowerCase().replaceAll("_", "-");
  const target = candidate.target_language.toLowerCase().replaceAll("_", "-");
  if (
    !/^[a-z]{2,8}(?:-[a-z0-9]{1,8})*$/.test(source) ||
    !/^[a-z]{2,8}(?:-[a-z0-9]{1,8})*$/.test(target) ||
    source === "auto" ||
    target === "auto"
  ) {
    throw new Error("durable candidate language routing is invalid");
  }
  const requestedTarget = (
    context.job.requestedTargetLanguage ?? context.job.targetLanguage
  ).toLowerCase().replaceAll("_", "-");
  const targetPrimary = target.split("-", 1)[0]!;
  const expectedTarget = requestedTarget === "auto"
    ? (source.split("-", 1)[0] === "zh" ? "en" : "zh")
    : requestedTarget.split("-", 1)[0]!;
  const requestedSource = context.job.sourceLanguage.toLowerCase().replaceAll("_", "-");
  if (
    targetPrimary !== expectedTarget ||
    (requestedSource !== "auto" &&
      requestedSource.split("-", 1)[0] !== source.split("-", 1)[0]) ||
    (candidate.requested_target_language != null &&
      candidate.requested_target_language.toLowerCase().replaceAll("_", "-") !== requestedTarget) ||
    (candidate.resolved_target_language != null &&
      candidate.resolved_target_language.toLowerCase().replaceAll("_", "-") !== target) ||
    (context.job.resolvedSourceLanguage != null && context.job.resolvedSourceLanguage !== source) ||
    (context.job.resolvedTargetLanguage != null && context.job.resolvedTargetLanguage !== targetPrimary)
  ) {
    throw new Error("durable candidate language routing conflicts with the registered route");
  }
  return {
    tool: "translate_once",
    job_id: context.job.id,
    status: "COMPLETED",
    summary: {
      requested_target_language: context.job.requestedTargetLanguage ?? context.job.targetLanguage,
      resolved_source_language: source,
      resolved_target_language: target,
    },
    artifacts: [],
    issues: [],
    error: null,
  };
}

async function validateDurableReport(
  jobRoot: string,
  jobId: string,
  candidate: DurableCandidateState,
  record: DurableQaState,
  kind: "qa_report" | "verification_report",
): Promise<DurableReportEvidence> {
  if (
    record.candidate_sha256 !== candidate.sha256 ||
    record.source_sha256 !== candidate.source_sha256
  ) {
    throw new Error(`durable ${kind} digest identity is invalid`);
  }
  const relpath = record.report_relpath;
  const normalized = relpath.replaceAll("\\", "/");
  const expectedPrefix = kind === "qa_report" ? "reports/qa/" : "reports/verification/";
  if (
    normalized !== relpath ||
    !normalized.startsWith(expectedPrefix) ||
    path.posix.extname(normalized).toLowerCase() !== ".json"
  ) {
    throw new Error(`durable ${kind} path is invalid`);
  }
  const reportPath = await resolveExistingContained(jobRoot, relpath);
  const reportStat = await stat(reportPath);
  let payload: unknown;
  try {
    payload = JSON.parse(await readFile(reportPath, "utf8"));
  } catch {
    throw new Error(`durable ${kind} report is not valid JSON`);
  }
  if (!isRecord(payload)) throw new Error(`durable ${kind} report payload is invalid`);
  const issues = validateDurableReportIssues(payload.issues, kind);
  const issueCounts = issueCountsFromReport(issues);
  const expectedStatus = issueCounts.error > 0 ? "NEEDS_REVIEW" : "COMPLETED";
  if (
    payload.job_id !== jobId ||
    payload.candidate_id !== candidate.candidate_id ||
    payload.candidate_sha256 !== candidate.sha256 ||
    payload.source_sha256 !== candidate.source_sha256 ||
    payload.status !== record.status ||
    payload.status !== expectedStatus ||
    !isIssueCounts(payload.issue_counts) ||
    !sameIssueCounts(payload.issue_counts, issueCounts) ||
    !sameIssueCounts(record.issue_counts, issueCounts) ||
    !isRecord(payload.metadata)
  ) {
    throw new Error(`durable ${kind} report identity or issue counts are invalid`);
  }
  if (kind === "qa_report") {
    if (typeof payload.semantic_qa !== "boolean") {
      throw new Error("durable QA report phase payload is invalid");
    }
  } else {
    validateVerificationPackage(payload.package, candidate);
  }
  return {
    artifact: {
      artifact_id: `durable:${kind}`,
      kind,
      relpath,
      filename: path.basename(relpath),
      size_bytes: reportStat.size,
      sha256: await sha256File(reportPath),
    },
    issues,
  };
}

function validateDurableReportIssues(
  value: unknown,
  kind: "qa_report" | "verification_report",
): Array<Record<string, unknown>> {
  if (!Array.isArray(value)) throw new Error(`durable ${kind} report issues are invalid`);
  return value.map((issue, index) => {
    if (
      !isRecord(issue) ||
      typeof issue.code !== "string" || issue.code.length === 0 ||
      typeof issue.message !== "string" || issue.message.length === 0 ||
      !new Set(["error", "warning", "info"]).has(String(issue.severity)) ||
      typeof issue.recoverable !== "boolean" ||
      !isRecord(issue.details) ||
      !(issue.issue_id == null || typeof issue.issue_id === "string") ||
      !(issue.stage == null || typeof issue.stage === "string") ||
      !(issue.block_id == null || typeof issue.block_id === "string") ||
      !(issue.page == null || (Number.isSafeInteger(issue.page) && Number(issue.page) >= 1)) ||
      !(issue.slide == null || (Number.isSafeInteger(issue.slide) && Number(issue.slide) >= 1))
    ) {
      throw new Error(`durable ${kind} report issue ${index} is invalid`);
    }
    return {
      code: issue.code,
      message: issue.message,
      severity: issue.severity,
      ...(issue.stage != null ? { stage: issue.stage } : {}),
      ...(issue.block_id != null ? { block_id: issue.block_id } : {}),
      ...(issue.page != null ? { page: issue.page } : {}),
      ...(issue.slide != null ? { slide: issue.slide } : {}),
      recoverable: issue.recoverable,
    };
  });
}

function validateVerificationPackage(value: unknown, candidate: DurableCandidateState): void {
  if (!isRecord(value)) throw new Error("durable verification report package is invalid");
  const expectedKeys = ["part_count", "required_part", "sha256", "size_bytes", "valid"];
  const expectedRequiredPart = candidate.document_format === "docx"
    ? "word/document.xml"
    : "ppt/presentation.xml";
  if (
    Object.keys(value).sort().join(",") !== expectedKeys.join(",") ||
    value.valid !== true ||
    value.required_part !== expectedRequiredPart ||
    !Number.isSafeInteger(value.part_count) ||
    Number(value.part_count) < 1 ||
    value.size_bytes !== candidate.size_bytes ||
    value.sha256 !== candidate.sha256
  ) {
    throw new Error("durable verification report package identity is invalid");
  }
}

function issueCountsFromReport(issues: Array<Record<string, unknown>>): QaIssueCounts {
  const counts: QaIssueCounts = { error: 0, warning: 0, info: 0 };
  for (const issue of issues) counts[issue.severity as keyof QaIssueCounts] += 1;
  return counts;
}

function durableReportEnvelope(
  jobId: string,
  tool: "run_document_qa" | "verify_candidate",
  record: DurableQaState,
  evidence: DurableReportEvidence,
): PythonToolEnvelope {
  return {
    tool,
    job_id: jobId,
    status: record.status,
    summary: { issue_counts: record.issue_counts },
    artifacts: [evidence.artifact],
    issues: evidence.issues,
    error: null,
  };
}

async function validateDurableResultEvidence(
  context: JobExecutionContext,
  jobRoot: string,
  resultName: string,
  candidate: DurableCandidateState,
  result: DurableResultState,
): Promise<void> {
  const sourceExtension = path.extname(context.job.sourceFilename).toLowerCase();
  const expectedExtension = sourceExtension === ".doc" || sourceExtension === ".docx"
    ? ".docx"
    : sourceExtension === ".ppt" || sourceExtension === ".pptx"
      ? ".pptx"
      : null;
  const normalized = result.relpath.replaceAll("\\", "/");
  if (
    !expectedExtension ||
    normalized !== result.relpath ||
    !normalized.startsWith("results/") ||
    path.posix.extname(normalized).toLowerCase() !== expectedExtension ||
    resultName !== path.posix.basename(normalized) ||
    result.candidate_id !== candidate.candidate_id ||
    result.sha256 !== candidate.sha256 ||
    result.size_bytes !== candidate.size_bytes
  ) {
    throw new Error("durable finalized result identity is invalid");
  }
  const resultPath = await resolveExistingContained(jobRoot, result.relpath);
  const resultStat = await stat(resultPath);
  if (
    resultStat.size !== result.size_bytes ||
    await sha256File(resultPath) !== result.sha256
  ) {
    throw new Error("durable finalized result evidence is invalid");
  }
}

async function adoptDurableResult(
  context: JobExecutionContext,
  result: DurableResultState,
  reportRelpath: string,
): Promise<void> {
  const warnings = [
    ...(context.qaIssues > 0 ? [`qa_issue_count:${context.qaIssues}`] : []),
    ...(context.verificationIssues > 0
      ? [`verification_issue_count:${context.verificationIssues}`]
      : []),
  ];
  await context.transition(
    warnings.length > 0 ? "COMPLETED_WITH_WARNINGS" : "COMPLETED",
    {
      resultRelpath: result.relpath,
      reportRelpath,
      ...(warnings.length > 0 ? { warnings } : {}),
    },
  );
}

async function validateDurableFile(
  jobRoot: string,
  relpath: string,
  expectedSha256: string,
  expectedSize: number | undefined,
  label: string,
): Promise<void> {
  if (!isNormalizedRelativePath(relpath)) throw new Error(`durable ${label} path is invalid`);
  const file = await resolveExistingContained(jobRoot, relpath);
  const fileStat = await stat(file);
  if (
    (expectedSize != null && fileStat.size !== expectedSize) ||
    await sha256File(file) !== expectedSha256
  ) {
    throw new Error(`durable ${label} digest mismatch or size mismatch`);
  }
}

function sameIssueCounts(left: QaIssueCounts, right: QaIssueCounts): boolean {
  return left.error === right.error && left.warning === right.warning && left.info === right.info;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function isSha256(value: unknown): value is string {
  return typeof value === "string" && /^[a-f0-9]{64}$/.test(value);
}

function safeLanguage(value: unknown): value is string {
  return typeof value === "string" && value.length > 0 && value.length <= 64 &&
    /^[A-Za-z][A-Za-z0-9_-]*$/.test(value);
}

function isIsoTimestamp(value: unknown): value is string {
  return typeof value === "string" &&
    /^\d{4}-\d{2}-\d{2}T/.test(value) &&
    Number.isFinite(Date.parse(value));
}

function isNormalizedRelativePath(value: string): boolean {
  if (
    value.length === 0 ||
    value.replaceAll("\\", "/") !== value ||
    path.posix.isAbsolute(value) ||
    value.startsWith("/")
  ) return false;
  const parts = value.split("/");
  return !parts.some((part) => part === "" || part === "." || part === "..") &&
    !parts[0]!.includes(":");
}

function isIssueCounts(value: unknown): value is QaIssueCounts {
  if (!isRecord(value)) return false;
  const names = ["error", "info", "warning"];
  return Object.keys(value).sort().join(",") === names.join(",") &&
    (["error", "warning", "info"] as const).every(
    (name) => Number.isSafeInteger(value[name]) && Number(value[name]) >= 0,
  );
}

async function optionalExistingContained(root: string, relpath: string): Promise<string | null> {
  try {
    return await resolveExistingContained(root, relpath);
  } catch (error) {
    const code = isRecord(error) ? error.code : undefined;
    if (code === "ENOENT") return null;
    throw error;
  }
}

async function sha256File(file: string): Promise<string> {
  const hash = createHash("sha256");
  const stream = createReadStream(file);
  for await (const chunk of stream) hash.update(chunk as Buffer);
  return hash.digest("hex");
}

async function invokeControlledTool(
  tools: ToolDefinition[],
  name: string,
  params: Record<string, unknown>,
  signal: AbortSignal,
): Promise<void> {
  const tool = tools.find((candidate) => candidate.name === name);
  if (!tool) throw new Error(`controlled recovery tool is unavailable: ${name}`);
  await tool.execute(`runner-recovery-${name}`, params, signal, undefined, {} as never);
}

function errorMessage(error: unknown): string {
  return (error instanceof Error ? error.message : "unknown error").slice(0, 200);
}
