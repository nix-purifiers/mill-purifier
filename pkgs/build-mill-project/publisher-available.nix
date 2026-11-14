# Whether a Mill version has mill.javalib.MavenPublishModule, the Maven
# publisher that the lock run runs once and that `release` uses. Mill 1.1.0
# is the first release with it: the 1.0 releases and 1.1.0-RC1 do not have
# it. mill_version_key in pkgs/cli/mill_purifier.py makes the same decision.
{ lib }:
version:
let
  base = builtins.head (lib.splitString "-" version);
  prerelease = lib.hasInfix "-" version;
in
!(lib.versionOlder base "1.1.0") && !(prerelease && base == "1.1.0")
