#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
#
# The conformance-update check. It runs `mill-purifier update` on a copy of
# tests/fixtures/ranged, whose lockfile pins cats-kernel 2.9.0 within the
# range [2.9.0,2.11.0), and checks that the update moves it to 2.10.0 and
# that readLock then reports no problem with the lockfile. It needs the
# network. Run it from the repository root.
set -euo pipefail

repo=$PWD
cli=$(nix build --no-link --print-out-paths '.#cli')
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
cp -r tests/fixtures/ranged/. "$work"
chmod -R u+w "$work"
cd "$work"

kernel=https/repo1.maven.org/maven2/org/typelevel/cats-kernel_3
grep -q "$kernel/2.9.0/" mill-purifier.lock.json || {
  echo "conformance-update: the fixture lockfile does not pin cats-kernel 2.9.0" >&2
  exit 1
}
"$cli/bin/mill-purifier" update org.typelevel:cats-kernel_3
if ! grep -q "$kernel/2.10.0/" mill-purifier.lock.json || grep -q "$kernel/2.9.0/" mill-purifier.lock.json; then
  echo "conformance-update: the update did not move cats-kernel from 2.9.0 to 2.10.0" >&2
  exit 1
fi
problems=$(nix-instantiate --eval --strict --expr "(import $repo/lib-overlays/default/read-lock.nix { src = $work; }).problems")
if [ "$problems" != "[ ]" ]; then
  echo "conformance-update: readLock reports $problems" >&2
  exit 1
fi
echo "conformance-update: the update moved cats-kernel to 2.10.0, and the lockfile is current" >&2
