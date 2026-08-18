# OfficeGuard Public Agent Eval

Eval contract: `1.0.0`; corpus: `officeguard-public-policy-eval@1.0.0`.
Manifest SHA-256: `86d84cfeba1aff850d840654fb143953cf8b681fa6e69fd0edd5761d0ec8dcd4`.

Evidence boundary: this is an offline orchestration-policy replay with `pi_session=false` and `model_calls=0`; it is not a live-model quality evaluation.
The CC0 OOXML files are runtime-generated carrier artifacts, and the QA/oracle labels are frozen synthetic annotations: this is not native Office QA evidence. Replay latency is a deterministic fixture value, not measured wall-clock latency.

| Strategy | Release coverage | Conditional risk | Repair uplift | New regression rate | Avg repair attempts | Replay P50/P95 (ms) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| no_repair | 20.0% | 50.0% | 0.0% | 0.0% | 0.00 | 8/10 |
| fixed_two_round | 70.0% | 35.7% | 62.5% | 10.0% | 1.20 | 13/25 |
| bounded_agent | 55.0% | 27.3% | 43.8% | 0.0% | 1.00 | 13/25 |

| Strategy | Scenario | Selected candidate | Outcome | Repair attempts |
| --- | --- | --- | --- | ---: |
| no_repair | early-pass-then-regression-pptx | candidate-r0 | NEEDS_REVIEW | 0 |
| no_repair | direct-safe-docx | candidate-r0 | RELEASED | 0 |
| no_repair | gradual-safe-repair-pptx | candidate-r0 | NEEDS_REVIEW | 0 |
| no_repair | irreducible-risk-pptx | candidate-r0 | NEEDS_REVIEW | 0 |
| no_repair | regression-guard-docx | candidate-r0 | NEEDS_REVIEW | 0 |
| no_repair | qa-false-negative-docx | candidate-r0 | RELEASED | 0 |
| no_repair | one-round-repair-docx | candidate-r0 | NEEDS_REVIEW | 0 |
| no_repair | direct-safe-pptx | candidate-r0 | RELEASED | 0 |
| no_repair | direct-layout-risk-pptx | candidate-r0 | NEEDS_REVIEW | 0 |
| no_repair | direct-semantic-risk-docx | candidate-r0 | NEEDS_REVIEW | 0 |
| no_repair | single-improvement-still-blocked-docx | candidate-r0 | NEEDS_REVIEW | 0 |
| no_repair | single-safe-repair-pptx | candidate-r0 | NEEDS_REVIEW | 0 |
| no_repair | two-round-semantic-repair-docx | candidate-r0 | NEEDS_REVIEW | 0 |
| no_repair | two-round-layout-repair-pptx | candidate-r0 | NEEDS_REVIEW | 0 |
| no_repair | late-regression-pptx | candidate-r0 | NEEDS_REVIEW | 0 |
| no_repair | no-improvement-stop-docx | candidate-r0 | NEEDS_REVIEW | 0 |
| no_repair | worsening-stop-pptx | candidate-r0 | NEEDS_REVIEW | 0 |
| no_repair | false-negative-after-repair-docx | candidate-r0 | NEEDS_REVIEW | 0 |
| no_repair | false-negative-layout-pptx | candidate-r0 | RELEASED | 0 |
| no_repair | budget-exhausted-docx | candidate-r0 | NEEDS_REVIEW | 0 |
| fixed_two_round | early-pass-then-regression-pptx | candidate-r2 | NEEDS_REVIEW | 2 |
| fixed_two_round | direct-safe-docx | candidate-r0 | RELEASED | 0 |
| fixed_two_round | gradual-safe-repair-pptx | candidate-r2 | RELEASED | 2 |
| fixed_two_round | irreducible-risk-pptx | candidate-r2 | NEEDS_REVIEW | 2 |
| fixed_two_round | regression-guard-docx | candidate-r2 | RELEASED | 2 |
| fixed_two_round | qa-false-negative-docx | candidate-r0 | RELEASED | 0 |
| fixed_two_round | one-round-repair-docx | candidate-r1 | RELEASED | 1 |
| fixed_two_round | direct-safe-pptx | candidate-r0 | RELEASED | 0 |
| fixed_two_round | direct-layout-risk-pptx | candidate-r0 | NEEDS_REVIEW | 0 |
| fixed_two_round | direct-semantic-risk-docx | candidate-r0 | NEEDS_REVIEW | 0 |
| fixed_two_round | single-improvement-still-blocked-docx | candidate-r1 | NEEDS_REVIEW | 1 |
| fixed_two_round | single-safe-repair-pptx | candidate-r1 | RELEASED | 1 |
| fixed_two_round | two-round-semantic-repair-docx | candidate-r2 | RELEASED | 2 |
| fixed_two_round | two-round-layout-repair-pptx | candidate-r2 | RELEASED | 2 |
| fixed_two_round | late-regression-pptx | candidate-r2 | RELEASED | 2 |
| fixed_two_round | no-improvement-stop-docx | candidate-r2 | RELEASED | 2 |
| fixed_two_round | worsening-stop-pptx | candidate-r2 | RELEASED | 2 |
| fixed_two_round | false-negative-after-repair-docx | candidate-r1 | RELEASED | 1 |
| fixed_two_round | false-negative-layout-pptx | candidate-r0 | RELEASED | 0 |
| fixed_two_round | budget-exhausted-docx | candidate-r2 | NEEDS_REVIEW | 2 |
| bounded_agent | early-pass-then-regression-pptx | candidate-r1 | RELEASED | 1 |
| bounded_agent | direct-safe-docx | candidate-r0 | RELEASED | 0 |
| bounded_agent | gradual-safe-repair-pptx | candidate-r2 | RELEASED | 2 |
| bounded_agent | irreducible-risk-pptx | candidate-r2 | NEEDS_REVIEW | 2 |
| bounded_agent | regression-guard-docx | candidate-r0 | NEEDS_REVIEW | 1 |
| bounded_agent | qa-false-negative-docx | candidate-r0 | RELEASED | 0 |
| bounded_agent | one-round-repair-docx | candidate-r1 | RELEASED | 1 |
| bounded_agent | direct-safe-pptx | candidate-r0 | RELEASED | 0 |
| bounded_agent | direct-layout-risk-pptx | candidate-r0 | NEEDS_REVIEW | 0 |
| bounded_agent | direct-semantic-risk-docx | candidate-r0 | NEEDS_REVIEW | 0 |
| bounded_agent | single-improvement-still-blocked-docx | candidate-r1 | NEEDS_REVIEW | 1 |
| bounded_agent | single-safe-repair-pptx | candidate-r1 | RELEASED | 1 |
| bounded_agent | two-round-semantic-repair-docx | candidate-r2 | RELEASED | 2 |
| bounded_agent | two-round-layout-repair-pptx | candidate-r2 | RELEASED | 2 |
| bounded_agent | late-regression-pptx | candidate-r1 | NEEDS_REVIEW | 2 |
| bounded_agent | no-improvement-stop-docx | candidate-r0 | NEEDS_REVIEW | 1 |
| bounded_agent | worsening-stop-pptx | candidate-r0 | NEEDS_REVIEW | 1 |
| bounded_agent | false-negative-after-repair-docx | candidate-r1 | RELEASED | 1 |
| bounded_agent | false-negative-layout-pptx | candidate-r0 | RELEASED | 0 |
| bounded_agent | budget-exhausted-docx | candidate-r2 | NEEDS_REVIEW | 2 |
