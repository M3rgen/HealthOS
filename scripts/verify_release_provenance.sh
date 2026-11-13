#!/usr/bin/env bash
# Verify a release.yml asset: healthos-image.tar.gz or healthos.spdx.json.
set -euo pipefail

usage() {
  echo "Kullanım: $0 <indirilen-dosya> [--repo <sahip/ad>]" >&2
  exit 2
}

if [[ $# -ne 1 && $# -ne 3 ]]; then
  usage
fi
file="$1"
[[ -n "$file" ]] || usage

# release.yml signs as github.repository; the public release repository is below.
repo="M3rgen/HealthOS"
if [[ $# -eq 3 ]]; then
  [[ "$2" == --repo && -n "$3" ]] || usage
  repo="$3"
fi

if ! command -v gh >/dev/null 2>&1; then
  echo "Hata: Köken doğrulaması için GitHub CLI (gh) kurulu ve PATH içinde olmalı." >&2
  exit 2
fi

exec gh attestation verify "$file" --repo "$repo"
