# SPDX-License-Identifier: MIT
#
# The default package overlay. It adds one attribute to the package set:
# the scope `pkgs.mill-purifier`, made with lib.makeScope. The scope holds
# the builder `buildMillProject` (pkgs/build-mill-project/default.nix) beside the
# mill-purifier command (`cli`), so a package written for callPackage takes
# the scope by name, `{ mill-purifier }:`, and uses
# `mill-purifier.buildMillProject`. `mill-purifier.overrideScope` replaces
# what everything in the scope shares, and `buildMillProject.override`
# replaces what the builder takes. A consumer that lists this flake in
# `projects` holds the overlay as `mill-purifier/default`, which its package
# sets apply by default.
#
# The command takes the source of the nix-purifiers/purifier flake,
# `closure-inputs.purifier`, because its interactive mode and its prefetch
# evaluate the builder with nixpkgs alone. The builder takes the library of
# that flake, `closure-lib.purifier`.
{ closure-inputs, closure-lib, ... }:
{
  overlay = closure-lib.caisson.nixpkgs.mkPackagesOverlay (callPackage: {
    cli = callPackage ../../pkgs/cli { purifierSource = closure-inputs.purifier; };
    buildMillProject = callPackage ../../pkgs/build-mill-project/default.nix {
      purifier = closure-lib.purifier;
    };
  }) "mill-purifier";
}
