#!/usr/bin/env bash
# Run every service's unit tests in its own virtualenv.
#
#   scripts/run_tests.sh          # offline unit tests (safe, no network)
#   scripts/run_tests.sh --live   # also run the upstream contract canaries
#
# The canaries need the services running and the real .env; they hit third-party
# sites, so keep them out of any tight loop.
set -uo pipefail
cd "$(dirname "$0")/.."
ROOT=$(pwd)

declare -A VENVS=(
  [vinted]=".venv"
  [leboncoin]=".venv"
  [lacentrale]=".venv"
  [ecoledirecte]="venv"
)

status=0
for service in vinted leboncoin ecoledirecte lacentrale; do
  pytest="$ROOT/$service/${VENVS[$service]}/bin/pytest"
  if [ ! -x "$pytest" ]; then
    echo "== $service: SKIP (no venv at $service/${VENVS[$service]})"
    continue
  fi
  echo "== $service"
  ( cd "$service" && "$pytest" ) || status=1
done

if [ "${1:-}" = "--live" ]; then
  echo "== live upstream canaries"
  "$ROOT/lacentrale/.venv/bin/pytest" tests_live -m live -rs || status=1
fi

if [ "$status" -eq 0 ]; then echo "ALL GREEN"; else echo "FAILURES ABOVE"; fi
exit "$status"
