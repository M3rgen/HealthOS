#!/usr/bin/env bash
# Exercise the real scanner with nonfunctional fixture strings.
set -euo pipefail
repo_root="$(cd "$(dirname "$0")/../.." && pwd)"
fixture_dir="$(mktemp -d)"
trap 'rm -rf "$fixture_dir"' EXIT

# Split the invented value so the regression script itself contains no key assignment.
fixture_value="71c9e2a4f8d360b5""a7e9c1d4f2b806a3"
{
  printf 'api_key = "ci-only-%s"\n' "$fixture_value"
  printf 'api_key = "synthetic-%s"\n' "$fixture_value"
  printf 'api_key = "wrong-token"\n'
  printf 'api_key = "candidate-access"\n'
  printf 'api_key = "short-123456789"\n'
  printf 'APPLE_TOKEN_NEW=""\n'
} > "$fixture_dir/allowed.txt"
gitleaks dir "$fixture_dir" --config "$repo_root/.gitleaks.toml" --redact --no-banner

# A different, explicitly nonfunctional fixture must still trip the default rules.
for variable in api_key APPLE_TOKEN_NEW; do
  printf '%s = "%s"\n' "$variable" "$fixture_value" > "$fixture_dir/blocked.txt"
  status=0
  gitleaks dir "$fixture_dir" --config "$repo_root/.gitleaks.toml" --redact \
    --no-banner --log-level error --exit-code 42 || status=$?
  if [ "$status" -ne 42 ]; then
    echo "Expected the default rules to reject the $variable fixture; got $status" >&2
    exit 1
  fi
done
echo "Gitleaks regression checks passed"
