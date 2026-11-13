#!/usr/bin/env bash
# Run the unit suite at the clock settings that break date arithmetic: midnights, DST changes,
# month and year ends, and hosts that are not on UTC. Prints one result line per setting and
# fails if any setting failed. Set CLOCK_OUT to keep the logs.
#
#   clock_matrix.sh --preflight   only prove that the harness works; ci.yml runs this on every
#                                 change, so a broken harness fails the blocking job within a
#                                 minute instead of a weekly run nobody reads.
set -uo pipefail
out="${CLOCK_OUT:-$(mktemp -d)}"
mkdir -p "$out"

# time-machine is a hash-locked development dependency in the project environment. Only the
# clock plugin needs PYTHONPATH; pytest and its workers use the same frozen environment.
uv sync --frozen --extra dev --quiet || exit 2

clocked_pytest() {
  local tz="$1" moment="$2"
  shift 2
  env TZ="$tz" HEALTHOS_TEST_CLOCK="$moment" PYTHONPATH="tests/support" \
    uv run --frozen --extra dev pytest -p clock_shift "$@"
}

# The plugin loaded and the process really sees the shifted moment and zone (support/clock_probe.py).
preflight() {
  local log="$out/preflight.txt"
  if clocked_pytest America/New_York 2027-11-07T05:59:30+00:00 \
    -q -n 0 -p no:cacheprovider tests/support/clock_probe.py >"$log" 2>&1; then
    echo "ok   preflight: the clock harness shifts time and zone"
    return 0
  fi
  echo "::error title=Clock harness is broken::tests/support/clock_matrix.sh could not shift the clock; see below"
  cat "$log"
  return 1
}

if [ "${1:-}" = "--preflight" ]; then
  preflight
  exit $?
fi
preflight || exit 2

failed=0
report() {
  [ -n "${GITHUB_STEP_SUMMARY:-}" ] && echo "| $1 | $2 | $3 |" >>"$GITHUB_STEP_SUMMARY"
  return 0
}
report Ayar Sonuç 'Son satır'
report --- --- ---
while IFS='|' read -r name tz moment; do
  [ -n "$name" ] || continue
  log="$out/$name.txt"
  if clocked_pytest "$tz" "$moment" -q -n "${WORKERS:-auto}" -m "not integration" >"$log" 2>&1; then
    echo "ok   $name ($tz $moment): $(tail -1 "$log")"
    report "$name" geçti "$(tail -1 "$log")"
  else
    failed=1
    echo "FAIL $name ($tz $moment): $(tail -1 "$log")"
    echo "::error title=Clock setting failed::$name ($tz $moment): $(tail -1 "$log")"
    report "$name" KALDI "$(tail -1 "$log")"
  fi
done <<'SETTINGS'
utc-midnight|UTC|2027-06-15T23:59:50+00:00
utc-year-end|UTC|2026-12-31T23:59:50+00:00
leap-day-end|UTC|2028-02-29T23:59:50+00:00
month-end-march|UTC|2027-03-31T23:59:50+00:00
newyork-dst-spring|America/New_York|2027-03-14T06:59:50+00:00
newyork-dst-fall|America/New_York|2027-11-07T05:59:30+00:00
istanbul-year-end|Europe/Istanbul|2026-12-31T20:59:50+00:00
apia-plus13-date-line|Pacific/Apia|2027-06-30T10:59:50+00:00
kathmandu-plus545|Asia/Kathmandu|2027-07-01T18:14:50+00:00
honolulu-now|Pacific/Honolulu|
far-future|UTC|2031-07-15T12:00:00+00:00
SETTINGS
exit "$failed"
