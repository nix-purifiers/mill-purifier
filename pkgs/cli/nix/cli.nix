# What the mill-purifier command builds: buildMillProject on the build root
# it runs in, with the nixpkgs and the purifier library of the command's
# own package. `prefetch` and the interactive mode build `dependencies` and
# `archiveCache`, which need only the lockfile; the publish verb builds
# `publishBuild`, which builds the packages.
#
#   nixpkgs         a nixpkgs source directory
#   purifierSource  the nix-purifiers/purifier source, whose default.nix
#                   takes { lib } and returns the library
#   src             the build root, as an absolute path string
#   lockFile        the lockfile, as an absolute path string
{
  nixpkgs,
  purifierSource,
  src,
  lockFile,
}:
let
  pkgs = import nixpkgs { };
  purifier = import purifierSource { inherit (pkgs) lib; };
  buildMillProject = pkgs.callPackage ../../build-mill-project { inherit purifier; };
in
buildMillProject {
  src = /. + src;
  lockFile = /. + lockFile;
}
