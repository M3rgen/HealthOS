#!/usr/bin/env bash
# Run every job of .github/workflows/ci.yml for one exact commit under act and Docker, and record
# the receipt scripts/hooks/pre-push requires before anything reaches the public remote.
#
#   bash scripts/ci_gate.sh [<rev>]      (default: healthos.githubBranch, else github-main)
#
# The commit is fetched into an empty temporary repository, so uncommitted files and private
# branches cannot influence the run, and it runs as a push to main. A receipt is written only
# when act reports that every job passed. A rerun for the same commit overwrites its receipt.
# CodeQL and the Compose smoke test cannot run under act (no Linux arm64 CodeQL; bind mounts resolve
# on the Docker host, not in the act container); those steps skip themselves and the receipt says so.
# The installer gates (lint, macOS, Windows, Windows PowerShell 5.1, Docker installs) skip
# the same way (no macOS or Windows runner under act; their labels map to the Linux image only so
# that each can announce its skip).
# The workflows job runs on both architectures (verified actionlint and ShellCheck archives).
# Only the blocking Python entry of the matrix runs here: act starts matrix entries together and
# they would fight over PostgreSQL's published port on one Docker host. Python 3.13 is
# informational on GitHub too; check it alone with: act push -j verify --matrix python:3.13
# The action cache starts empty on every run: a cache from another run restores tools, such as
# Trivy, into a workspace path that no longer exists, and the job fails for that reason alone.
#
# Exit 0: gate passed. Exit 1: a CI job failed. Exit 2: missing tool or bad revision.
set -euo pipefail

# Same image digest for every run; `act` resolves the architecture from this index.
RUNNER_IMAGE="ghcr.io/catthehacker/ubuntu:act-24.04@sha256:62d572b92f9f32d3427b6d220ad1f9dca9c7b6ffad37d295425037dbff78abaf"

die() {
  echo "ci_gate: $*" >&2
  exit 2
}

cd "$(git rev-parse --show-toplevel)"
repo="$PWD"
rev="${1:-$(git config --get healthos.githubBranch || echo github-main)}"
commit="$(git rev-parse --verify --quiet "$rev^{commit}")" || die "unknown revision: $rev"
tree="$(git rev-parse "$commit^{tree}")"

command -v act >/dev/null || die "act is required (https://nektosact.com)"
command -v docker >/dev/null || die "docker is required"
docker info >/dev/null 2>&1 || die "no running Docker daemon"
: "${DOCKER_HOST:=$(docker context inspect --format '{{.Endpoints.docker.Host}}')}"
export DOCKER_HOST
case "$(docker info --format '{{.Architecture}}')" in
  aarch64 | arm64) architecture=linux/arm64 ;;
  x86_64 | amd64) architecture=linux/amd64 ;;
  *) die "unsupported Docker architecture" ;;
esac

receipts="$(git rev-parse --git-common-dir)/healthos-ci-gate"
remote_url="$(git remote get-url origin 2>/dev/null || echo https://github.com/example/healthos.git)"
work="$(mktemp -d "${TMPDIR:-/tmp}/healthos-ci-gate.XXXXXX")"
trap 'rm -rf "$work"' EXIT

git init -q "$work/tree"
git -C "$work/tree" fetch -q --no-tags "$repo" "$commit:refs/heads/main"
git -C "$work/tree" symbolic-ref HEAD refs/heads/main
git -C "$work/tree" reset -q --hard main
git -C "$work/tree" remote add origin "$remote_url"

echo "ci_gate: running ci.yml for $commit"
status=0
(
  cd "$work/tree"
  act push -W .github/workflows/ci.yml --concurrent-jobs 1 --matrix python:3.12.16 \
    -P "ubuntu-24.04=$RUNNER_IMAGE" -P "macos-15=$RUNNER_IMAGE" -P "windows-2025=$RUNNER_IMAGE" \
    --container-architecture "$architecture" \
    --container-daemon-socket /var/run/docker.sock \
    --cache-server-path "$work/actcache" \
    --rm
) || status=$?
if [ "$status" -ne 0 ]; then
  echo "ci_gate: ci.yml failed for $commit; no receipt written" >&2
  exit 1
fi

mkdir -p "$receipts"
{
  echo "tree $tree"
  echo "workflow $(git rev-parse "$commit:.github/workflows/ci.yml")"
  act --version
  echo "not-run-under-act codeql smoke installer_lint installer_hosts installer_linux_docker installer_windows_ps51"
} > "$receipts/$commit"
echo "ci_gate: passed; receipt for $commit"
