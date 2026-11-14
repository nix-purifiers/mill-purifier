# SPDX-License-Identifier: MIT
#
# The `mill-purifier` library namespace:
#
#   readLock  the lockfile of a Mill build in the shape every purifier reads
#             its lockfiles into (lib-overlays/default/read-lock.nix), for tools that read
#             every purifier, such as an SBOM generator
#
# It imports the purifier library (`lib.purifier`), whose fetchers, tree,
# checkLock and entriesFor the builder uses. The builder is the package
# `buildMillProject` in the scope `pkgs.mill-purifier` that the default
# package overlay adds.
{ ... }:
{

  imports = [ "purifier/default" ];

  overlay = final: prev: {
    mill-purifier = (prev.mill-purifier or { }) // {
      readLock = import ./read-lock.nix;
    };
  };

}
