# OfficeGuard Public Agent Eval

This directory contains the public, deterministic evaluation contract for the
three orchestration policies used by the project:

- `no_repair`: inspect and select `candidate-r0` only.
- `fixed_two_round`: select the last available candidate up to `candidate-r2`,
  even when an earlier round passed or a later round regressed.
- `bounded_agent`: stop on a passing candidate; otherwise accept only a strict
  blocker reduction with no new regression, up to the scenario repair budget.

## Corpus

[`corpus/manifest.json`](corpus/manifest.json) freezes 20 hand-authored
synthetic policy scenarios: 10 DOCX and 10 PPTX. They cover direct release,
one- and two-round repair, early pass followed by regression, no improvement,
worsening repair, budget exhaustion, and deliberately injected QA false
negatives. Every scenario and generated artifact is released under CC0-1.0;
see [`corpus/LICENSE.md`](corpus/LICENSE.md).

The repository does not store binary Office fixtures. The Python generator
creates deterministic OOXML carrier files at runtime from the manifest. Their
hashes are recorded in `fixture-index.json`.

## Evidence boundary

This is an **offline orchestration-policy replay**, not a live-model or native
Office quality benchmark:

- `pi_session=false` and `model_calls=0` for every scenario and strategy;
- semantic/layout oracle labels and blocker trajectories are frozen synthetic
  annotations from the manifest;
- generated OOXML files are deterministic carrier artifacts and are not
  independently passed through the native Office QA engine in this eval;
- `replay_latency_ms` is a deterministic fixture value, not measured wall-clock
  latency;
- token counts and estimated model cost are therefore exactly zero.

The separate P1-A demo exercises native Python QA on its documented fixture
paths. A future live-model eval must use a different evidence label and must not
merge its results with this baseline.

## Reproduce

From `translation_core/pi_agent`:

```powershell
npm run build
node dist/src/eval-cli.js
```

An optional first positional argument changes the output directory:

```powershell
node dist/src/eval-cli.js D:\tmp\officeguard-public-eval
```

The command generates:

```text
<output>/eval-summary.json
<output>/eval-report.md
<output>/corpus/fixture-index.json
<output>/corpus/fixtures/**/*.docx|pptx
```

`eval-summary.json` and `eval-report.md` are byte-stable for the same manifest
and evaluator version; neither contains timestamps or machine paths. Both the
summary and generated fixture index bind the exact manifest with SHA-256.

The checked-in v1.0.0 baseline is available as
[`eval-summary.json`](baselines/v1.0.0/eval-summary.json) and
[`eval-report.md`](baselines/v1.0.0/eval-report.md).

## Metric contract

| Metric | Definition |
| --- | --- |
| Release coverage | selected candidates with zero observed blockers / all scenarios |
| Conditional risk | released candidates failing a frozen semantic or layout oracle / released candidates; `null` when nothing is released |
| Semantic/layout pass rate | selected candidates passing the corresponding frozen oracle / all scenarios |
| Repair uplift | initially blocked scenarios ending released / initially blocked scenarios |
| New-regression rate | selected candidates annotated with one or more new regressions / all scenarios |
| Repair attempts | repair candidates inspected, including a candidate rejected by the bounded policy |
| Replay P50/P95 | nearest-rank percentile of deterministic cumulative fixture latency |

The frozen v1.0.0 baseline intentionally exposes a tradeoff: the fixed workflow
has higher raw release coverage, while the bounded policy has lower conditional
risk, performs fewer repair attempts, and selects no annotated new regression.
The report should be read as a policy-behavior comparison, not as a claim that
synthetic scores predict real-world translation accuracy.
