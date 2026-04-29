# Rule Promotion Design

## Purpose

`claude-review-pdca` already stores review findings and learned implementation patterns.
This document defines the next layer: promoting repeated or high-value feedback into repo rule documents such as `CLAUDE.md`, `AGENT.md`, `AGENTS.md`, or `CODEX.md` with one human approval step.

The goal is not to write every review comment into rules. The goal is to keep only durable, reusable guidance that prevents future mistakes without bloating the project instructions.

## Scope

In scope:

- Review feedback, user corrections, and repeated findings that may become project rules.
- One-shot HITL approval before writing a rule document.
- Adoption and rejection reason logging.
- Existing-rule deduplication and minimal patch proposal.
- Choosing the correct repo-local rule target.

Out of scope for the first implementation:

- Fully autonomous global rule updates.
- Bulk rollout to every repo.
- Rewriting large rule documents without an explicit user request.
- Treating highly local wording/style comments as reusable rules by default.

## Inputs

Candidate rule promotion can be triggered by:

1. A review outcome item from `record-review-outcome.py`.
2. A direct user correction such as “次からこうして”.
3. A repeated pattern in `review-patterns.db`.
4. A confirmed false-positive learning event.

Each candidate should be normalized to this shape:

```json
{
  "source": "review-outcome | user-correction | learned-pattern | false-positive",
  "repo_root": "C:/path/to/repo",
  "summary": "short candidate rule",
  "evidence": ["why this happened", "where it was observed"],
  "proposed_scope": "file | directory | project | global-candidate",
  "risk": "low | medium | high",
  "adoption_reason": "why this should become a rule",
  "rejection_reason": null
}
```

## Promotion Decision

A candidate should be proposed for rule promotion only when it is:

- Reusable across future work in the same repo.
- More valuable as an instruction than as a one-time finding.
- Specific enough for agents to execute.
- General enough not to encode one-off text/layout preferences.
- Not already covered by an existing rule.
- Safe to apply without changing project intent.

Reject or keep as a DB pattern when it is:

- A one-off local edit preference.
- A transient bug caused by a temporary implementation detail.
- A business/product judgment that still needs user decision.
- A duplicate of an existing rule.
- Too vague for an agent to act on.
- Better handled by tests, lint, or code structure than by instructions.

## Rule Target Resolver

The tool must not blindly create or edit `CLAUDE.md`.

Resolution order:

1. Detect repo root from explicit input, current git root, or payload `repo_root`.
2. Search repo root only for known rule docs:
   - `CLAUDE.md`
   - `AGENTS.md`
   - `AGENT.md`
   - `CODEX.md`
3. If exactly one repo master rule is obvious, use it.
4. If both Claude and Codex/Agent rule docs exist, compare contents before proposing:
   - If one clearly points to the other as canonical, edit the canonical file.
   - If they contain different active rule sets, propose the smaller safe patch and explain the target choice.
5. If no clear target exists, do not write. Output a proposal only.
6. Never write to home/global rule files from project promotion unless the user explicitly approves global promotion.

## HITL Contract

Before writing a rule document, show the user one compact approval block:

```md
## Rule promotion proposal

Target: path/to/CLAUDE.md
Action: add | modify | skip
Scope: project

Proposed rule:
- ...

Adoption reason:
- ...

Existing-rule check:
- Similar rule found: yes/no
- Merge strategy: append | refine existing | skip duplicate

Diff:
```diff
...
```

Approve? yes/no
```

The user should not be asked multiple exploratory questions by default.
If the candidate cannot be judged from available context, choose `skip/proposal-only` rather than asking 2-3 extra questions.

## Adoption / Rejection Logging

Every decision should be logged, including skipped candidates.

Default repo-local log path:

- `.review-pdca-rule-promotions.jsonl`

Minimum fields:

- timestamp
- repo_root
- target_doc
- source
- candidate_summary
- decision: adopted | rejected | proposal-only
- adoption_reason
- rejection_reason
- existing_rule_refs
- user_approved: true | false

This log is used to improve the classifier and to audit bias. Adoption reasons are as important as rejection reasons.

## Third-Party / Second-Pass Review

When token/runtime budget allows, a second-pass reviewer should inspect:

- Whether the candidate is too local.
- Whether an existing rule already covers it.
- Whether the proposed rule would bloat the document.
- Whether the adoption/rejection reason is faithful to the evidence.

This is advisory only. The final write still requires the user approval mode configured for the project.

## Safety Defaults

Initial mode:

- HITL required for every rule-document write.
- No automatic global rule writes.
- No autonomous switch from HITL to no-HITL.
- If uncertain, output proposal-only.
- Prefer editing an existing rule over appending a near-duplicate.

Future no-HITL mode can be enabled only by explicit user decision.

## Relationship to Existing DBs

- `review-feedback.db`: unresolved or pending findings.
- `review-patterns.db`: file/project implementation patterns used for injection.
- rule-promotion log: governance/audit layer for durable instruction changes.

Pattern DB entries do not automatically become rules. Rule promotion is a separate governance step.

## Implementation Plan

Phase 1:

1. Add this design document.
2. Add a target resolver design/test plan.
3. Tighten unsafe pattern recording so judgment-required items do not contaminate learned patterns.

Phase 2:

1. Implement `scripts/propose-rule-update.py`.
2. Generate proposal-only diffs.
3. Add tests for target resolution, deduplication, and HITL output.

Phase 3:

1. Add approval/apply mode.
2. Add adoption/rejection log storage.
3. Connect review outcome producer to rule-promotion proposal generation.

Producer connection:

- `record-review-outcome.py --propose-rules` may route explicit `rule_candidate` / `rule-promotion-candidate` items into `propose-rule-update.py`.
- Rule candidates require high confidence, a non-empty adoption reason, and `needs_judgment=false`.
- Rule proposal generation remains opt-in and proposal/log only. It must not imply apply approval.

Apply mode safety:

- Applying a proposal requires both an explicit apply flag and explicit user approval flag.
- Only `proposal-ready` + `add` proposals can be written.
- Duplicate-suspected and proposal-only states must not write to rule docs.
- Applied proposals must write an `adopted` audit log entry with `user_approved=true`.

Phase 4:

1. Add second-pass reviewer hook.
2. Add periodic audit command for adopted/rejected reasoning drift.
3. Consider explicit global-promotion proposals, still user-approved.
