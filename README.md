# workflows

Reusable [`workflow_call`](https://docs.github.com/actions/using-workflows/reusing-workflows)
GitHub Actions workflows for the **bendoerr-terraform-modules** org.

Consolidates the per-repo workflow files (`lint.yml`, `pr-label.yml`, `dependency-review.yml`,
`scorecard.yml`, …) that were copy-pasted and drifting across the module repos. Callers pin a
version and pass per-repo variation via `inputs` + `secrets: inherit`.

> Rollout in progress. See the proposal for the staged plan (canary first, fan-out after).

## sandbox-lock action

`.github/actions/sandbox-lock` — a cross-repo reader/writer lock on the shared sandbox AWS account
(`234656776442`). GitHub concurrency groups are repository-scoped, so `concurrency: sandbox-test` never
serialised terratest against the nuke. Terratest lanes take this lock as `reader`; the nuke's `no-dry-run` job as
`writer`. Place `acquire` after `configure-aws-credentials` and `release` in a step with `if: always()`. Verdicts:
`0` acquired/released · `1` TIMED OUT (names the holders) · `2` LOCK UNREADABLE (the store failed, not a held lock) ·
`3` LEASE LOST (the job ran partly unprotected) · `64` USAGE.
