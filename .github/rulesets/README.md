# Repository rulesets

`main.json` and `release-tags.json` are the machine-readable protection for the public repository.
They are not applied by anything in this repository. To apply or update them (repository admin):

```sh
gh api --method POST repos/M3rgen/HealthOS/rulesets --input .github/rulesets/main.json
gh api --method POST repos/M3rgen/HealthOS/rulesets --input .github/rulesets/release-tags.json
gh api repos/M3rgen/HealthOS/rulesets   # list; update one with --method PUT .../rulesets/<id>
```

`main.json` requires the `summary` job of `ci.yml` and the two `CodeQL (…)` jobs (from GitHub
Actions, integration 15368). `summary` passes only when all nine gates of `ci.yml` pass: `verify`,
`bridge_boundary` (scripted CLI subprocess and JSON-RPC boundary), `installer`, `browser`, `e2e` (the built frontend against the real app and PostgreSQL), `security`,
`secrets`, `codeql` and `smoke`, so `e2e` is required through `summary` and needs no entry of its
own. It also requires a code-owner-reviewed pull request, and a branch that is up to date with
`main` (`strict`). Both rulesets have an empty `bypass_actors` list. Admins follow the same
review and required-check path. Release tags may be created by accounts with repository write
access, but no role may update or delete them; publication still requires the tag gate and
reviewed `release` environment.

Publishing order: prepare the snapshot locally, pass `make ci-gate`, submit the snapshot through a
branch and code-owner-reviewed pull request, then wait for successful hosted CI on the resulting
`main` commit. Only then create a matching `vX.Y.Z` tag. `release.yml` checks the version, main
ancestry and exact commit's successful CI, compares the image with the smoke-tested manifest,
scans it and verifies provenance before the environment review allows publication.

The current snapshot script and local pre-push hook only allow the direct snapshot push. That
path will be rejected once the main ruleset is applied. Adapting those tools to a reviewed branch
is a separate prerequisite for applying these files; do not restore an admin bypass to make the
old path work. An independent code owner must approve the pull request.

## The `release` environment

`publish` is the only job in `release.yml` with write access and it runs in `environment: release`.
**Until that environment exists with a required reviewer, `publish` goes through with no approval:**
GitHub lets a job through an environment that has no protection rule. Nothing in this repository can
create it. A repository admin adds it once, under *Settings → Environments → New environment*, named
exactly `release`, with an independent *Required reviewer* set, *Prevent self-review* enabled
and administrator bypass disabled. The API command below sets the reviewer and self-review
policy; verify the administrator-bypass setting in the UI as well:

```sh
REVIEWER_ID=$(gh api users/<reviewer-login> --jq .id)
gh api --method PUT repos/M3rgen/HealthOS/environments/release --input - <<EOF
{"prevent_self_review": true, "reviewers": [{"type": "User", "id": $REVIEWER_ID}]}
EOF
```

The environment approval is separate from branch protection and never grants a ruleset bypass. **Not yet proven:** the
environment has not been created, and the approval step has never run. So far the manifest comparison has
been run by hand on `arm64` only (a smoke-tested image against a fresh `--no-cache` build).

## CodeQL

The `codeql` job of `ci.yml` fails on a finding of high severity or error level
(`.github/scripts/codeql_gate.py`), so CodeQL blocks `summary` and therefore releases. Dismissing an
alert on GitHub does not change the SARIF the job reads: a finding judged acceptable goes in
`.github/codeql-exceptions.json` with `rule`, `path` and `reason`, reviewed like any other change.
The file starts empty on purpose. Results also go to code scanning, and `codeql.yml` re-scans weekly.

## Verifying a release

`release.yml` signs a build provenance attestation for both release assets, and verifies them
itself before publishing. Anyone can repeat that check on the downloaded files:

```sh
gh release download vX.Y.Z --repo M3rgen/HealthOS
for asset in healthos-image.tar.gz healthos.spdx.json; do
  gh attestation verify "$asset" --repo M3rgen/HealthOS \
    --signer-workflow M3rgen/HealthOS/.github/workflows/release.yml \
    --source-ref refs/tags/vX.Y.Z --deny-self-hosted-runners
done
docker load --input healthos-image.tar.gz   # only after both verifications pass
```

A pass proves the file is byte for byte what `release.yml` produced for that tag on a GitHub-hosted
runner. It needs a GitHub login (the attestations are read through the API). Compose builds the
image locally by default and does not consume this; only an operator who loads the released archive
gets the guarantee. **Not yet proven:** no release has been cut with this workflow, so neither the
job nor the commands above have run against a real attestation.

## Applying the reviewed path

These files are a proposed configuration, not proof of live GitHub settings. A repository admin
must adapt the snapshot branch/hook path, arrange an independent code owner and release reviewer,
create the protected environment, and apply both rulesets. After applying, verify that an admin
cannot push directly to `main` or rewrite/delete a release tag, and that `publish` waits for a
reviewer other than its initiator. Local receipts remain useful evidence but do not replace hosted
checks or review. No remote settings are changed by editing this repository.
