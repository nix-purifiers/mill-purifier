# SPDX-License-Identifier: MIT
#
# The mill-purifier command. It runs Mill on `jdk`, downloads Mill launchers
# with curl, and realizes lockfiles with the Nix library in nix/lib.
{
  lib,
  path,
  stdenvNoCC,
  makeWrapper,
  python3,
  curl,
  mill,
  callPackage,
  # The JDK the lock run runs Mill's launcher on, which the builds of
  # buildMillProject also take (pkgs/build-mill-project/jdk.nix).
  jdk ? callPackage ../build-mill-project/jdk.nix { },
  # The source of the nix-purifiers/purifier flake, whose default.nix the
  # command's builds import; the package overlay passes it.
  purifierSource ? null,
}:
assert
  purifierSource != null
  || throw "mill-purifier: the command needs purifierSource, the nix-purifiers/purifier source; take the package from this flake's overlay";
let
  runtimeSource = lib.fileset.toSource {
    root = ../..;
    fileset = lib.fileset.unions [
      ../../lib-overlays/default
      ../build-mill-project
      ./nix
    ];
  };
in
stdenvNoCC.mkDerivation {
  pname = "mill-purifier";
  version = import ../version.nix;

  src = lib.fileset.toSource {
    root = ./.;
    fileset = lib.fileset.unions [
      ./mill_purifier.py
      ./coursier_cache.py
      ./test_coursier_cache.py
      ./test_mill_purifier.py
    ];
  };

  nativeBuildInputs = [ makeWrapper ];

  dontConfigure = true;
  dontBuild = true;

  doInstallCheck = true;
  installCheckPhase = ''
    runHook preInstallCheck
    ${python3.interpreter} -m unittest test_coursier_cache test_mill_purifier
    runHook postInstallCheck
  '';

  installPhase = ''
    runHook preInstall
    mkdir -p $out/libexec/mill-purifier $out/bin
    cp mill_purifier.py coursier_cache.py $out/libexec/mill-purifier/
    cp -r ${runtimeSource}/. $out/libexec/mill-purifier/
    makeWrapper ${python3.interpreter} $out/bin/mill-purifier \
      --add-flags "$out/libexec/mill-purifier/mill_purifier.py" \
      --prefix PYTHONPATH : ${purifierSource}/tools \
      --set MILL_PURIFIER_MILL ${lib.getExe mill} \
      --set MILL_PURIFIER_JDK ${jdk.home} \
      --set MILL_PURIFIER_LIB "$out/libexec/mill-purifier/pkgs/cli/nix" \
      --set MILL_PURIFIER_NIXPKGS ${path} \
      --set MILL_PURIFIER_PURIFIER_SOURCE ${purifierSource} \
      --set MILL_PURIFIER_BIN "$out/bin/mill-purifier" \
      --prefix PATH : ${lib.makeBinPath [ curl ]}
    runHook postInstall
  '';

  meta = {
    description = "Locks the downloads of a Mill build and runs Mill on them";
    license = lib.licenses.mit;
    mainProgram = "mill-purifier";
  };
}
