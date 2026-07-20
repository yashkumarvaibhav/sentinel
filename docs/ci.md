# CI and branch protection

## What runs

`.github/workflows/ci.yml` runs on every pull request and on every push to
`main`, in three jobs:

| Job | Gate |
|---|---|
| `python` | `ruff format --check` → `ruff check` → `mypy` (strict) → `pytest` |
| `web` | `tsc --noEmit` → `eslint` → `vitest run` → `vite build` |
| `stack` | `make up` (builds gateway + web images), then `/api/health` is `ready`, `/api/version` names the checked-out commit, and the front door serves the app |

`stack` runs only after the two fast jobs pass — there is no point spending five
minutes pulling images to smoke-test code that does not typecheck.

The same gates run locally as `make verify` plus `make up`. Nothing in CI is
allowed to be a gate that a developer cannot run on their own machine.

## What deliberately does not run here

The evaluation lab does not run on hosted runners. The k3s testbed, chaos
experiments and load generation need far more than a hosted runner has, and a
live testbed is only statistically reproducible, not bit-exact — grading a pull
request on it would produce flapping results.

So the split is:

- **Hosted CI** grades what is deterministic: unit and property tests, replays
  of recorded captures, and the scoring gate over those captures.
- **The lab host** runs live testbed scoring — nightly and at every phase
  close — where run-to-run variance is expected and judged with tolerance
  bands.

## Branch protection

The build currently commits straight to `main` in verified slices, so the
protection that makes sense today is the kind that cannot get in the way of
that:

```bash
gh api -X PUT repos/:owner/:repo/branches/main/protection \
  -F required_status_checks=null \
  -F enforce_admins=false \
  -F required_pull_request_reviews=null \
  -F restrictions=null \
  -F allow_force_pushes=false \
  -F allow_deletions=false
```

That protects history — no force pushes, no branch deletion — while leaving
direct pushes open.

When work moves onto branches and pull requests, tighten it: require a pull
request, require the branch to be up to date, and require these checks by
name — `python — lint, types, tests`, `web — types, lint, tests, build`,
`stack — compose smoke`, plus the scoring and golden-replay checks as soon as
they exist.

Until then the gate is discipline: never commit red, and never push a commit
whose gates were not run locally.
