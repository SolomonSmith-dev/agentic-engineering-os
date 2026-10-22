---
name: requirement-tracing
description: Map each acceptance criterion to the diff hunks and tests that satisfy it; report the ones that do not map.
---

# Requirement tracing

For each acceptance criterion id:

1. Find the diff hunks (file and line range) that implement it. None found: finding `requirement_unmapped`, severity blocker.
2. Find the test that fails without those hunks and passes with them. None found: finding, severity major.
3. Record the triple (criterion id, hunks, test) in the review output so the verdict is checkable by a human.
