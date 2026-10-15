---
name: test-repair
description: Repair production behavior against fixed acceptance tests in an isolated worktree. Use when a bug has an explicit expected result; preserve tests and protected configuration.
metadata:
  version: '1'
---

# Repair against fixed acceptance

1. Confirm the expected behavior, source baseline, allowed files and fixed acceptance command.
2. Reproduce the failure using the existing execution policy. Read `references/acceptance.md` before selecting a candidate patch.
3. Make the smallest coherent production-code correction in the assigned execution copy. Never change tests, dependencies, CI or acceptance rules to manufacture success.
4. Run independent acceptance and inspect the exact patch. A final model message does not prove tests passed.
5. If verification fails, use the concrete failure to select a bounded correction. Honor controller iteration and budget limits; do not create a new task to bypass them.
6. Present patch and verification evidence for explicit user adoption. Never auto-commit, merge or push.

This Skill does not create an isolated worktree, grant permissions, or start acceptance by itself. The Host and DeliveryController own those operations and stop conditions.
