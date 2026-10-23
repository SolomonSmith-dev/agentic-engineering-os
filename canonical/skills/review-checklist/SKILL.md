---
name: review-checklist
description: Ordered checklist the independent reviewer walks before issuing a verdict on a plan or diff.
---

# Review checklist

Walk every item. Record a finding for each item that fails; do not stop at the first.

1. Requirements: every acceptance criterion in the plan has an id. If the plan has none, the single synthetic criterion is "the reproduction command passes".
2. Tracing: each criterion maps to at least one diff hunk and one test (see requirement-tracing).
3. Tests: the orchestrator-run test output is green for the tests named in step 2. A test that cannot fail is not a test.
4. Scope: the diff touches nothing the plan does not name. Unplanned changes are a major finding.
5. Security: no secrets in the diff, no new network egress, no writes outside the worktree. Unresolved items escalate.
6. Lint and types: the orchestrator's lint and type-check output is clean, or the run record says the gate was skipped.
7. Verdict: `approve` only when steps 1 to 6 pass with no blocker or major finding.
