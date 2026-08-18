import { MAX_PUBLIC_TRACE_BYTES, parsePublicTraceJson } from "./trace-schema.mjs";
import { MISSING_LABEL, buildTraceViewModel } from "./render-model.mjs";

const exampleSelect = requireElement("example-select");
const loadButton = requireElement("load-example");
const fileInput = requireElement("trace-file");
const statusRegion = requireElement("load-status");
const traceRoot = requireElement("trace-root");

loadButton.addEventListener("click", () => void loadBundledExample(true));
exampleSelect.addEventListener("change", () => void loadBundledExample(true));
fileInput.addEventListener("change", () => void loadLocalFile());

void loadBundledExample(false);

async function loadBundledExample(moveFocus) {
  setStatus("Loading bundled trace…", false);
  try {
    const filename = String(exampleSelect.value);
    if (!/^p1a-[a-z-]+\.json$/.test(filename)) throw new Error("Bundled trace selection is invalid");
    const response = await fetch(`./examples/${filename}`, { cache: "no-store" });
    if (!response.ok) throw new Error(`Bundled trace request failed (HTTP ${response.status})`);
    const contentLength = Number(response.headers.get("content-length") ?? "0");
    if (contentLength > MAX_PUBLIC_TRACE_BYTES) throw new Error("Trace exceeds the 1 MiB viewer limit");
    displayTrace(parsePublicTraceJson(await response.text()), moveFocus);
    setStatus(`Loaded ${filename}`, false);
  } catch (error) {
    showError(error);
  }
}

async function loadLocalFile() {
  const file = fileInput.files?.[0];
  if (!file) return;
  setStatus(`Validating ${file.name}…`, false);
  try {
    if (file.size > MAX_PUBLIC_TRACE_BYTES) throw new Error("Trace exceeds the 1 MiB viewer limit");
    displayTrace(parsePublicTraceJson(await file.text()), true);
    setStatus(`Loaded ${file.name} locally; no data was uploaded`, false);
  } catch (error) {
    showError(error);
  }
}

function displayTrace(trace, moveFocus) {
  const model = buildTraceViewModel(trace);
  const fragment = document.createDocumentFragment();
  fragment.append(summaryPanel(model));

  const metrics = panel("Recorded usage", "Measured values only; unavailable fields stay explicit.");
  metrics.body.append(metricGrid([
    ["Input tokens", model.metrics.inputTokens],
    ["Output tokens", model.metrics.outputTokens],
    ["Total tokens", model.metrics.totalTokens],
    ["Cost (USD)", model.metrics.costUsd],
    ["End-to-end latency", model.metrics.latencyMs],
  ]));
  fragment.append(metrics.section);

  const states = panel("State timeline", model.evidence.ordering);
  states.body.append(stateTimeline(model.states));
  fragment.append(states.section);

  const tools = panel("Controlled tool calls", "Only allowlisted tool names and bounded outcomes are public.");
  tools.body.append(dataTable(
    "Tool call stream",
    ["#", "Tool", "Outcome", "Duration"],
    model.tools.map((tool) => [tool.sequence, tool.tool, tool.outcome, tool.durationMs]),
    "No tool calls were recorded.",
  ));
  fragment.append(tools.section);

  const qa = panel("QA evidence", "Blockers and quality snapshots across bounded repair rounds.");
  qa.body.append(dataTable(
    "QA snapshot stream",
    ["#", "Blockers", "Quality", "Issue counts"],
    model.qaSnapshots.map((snapshot) => [
      snapshot.sequence,
      snapshot.blockingCount,
      snapshot.qualityScore,
      snapshot.issueCounts,
    ]),
    "No QA snapshots were recorded.",
  ));
  fragment.append(qa.section);

  const candidates = panel("Candidate integrity", "SHA-256 binds the selected candidate to release evidence.");
  candidates.body.append(dataTable(
    "Candidate stream",
    ["#", "Candidate", "SHA-256", "Selected", "Released"],
    model.candidates.map((candidate) => [
      candidate.sequence,
      candidate.candidateId,
      candidate.sha256,
      candidate.selected,
      candidate.released,
    ]),
    "No candidates were recorded.",
    new Set([2]),
  ));
  fragment.append(candidates.section);

  traceRoot.replaceChildren(fragment);
  if (moveFocus) requireElement("trace-heading").focus();
}

function summaryPanel(model) {
  const section = element("section", "summary-card");
  const headingRow = element("div", "summary-card__heading");
  const titleBlock = element("div");
  titleBlock.append(element("p", "section-kicker", "Validated schema v1"));
  const heading = element("h2", "summary-card__title", model.traceId);
  heading.id = "trace-heading";
  heading.tabIndex = -1;
  titleBlock.append(heading, element("p", "summary-card__time", `Projected ${model.generatedAt}`));
  const badge = element("span", `status status--${model.terminal.status.toLowerCase()}`, model.terminal.status);
  headingRow.append(titleBlock, badge);
  section.append(headingRow);

  const facts = element("dl", "facts");
  for (const [term, value] of [
    ["Document", model.documentFormat],
    ["Evidence", model.evidence.kind],
    ["Pi session", model.evidence.piSession],
    ["Model calls", model.evidence.modelCalls],
    ["Result HTTP", model.terminal.httpStatus],
    ["Terminal reason", model.terminal.reason],
  ]) {
    const item = element("div", "fact");
    item.append(element("dt", "fact__term", term), element("dd", "fact__value", value));
    facts.append(item);
  }
  section.append(facts);
  return section;
}

function panel(title, description) {
  const section = element("section", "panel");
  const header = element("div", "panel__header");
  header.append(element("h2", "panel__title", title), element("p", "panel__description", description));
  const body = element("div", "panel__body");
  section.append(header, body);
  return { section, body };
}

function metricGrid(metrics) {
  const list = element("dl", "metric-grid");
  for (const [label, value] of metrics) {
    const item = element("div", `metric${value === MISSING_LABEL ? " metric--missing" : ""}`);
    item.append(element("dt", "metric__label", label), element("dd", "metric__value", value));
    list.append(item);
  }
  return list;
}

function stateTimeline(states) {
  const list = element("ol", "timeline");
  for (const state of states) {
    const item = element("li", "timeline__item");
    item.append(
      element("span", "timeline__number", state.sequence),
      element("strong", "timeline__status", state.status),
      element("span", "timeline__metric", `Elapsed: ${state.elapsedMs}`),
    );
    list.append(item);
  }
  return list;
}

function dataTable(captionText, headers, rows, emptyText, codeColumns = new Set()) {
  if (rows.length === 0) return element("p", "empty", emptyText);
  const wrapper = element("div", "table-wrap");
  const table = element("table", "data-table");
  table.append(element("caption", "sr-only", captionText));
  const head = element("thead");
  const headerRow = element("tr");
  for (const header of headers) {
    const cell = element("th", undefined, header);
    cell.scope = "col";
    headerRow.append(cell);
  }
  head.append(headerRow);
  const body = element("tbody");
  for (const row of rows) {
    const tableRow = element("tr");
    row.forEach((value, index) => {
      const cell = element("td");
      if (codeColumns.has(index)) cell.append(element("code", "hash", value));
      else cell.textContent = value;
      tableRow.append(cell);
    });
    body.append(tableRow);
  }
  table.append(head, body);
  wrapper.append(table);
  return wrapper;
}

function element(tagName, className, text) {
  const node = document.createElement(tagName);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = String(text);
  return node;
}

function setStatus(message, isError) {
  statusRegion.textContent = message;
  statusRegion.classList.toggle("load-status--error", isError);
}

function showError(error) {
  traceRoot.replaceChildren();
  const message = error instanceof Error ? error.message : "Unable to load trace";
  setStatus(`Trace rejected: ${message}`, true);
}

function requireElement(id) {
  const node = document.getElementById(id);
  if (!node) throw new Error(`Viewer element #${id} is missing`);
  return node;
}
