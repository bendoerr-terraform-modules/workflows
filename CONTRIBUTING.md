# Contributing

Reusable `workflow_call` workflows for **bendoerr-terraform-modules/\***. Once a module repo pins one
of these, it is **load-bearing CI for the whole org** — treat changes accordingly.

## Layout

```
.github/workflows/<name>.yml   # each: on: workflow_call, with inputs + secrets
README.md                      # the input/secret contract per workflow
CONTRIBUTING.md                # this file
```

## Review gate

- **Scaffolding** (README, this file, dir layout) may go **straight to `main`** — it's inert.
- **Reusable workflows themselves come in via PR + review** before we cut/move a tag or wire a caller.
  The moment a repo pins `@v1`/`@<sha>`, the content gates merges for 16 repos. Eyes on it first.

## Versioning — graduated pin lane

You don't get max-propagation **and** controlled-blast on the same workflow. Pick per blast radius:

| lane | workflows | why |
|------|-----------|-----|
| **SHA-pin** (`@<sha>`) | `lint`, `test` (terratest) | gate-definers; a bad push must not turn every repo's CI red at once. Consistent with how every `uses:` here is already SHA-pinned; dependabot group-bumps the SHAs. |
| **moving `@v1`** | `pr-label`, `scorecard`, `dependency-review` | can't block a merge, so instant org-wide propagation is safe. |

Tags: moving `v1` (major) + immutable `v1.x.y`.

## Caller pattern

```yaml
name: lint
on: [pull_request]          # triggers stay in the caller — can't live in a reusable workflow
permissions: { contents: read }
jobs:
  lint:
    uses: bendoerr-terraform-modules/workflows/.github/workflows/lint.yml@v1
    with:
      allowed-endpoints: |
        api.deps.dev:443
        api.github.com:443
        api.securityscorecards.dev:443
        github.com:443
    secrets: inherit
```

`allowed-endpoints` above is the **canonical egress default** for the org (two sweeps of scar tissue).

## Branch-protection notes (verified 2026-06-30)

- The org repos run "Ben's Standard Main Branch Ruleset" with **no `required_status_checks` rule**, so
  moving a job into a reusable workflow renames its surfaced check from `<job>` to `<caller>/<inner>`
  **cosmetically** — nothing in the ruleset references the name, so there is no "Expected — waiting for
  status forever" trap.
- The **one** real status gate is **CodeQL** via the `code_scanning` rule, which keys on the SARIF
  **tool name** (`CodeQL`), not a check-run name — a reusable CodeQL workflow satisfies it as long as
  `github/codeql-action/analyze` uploads under tool `CodeQL` (the default).
