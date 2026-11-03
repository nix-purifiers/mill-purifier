#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
#
# Writes the lockfile of every fixture build again, with the mill-purifier
# command of this checkout. It needs the network. Run it from the
# repository root after a change to a fixture or to the lock run.
# MILL_PURIFIER_MAVEN_CENTRAL_MIRROR, when it is set, makes the lock runs
# resolve through that mirror; the lockfiles come out the same.
set -euo pipefail

cli=$(nix build --no-link --print-out-paths '.#cli')
for fixture in tests/fixtures/*/; do
  # The lock run locks the JVMs a build downloads (the lockfile's
  # platforms) for the Linux systems only, so such a fixture (polyglot) is
  # locked on Linux only.
  if [ "$(uname -s)" != Linux ] && jq -e '(.platforms // {}) != {}' "$fixture/mill-purifier.lock.json" >/dev/null; then
    echo "skipping $fixture: its build downloads JVMs, which a lock locks on Linux only" >&2
    continue
  fi
  echo "locking $fixture" >&2
  (cd "$fixture" && "$cli/bin/mill-purifier" lock)
done
