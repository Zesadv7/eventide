---
name: code-review
description: Review an existing code change for reproducible correctness, security, and regression risks. Use for review requests; do not edit files unless the user separately requests fixes.
metadata:
  version: '1'
---

# Review a change

1. Read the requested diff and applicable project instructions. Keep the review inside the user's scope.
2. Trace changed behavior through callers and tests. Load `references/evidence.md` when preparing findings.
3. Report only actionable issues supported by a concrete execution path, with file and line references.
4. Distinguish observed failures from hypotheses. State untested assumptions and missing runtime evidence.
5. If no supported findings remain, say so and list material validation gaps. A model's review is not independent acceptance.

This Skill grants no tools or permissions. It cannot authorize shell commands, modify model settings, or change budgets.
