# Pinned image update procedure

HealthOS frontend build image, resolved 2026-11-08:
`node:24.22.2-bookworm-slim@sha256:01d566e8d94d8c619591d78652babfccd3c6992a3dffa9ed5ab39e11cf6c70e9`.
Node is used only during compilation; the production runtime remains Python.

Resolved multi-architecture manifest-list digests recorded on 2026-08-25 (Python: 2026-11-08; uv: 2026-11-12):

| Component | Pinned reference |
| --- | --- |
| Python | `python:3.12.16-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df` |
| uv build tool | `ghcr.io/astral-sh/uv:0.13.6@sha256:af22a52f5eafef3d78e551599d405c02df61919d18028f0cea8eda93bc9986d0` |
| PostgreSQL/backup | `postgres:17.10-alpine@sha256:742f40ea20b9ff2ff31db5458d127452988a2164df9e17441e191f3b72252193` |
| Caddy | `caddy:2.11.4-alpine@sha256:5f5c8640aae01df9654968d946d8f1a56c497f1dd5c5cda4cf95ab7c14d58648` |

Digests pin bytes while patch tags explain update intent and make reviews understandable. The listed
digests are manifest lists, so Docker still selects the right architecture (for example amd64 or
arm64) on any host.

1. **Verification-only command:** inspect the candidate with `docker buildx imagetools inspect
   <patch-tag>`; confirm supported architectures and record the new digest. This does not pull it.
2. Change one component at a time. For Python or uv, regenerate/check `uv.lock` only if dependency
   metadata requires it.
3. **Verification-only command:** run the lock, lint, unit, PostgreSQL migration, image build, and
   isolated Compose smoke gates.
4. Create a fresh verified backup before approving deployment (`make backup`, then the restore
   drill in the [runbook](RUNBOOK.md#backup-verify-and-restore-drill)).
5. Deploy in a maintenance window with `docker compose up --build -d` on the server. This recreates
   containers; it is not a verification-only command.
6. Verify health, Alembic head, scheduler ownership, webhook path, and data freshness.
7. Roll back by restoring the previous tag+digest references and rebuilding. A database restore is
   not normally needed for patch-image rollback; if schema/data changed, use the separately verified
   backup and explicit restore procedure.

Never change PostgreSQL 17 to another major by editing an image tag. PostgreSQL major versions have
different on-disk formats and require a dedicated logical dump/restore or `pg_upgrade` migration,
extension compatibility checks, downtime planning, and rollback proof. The existing named volume
must never be attached to a different PostgreSQL major as an experiment.

To verify a `healthos-image.tar.gz` downloaded from a GitHub Release before loading it into Docker,
run the following from the repository root with GitHub CLI (`gh`) installed and authenticated;
verification reads attestations through the GitHub API. The default repository is
`M3rgen/HealthOS`, matching the public repository whose `release.yml` signs the assets; append
`--repo owner/name` for a fork. The same command also accepts the downloaded `healthos.spdx.json`.

```sh
bash scripts/verify_release_provenance.sh ./healthos-image.tar.gz
```

## Workflow lint tool

`ci.yml` pins actionlint **1.7.12** by release-archive SHA-256 (recorded 2026-11-12):

| Archive | SHA-256 |
| --- | --- |
| `actionlint_1.7.12_linux_amd64.tar.gz` | `8aca8db96f1b94770f1b0d72b6dddcb1ebb8123cb3712530b08cc387b349a3d8` |
| `actionlint_1.7.12_linux_arm64.tar.gz` | `325e971b6ba9bfa504672e29be93c24981eeb1c07576d730e9f7c8805afff0c6` |

Release archives keep the existing SHA-pinned `uses:` policy intact, which does not accept
`docker://` digest references. The `workflows` job checks the download before extraction and
installs ShellCheck 0.11.0 the same way, from the official release archives; actionlint invokes it
automatically. The hosted runner has its own ShellCheck, but the image `make ci-gate` uses under
`act` ships none, and one pinned version gives the same findings in both places.

| ShellCheck archive | SHA-256 |
| --- | --- |
| `shellcheck-v0.11.0.linux.x86_64.tar.xz` | `8c3be12b05d5c177a04c29e3c78ce89ac86f1595681cab149b65b97c4e227198` |
| `shellcheck-v0.11.0.linux.aarch64.tar.xz` | `12b331c1d2db6b9eb13cfca64306b1b157a86eb69db83023e261eaa7e7c14588` |

For a deliberate tool update, obtain both archives and `actionlint_<version>_checksums.txt`
from the [official releases](https://github.com/rhysd/actionlint/releases), verify both hashes,
and update the version URL, step label and both digests in `ci.yml` together with their guard
constants in `tests/test_ci_actionlint.py`. Run actionlint with ShellCheck, `make verify` and
`make ci-gate`; the maintainer must also confirm the hosted `workflows` job before publishing.
