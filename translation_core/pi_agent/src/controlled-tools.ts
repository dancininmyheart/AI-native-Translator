import { Type } from "typebox";
import { defineTool, type ToolDefinition } from "@earendil-works/pi-coding-agent";
import type { PythonToolEnvelope, PythonToolName } from "./contracts.js";
import type { JobExecutionContext } from "./execution-context.js";
import type { SafeEventLog } from "./event-log.js";
import type { PythonToolBridge } from "./python-tool-bridge.js";
import type { ProviderLimiter } from "./provider-limiter.js";

const Candidate = Type.String({ pattern: "^candidate-r[0-3]$", description: "Opaque candidate identifier" });

function toToolResult(envelope: PythonToolEnvelope) {
  return {
    content: [{ type: "text" as const, text: JSON.stringify(envelope) }],
    details: {
      status: envelope.status,
      issueCount: envelope.issues.length,
      artifactKinds: envelope.artifacts.map((artifact) => artifact.kind),
    },
  };
}

function failed(envelope: PythonToolEnvelope): boolean {
  return envelope.error !== null || new Set(["FAILED", "ERROR"]).has(envelope.status.toUpperCase());
}

export function createControlledTools(
  context: JobExecutionContext,
  bridge: PythonToolBridge,
  events: SafeEventLog,
  providerLimiter: ProviderLimiter,
): ToolDefinition[] {
  async function invoke(
    tool: PythonToolName,
    payload: Record<string, unknown>,
    signal?: AbortSignal,
    allowFailedEnvelope = false,
  ) {
    await events.append(context.job.id, { at: new Date().toISOString(), type: "tool_start", tool });
    const envelope = await bridge.execute(tool, context.job.id, payload, signal);
    await events.append(context.job.id, {
      at: new Date().toISOString(),
      type: "tool_end",
      tool,
      outcome: envelope.status,
      ...(envelope.error?.code ? { code: envelope.error.code } : {}),
    });
    if (failed(envelope) && !allowFailedEnvelope) {
      throw new Error(`${tool} failed: ${envelope.error?.code ?? envelope.status}`);
    }
    return envelope;
  }

  const preflight = defineTool({
    name: "preflight_document",
    label: "Preflight document",
    description: "Validate the registered Word or PowerPoint input and convert legacy DOC/PPT when necessary.",
    parameters: Type.Object({}, { additionalProperties: false }),
    execute: async (_id, _params, signal) => {
      if (context.job.status !== "PREFLIGHT") throw new Error("preflight is not allowed in the current state");
      const payload: Record<string, unknown> = { input_relpath: context.job.inputRelpath };
      if (context.job.executionMode === "pretranslated_qa") {
        if (!context.job.existingTranslationRelpath) throw new Error("pretranslated candidate is missing");
        payload.existing_translation_relpath = context.job.existingTranslationRelpath;
        payload.candidate_id = "candidate-r0";
        payload.source_language = context.job.sourceLanguage;
        payload.target_language = context.job.targetLanguage;
      }
      const envelope = await invoke("preflight_document", payload, signal);
      if (context.job.executionMode === "pretranslated_qa") {
        await context.selectCandidate("candidate-r0");
      }
      return toToolResult(envelope);
    },
  });

  const translate = defineTool({
    name: "translate_once",
    label: "Translate once",
    description: "Create the immutable initial translated candidate using the job's fixed language and provider settings.",
    parameters: Type.Object({}, { additionalProperties: false }),
    execute: async (_id, _params, signal) => {
      if (context.job.executionMode !== "pi_agent_live") {
        throw new Error("translate_once is forbidden in pretranslated_qa mode");
      }
      if (context.job.status !== "PREFLIGHT") throw new Error("translation is not allowed before preflight");
      await context.transition("TRANSLATING");
      const provider = context.config.fixtureMode ? { kind: "fixture", fixture_relpath: "fixture.json" } : { kind: "openai_compatible" };
      const lease = await providerLimiter.acquire(context.config.translationConcurrency, signal);
      const envelope = await invoke(
        "translate_once",
        {
          input_relpath: context.job.inputRelpath,
          candidate_id: "candidate-r0",
          source_language: context.job.sourceLanguage,
          target_language: context.job.targetLanguage,
          provider,
          layout_mode: context.job.qualityProfile === "strict" ? "strict" : "balanced",
          max_translation_concurrency: context.config.translationConcurrency,
        },
        signal,
      ).finally(() => lease.release());
      await context.recordTranslationRouting(envelope);
      await context.selectCandidate("candidate-r0");
      return toToolResult(envelope);
    },
  });

  const qa = defineTool({
    name: "run_document_qa",
    label: "Run document QA",
    description: "Run semantic, structural, and layout QA against the current immutable candidate.",
    parameters: Type.Object({ candidate_id: Candidate }, { additionalProperties: false }),
    execute: async (_id, params, signal) => {
      if (context.job.status !== "QA" || params.candidate_id !== context.job.candidateId) {
        throw new Error("QA candidate or state is invalid");
      }
      const semanticQa = context.config.semanticQaEnabled && context.job.qualityProfile !== "fast";
      const lease = semanticQa
        ? await providerLimiter.acquire(context.config.translationConcurrency, signal)
        : null;
      const envelope = await invoke(
        "run_document_qa",
        {
          candidate_id: params.candidate_id,
          rendered_layout_required: context.job.qualityProfile === "strict",
          semantic_qa: semanticQa,
          max_processing_concurrency: context.config.translationConcurrency,
        },
        signal,
      ).finally(() => lease?.release());
      await context.recordQa(envelope);
      return toToolResult(envelope);
    },
  });

  const repair = defineTool({
    name: "apply_repair",
    label: "Apply repair",
    description: "Apply one whitelisted deterministic repair round to the current candidate.",
    parameters: Type.Object({ candidate_id: Candidate }, { additionalProperties: false }),
    execute: async (_id, params, signal) => {
      if (context.job.status !== "QA" || params.candidate_id !== context.job.candidateId) {
        throw new Error("repair candidate or state is invalid");
      }
      if (context.softExpired) throw new Error("soft deadline reached; no new repair may start");
      if (context.qaIssues < 1) throw new Error("repair requires QA issues");
      if (context.job.repairRound >= context.job.maxRepairRounds || context.job.repairRound >= context.config.hardMaxRepairRounds) {
        throw new Error("repair round limit reached");
      }
      const nextRound = context.job.repairRound + 1;
      const outputCandidate = `candidate-r${nextRound}`;
      const processingRepair =
        context.config.processingRepairEnabled && context.job.qualityProfile !== "fast";
      await context.transition("REPAIRING");
      const lease = processingRepair ? await providerLimiter.acquire(1, signal) : null;
      const envelope = await invoke(
        "apply_repair",
        {
          candidate_id: params.candidate_id,
          output_candidate_id: outputCandidate,
          max_rounds: 1,
          rendered_layout_required: context.job.qualityProfile === "strict",
          use_processing_provider: processingRepair,
        },
        signal,
        true,
      ).finally(() => lease?.release());
      const acceptedCandidate =
        !failed(envelope) &&
        envelope.summary.output_created === true &&
        envelope.summary.output_candidate_id === outputCandidate &&
        envelope.artifacts.some(
          (artifact) =>
            artifact.kind === "candidate_document" &&
            artifact.artifact_id === `candidate:${outputCandidate}`,
        );
      if (!acceptedCandidate) {
        const repairCode = envelope.error?.code === "REPAIR_NOT_IMPROVED"
          ? "repair_not_improved"
          : envelope.error?.code === "REPAIR_AGENT_FAILED"
            ? "repair_agent_failed"
            : "repair_no_candidate";
        const repairSummary = envelope.error?.message?.trim() ||
          `Repair did not produce an accepted ${outputCandidate}`;
        await context.markNeedsReview(
          repairCode,
          `${repairSummary}; retained ${params.candidate_id}`,
        );
        return toToolResult(envelope);
      }
      await context.selectCandidate(outputCandidate, { repairRound: nextRound });
      return toToolResult(envelope);
    },
  });

  const verify = defineTool({
    name: "verify_candidate",
    label: "Verify candidate",
    description: "Perform the final package, regression, and openability gate for the selected candidate.",
    parameters: Type.Object({ candidate_id: Candidate }, { additionalProperties: false }),
    execute: async (_id, params, signal) => {
      if (context.job.status !== "QA" || params.candidate_id !== context.job.candidateId) {
        throw new Error("verification candidate or state is invalid");
      }
      if (context.qaBlockingIssues > 0) {
        throw new Error("verification requires full QA without blocking issues");
      }
      await context.transition("VERIFYING");
      const envelope = await invoke(
        "verify_candidate",
        {
          candidate_id: params.candidate_id,
          rendered_layout_required: context.job.qualityProfile === "strict",
        },
        signal,
      );
      context.recordVerification(envelope);
      await context.transition("QA");
      return toToolResult(envelope);
    },
  });

  const finalize = defineTool({
    name: "finalize_result",
    label: "Finalize result",
    description: "Publish a verified DOCX/PPTX candidate and its QA report.",
    parameters: Type.Object({ candidate_id: Candidate }, { additionalProperties: false }),
    execute: async (_id, params, signal) => {
      if (
        context.job.status !== "QA" ||
        params.candidate_id !== context.job.candidateId ||
        !context.verified ||
        context.qaBlockingIssues > 0
      ) {
        throw new Error("only the current verified candidate can be finalized");
      }
      const envelope = await invoke(
        "finalize_result",
        { candidate_id: params.candidate_id, result_basename: "translated" },
        signal,
      );
      const resultRelpath = artifactRelpath(envelope, ["translated_document"]);
      const reportRelpath = context.reportRelpath;
      if (!resultRelpath || !reportRelpath) throw new Error("finalize_result did not return result and report artifacts");
      const warnings = [
        ...(context.qaIssues > 0 ? [`qa_issue_count:${context.qaIssues}`] : []),
        ...(context.verificationIssues > 0
          ? [`verification_issue_count:${context.verificationIssues}`]
          : []),
      ];
      const status = warnings.length > 0 ? "COMPLETED_WITH_WARNINGS" : "COMPLETED";
      await context.transition(status, {
        resultRelpath,
        reportRelpath,
        ...(warnings.length > 0 ? { warnings } : {}),
      });
      return toToolResult(envelope);
    },
  });

  return [preflight, translate, qa, repair, verify, finalize];
}

function artifactRelpath(envelope: PythonToolEnvelope, kinds: string[]): string | undefined {
  return envelope.artifacts.find((artifact) => kinds.includes(artifact.kind))?.relpath;
}
