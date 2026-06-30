# workflows

Reusable [`workflow_call`](https://docs.github.com/actions/using-workflows/reusing-workflows)
GitHub Actions workflows for the **bendoerr-terraform-modules** org.

Consolidates the per-repo workflow files (`lint.yml`, `pr-label.yml`, `dependency-review.yml`,
`scorecard.yml`, …) that were copy-pasted and drifting across the module repos. Callers pin a
version and pass per-repo variation via `inputs` + `secrets: inherit`.

> Rollout in progress. See the proposal for the staged plan (canary first, fan-out after).
