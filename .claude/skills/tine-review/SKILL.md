---
name: tine-review
description: >
  Review a branch's commits against origin/main with adversarial sub-agents and write a report to post as
  a PR review. Use it also for a re-review after the branch was pushed again.
argument-hint: "[branch]"
---

<!--
SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
SPDX-License-Identifier: MPL-2.0
-->

# Reviewing a commit series

Do an adversarial sub-agent review of the commits of the argument's branch (defaulting to the current
one) since `origin/main`, and output a PR review report.

One sub-agent per lens over the same ref, in parallel. Give each the constraints from AGENTS.md, plus:
never kill a daemon, since other sessions share it. Their findings are leads; verify each one before
it reaches the report, and drop or hedge what you cannot.

Pin the base before reading the diff. The branch the user names may not be HEAD, the local
`origin/main` may be outdated, and a re-review is a `range-diff` against the revision reviewed last.
An existing report for the branch makes this a re-review; its anchor is that revision.
Check out the branch for running something; if the tree is dirty, stop and ask first.

## Lenses

Focus on these unless the prompt names others:

- **Correctness**: handles invalid input or external state, follows AGENTS.md rules, bug hunting,
  test coverage of the actual code and of the commit message's claim, handles user input errors
  in a readable/actionable way
- **Architecture**: does the change fit into the project, and is there a better conceptual way to
  achieve the result?
- **Minimality**: don't overbuild (YAGNI), DRY, reuses existing internal or external API/libraries where
  available, a commit has only one conceptual change. No over-handling of errors: tracebacks
  are enough for fringe OS errors
- **Correspondence**: code, comments, user and design docs, commit message

## The report

Write `/tmp/pr-<branchname>-review.md`:

- Record the reviewed ref range as the anchor for re-reviews.
- Be brief and specific: link to specs/docs/commits, don't say what is right, don't repeat project
  rules/code/diffs.
- Sort findings by descending severity, mark blockers.
- Per finding: the file:line anchor, what breaks, and the state that breaks it. Give it a short stable
  identifier, like "B1" for the first blocker. Suggest a fix only when the user asks.
- Round N: what was addressed, what is left, what is new, what was declined by the user. Delete what
  landed, keep the stable identifiers of what remains.
