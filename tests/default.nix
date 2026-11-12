# SPDX-License-Identifier: MIT
#
# The checks of the flake: the fixture builds of tests/fixtures, made from
# their committed lockfiles in the Nix sandbox, the conformance checks of
# the purifier contract, and the mill-purifier command (whose install check
# runs the unit tests). conformance-relock and conformance-rebuild need the
# network or a second build, so they are CI jobs (.github/workflows).
# `tests/relock-fixtures.sh` writes the lockfiles again.
{
  pkgs,
  # The flake's default package overlay, which adds buildMillProject.
  overlay,
  millPurifier,
  cli,
}:
let
  inherit (pkgs) lib;
  # Every build goes through buildMillProject in the mill-purifier scope of
  # a package set that the overlay applies to, as the build of a consumer
  # does.
  build = (pkgs.extend overlay).mill-purifier.buildMillProject;

  # The output of a Mill run must equal `expected`.
  prints =
    name: run: expected:
    pkgs.runCommand name { } ''
      if [ "$(cat ${run})" != ${lib.escapeShellArg expected} ]; then
        echo "${name}: expected ${lib.escapeShellArg expected}, Mill printed:" >&2
        cat ${run} >&2
        exit 1
      fi
      touch $out
    '';

  # Every artifact a package names exists, and the package holds the module
  # at its coordinates in Maven layout.
  hasArtifacts =
    name: package: mavenPath:
    pkgs.runCommand name { } ''
      test -d ${package}/${mavenPath}
      ${lib.concatMapStrings (file: ''
        test -e ${lib.escapeShellArg file}
      '') (lib.attrValues package.artifacts)}
      touch $out
    '';

  # tests/fixtures/simple: a Scala module with a test suite, on the system
  # JDK.
  simple = build { src = ./fixtures/simple; };
  core = simple.packages.core;

  # Lockfiles made from the simple fixture with one thing changed, sealed
  # with their content hash as `mill-purifier lock` would write them, or not
  # (`tampered`).
  simpleLock = builtins.fromJSON (builtins.readFile ./fixtures/simple/mill-purifier.lock.json);
  seal =
    lock:
    lock
    // {
      contentHash =
        "sha256:" + builtins.hashString "sha256" (builtins.toJSON (removeAttrs lock [ "contentHash" ]));
    };
  lockFileOf = name: lock: builtins.toFile "${name}.json" (builtins.toJSON lock);
  simpleWith =
    name: lock:
    build {
      src = ./fixtures/simple;
      lockFile = lockFileOf name lock;
    };
  # A check that evaluating `value` fails, as a lockfile error must.
  failsToEvaluate =
    name: value:
    assert
      !(builtins.tryEval (builtins.seq value true)).success
      || throw "${name}: the evaluation was expected to fail";
    pkgs.runCommand name { } "touch $out";
  # A check that readLock reports a problem with `lockFile` that names
  # `mill-purifier lock`, and that the build stops at it.
  rejects =
    name: args:
    let
      problems = (millPurifier.readLock ({ src = ./fixtures/simple; } // args)).problems;
    in
    assert
      problems != [ ] && lib.all (lib.hasInfix "`mill-purifier lock`") problems
      || throw "${name}: readLock reports ${builtins.toJSON problems}";
    failsToEvaluate name (build ({ src = ./fixtures/simple; } // args)).packages.core.drvPath;
  upickleJar = "https/repo1.maven.org/maven2/com/lihaoyi/upickle_3/4.1.0/upickle_3-4.1.0.jar";

  # The simple fixture without its lockfile, as a packaging flake sees a
  # project whose author keeps no lockfile.
  simpleWithoutLock = builtins.path {
    path = ./fixtures/simple;
    name = "simple-without-lock";
    filter = path: _: baseNameOf path != "mill-purifier.lock.json";
  };
  external = build {
    src = simpleWithoutLock;
    lockFile = ./fixtures/simple/mill-purifier.lock.json;
  };

  # tests/fixtures/polyglot: Java, Kotlin and Scala modules, a custom
  # repository, a meta-build plugin, a version from git, and JVMs Mill
  # downloads, for x86_64-linux and aarch64-linux.
  polyglotGit = commitsSinceTag: {
    rev = "0123456789abcdef0123456789abcdef01234567";
    revCount = 42;
    tag = "v1.2.3";
    inherit commitsSinceTag;
  };
  polyglot = build {
    src = ./fixtures/polyglot;
    git = polyglotGit 0;
  };
  polyglotUntagged = build {
    src = ./fixtures/polyglot;
    git = polyglotGit 5;
  };

  # A real-world project: fansi 0.5.1, on Mill 1.0.0. The lockfile in
  # tests/real-world comes from `mill-purifier lock` with the selector
  # fansi.jvm[2.13.15].test.testForked (tests/relock-real-world.sh). Mill 1.0.0
  # has no Maven publisher, so `release` is unavailable for this build.
  fansiRev = "387754bc1fc0be0ac787b195490275012d80c683";
  # The build reads the source while Nix evaluates, so the source is
  # fetched at evaluation time and is the same on every system, which a
  # derivation of fetchFromGitHub is not for a system other than the host.
  fansiSource = builtins.fetchTree {
    type = "github";
    owner = "com-lihaoyi";
    repo = "fansi";
    rev = fansiRev;
    narHash = "sha256-51QTUIXv6srcK9w7y9f4uo57kj5Fnlv0CO1PdoPWw6s=";
  };
  fansi = build {
    src = fansiSource.outPath;
    lockFile = ./real-world/fansi.lock.json;
    git = {
      rev = fansiRev;
      revCount = 147;
      tag = "0.5.1";
      commitsSinceTag = 0;
    };
  };
in
{
  # A producer needs a complete runtime view even with no Nix project inputs.
  standalone-projection = pkgs.runCommand "mill-standalone-projection" { } ''
    test -n "$(find ${core.projections.maven}/org/scala-lang/scala3-library_3 -name '*.jar')"
    touch $out
  '';
  composition = import ./composition.nix {
    inherit pkgs build;
    purifier = (pkgs.extend overlay).lib.purifier;
  };

  jvm-projection = core.projections.maven;

  inherit cli;

  simple-core = core;
  simple-tests = simple.tests;
  simple-core-artifacts = hasArtifacts "simple-core-artifacts" core "example/core_3/0.1.0";
  simple-core-jar = pkgs.runCommand "simple-core-jar" { nativeBuildInputs = [ pkgs.unzip ]; } ''
    unzip -l ${core.artifacts."core_3.jar"} | grep -q 'example/Encode.class'
    grep -q '<artifactId>core_3</artifactId>' ${core.artifacts."core_3.pom"}
    touch $out
  '';
  simple-run = prints "simple-run" (simple.runTask "simple-run-output" [ "core.run" ]) "[1,2]";

  # Mill's Maven publisher, offline, publishes the files Nix built.
  simple-release = pkgs.runCommand "simple-release" { } ''
    directory=$(find ${simple.release}/example/core_3 -mindepth 1 -maxdepth 1 -type d -name '0.1.0-nix-*')
    version=$(basename "$directory")
    cmp "$directory/core_3-$version.jar" ${core.artifacts."core_3.jar"}
    cmp "$directory/core_3-$version.pom" "${simple.projections.maven}/example/core_3/$version/core_3-$version.pom"
    test -f "$directory/core_3-$version-sources.jar.sha1"
    touch $out
  '';

  # readLock derives from each path the URL and the mirrors that the lock
  # run recorded for it (tests/url-vectors.json, taken from lock runs).
  derived-urls =
    let
      vectors = builtins.fromJSON (builtins.readFile ./url-vectors.json);
      lockFile = lockFileOf "url-vectors" (seal {
        version = 2;
        definitionHash = "sha256:0";
        projects = [ ];
        artifacts = map (v: {
          inherit (v) path;
          hash = "sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=";
        }) vectors;
      });
      derived =
        (millPurifier.readLock {
          src = ./fixtures/simple;
          inherit lockFile;
          allowStaleLock = true;
        }).entries;
      wrong = lib.filter (p: p.d.url != p.v.url || p.d.mirrors != p.v.mirrors) (
        lib.zipListsWith (v: d: { inherit v d; }) vectors derived
      );
    in
    assert
      wrong == [ ] || throw "derived-urls: readLock derives ${builtins.toJSON (map (p: p.d) wrong)}";
    pkgs.runCommand "derived-urls" { } "touch $out";

  # readLock gives the lock in the shape of the contract, which checkLock of
  # the purifier library accepts (the builder's `lock` has passed it), with
  # the JVM index and archives of each platform marked with their system.
  read-lock =
    let
      inherit (polyglot) lock;
      systems = lib.unique (lib.concatMap (e: e.systems or [ ]) lock.entries);
    in
    assert
      lock.purifier == "mill-purifier"
      && lock.outputs ? greeter
      &&
        lib.sort lib.lessThan systems == [
          "aarch64-linux"
          "x86_64-linux"
        ]
      || throw "read-lock: readLock gives purifier ${lock.purifier}, outputs ${builtins.toJSON (builtins.attrNames lock.outputs)} and systems ${builtins.toJSON systems}";
    pkgs.runCommand "read-lock" { } "touch $out";

  # Mill 1.1.0 is the first release with the Maven publisher: the 1.0
  # releases and 1.1.0-RC1 have none, and `release` says so for them.
  publisher-available =
    let
      available = import ../pkgs/build-mill-project/publisher-available.nix { inherit lib; };
      without = [
        "0.12.14"
        "1.0.0"
        "1.0.6"
        "1.1.0-RC1"
        "1.1.0-RC4"
      ];
      with' = [
        "1.1.0"
        "1.1.4"
        "1.3.1"
        "1.10.0"
        "1.2.0-RC1"
        "2.0.0"
      ];
      wrong = lib.filter available without ++ lib.filter (v: !(available v)) with';
    in
    assert wrong == [ ] || throw "publisher-available: wrong for ${builtins.toJSON wrong}";
    pkgs.runCommand "publisher-available" { } "touch $out";

  # The Nix side computes the definition hash the command wrote.
  definition-hash =
    assert
      (import ../lib-overlays/default/definition-hash.nix ./fixtures/simple).hash
      == simpleLock.definitionHash
      || throw "definition-hash: lib-overlays/default/definition-hash.nix disagrees with mill_purifier.definition_hash";
    pkgs.runCommand "definition-hash" { } "touch $out";

  # The conformance checks of the purifier contract.
  conformance-stale = rejects "conformance-stale" {
    lockFile = lockFileOf "stale" (seal (simpleLock // { definitionHash = "sha256:0"; }));
  };
  conformance-tampered = rejects "conformance-tampered" {
    lockFile = lockFileOf "tampered" (
      simpleLock
      // {
        artifacts = map (
          a:
          if a.path or "" == upickleJar then
            a // { hash = "sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="; }
          else
            a
        ) simpleLock.artifacts;
      }
    );
  };
  conformance-malformed = rejects "conformance-malformed" {
    lockFile = builtins.toFile "malformed.json" ''{"version": 2}'';
    allowStaleLock = true;
  };
  conformance-other-version = rejects "conformance-other-version" {
    lockFile = lockFileOf "version" (seal (simpleLock // { version = 99; }));
  };
  # A lock that lacks a download fails in the sandbox, offline, and the
  # build names the command that fixes it.
  conformance-missing =
    pkgs.runCommand "conformance-missing"
      {
        failed =
          pkgs.testers.testBuildFailure
            (simpleWith "missing" (
              seal (simpleLock // { artifacts = builtins.filter (a: a.path != upickleJar) simpleLock.artifacts; })
            )).packages.core;
      }
      ''
        grep -F 'run `mill-purifier lock`' $failed/testBuildFailure.log
        touch $out
      '';
  # A source tree with no lockfile builds from one kept outside it.
  conformance-external = prints "conformance-external" (external.runTask "external-run" [
    "core.run"
  ]) "[1,2]";
  # After a prefetch, the everyday loop works with no network: the sandbox
  # has none. The prefetch record points at the caches Nix built, as
  # `mill-purifier prefetch` writes it, and the command compiles and tests
  # the fixture through `mill-purifier mill`.
  conformance-offline =
    pkgs.runCommand "conformance-offline"
      {
        nativeBuildInputs = [ cli ];
        prefetchState = builtins.toJSON {
          inherit (simpleLock) contentHash;
          dependencies = "${simple.dependencies}";
          archiveCache = "${simple.archiveCache}";
        };
        passAsFile = [ "prefetchState" ];
      }
      ''
        export HOME="$TMPDIR/home" XDG_CACHE_HOME="$TMPDIR/cache"
        mkdir -p "$HOME"
        cp -r ${./fixtures/simple} project
        chmod -R u+w project
        cd project
        mkdir -p .mill-purifier
        cp "$prefetchStatePath" .mill-purifier/prefetch.json
        mill-purifier mill --no-daemon --ticker false core.compile + core.test
        touch $out
      '';

  # fansi 0.5.1: the JVM tests the lock run ran, every package (JVM, JS and
  # Scala Native modules for three Scala versions), and the version from the
  # git shim, which is the tag.
  real-world-fansi-source =
    pkgs.runCommand "real-world-fansi-source" { }
      "cp -r ${fansiSource.outPath} $out";
  real-world-fansi-tests = fansi.tests;
  real-world-fansi-packages = pkgs.linkFarm "real-world-fansi-packages" (
    lib.mapAttrsToList (id: package: {
      name = id;
      path = package;
    }) fansi.packages
  );
  real-world-fansi-version =
    hasArtifacts "real-world-fansi-version" fansi.packages."fansi.jvm[2.13.15]"
      "com/lihaoyi/fansi_2.13/0.5.1";
  # Mill 1.0.0 has no Maven publisher, and `release` says so.
  real-world-fansi-release = failsToEvaluate "real-world-fansi-release" fansi.release.drvPath;
}
// lib.optionalAttrs (polyglot.lock.mill.archives ? ${pkgs.stdenv.hostPlatform.system}) {
  polyglot-greeter-package = polyglot.packages.greeter;
  polyglot-hello-package = polyglot.packages.hello;
  polyglot-versioned-package = polyglot.packages.versioned;
  polyglot-greeter =
    hasArtifacts "polyglot-greeter" polyglot.packages.greeter
      "example/greeter/0.1.0";
  polyglot-hello = hasArtifacts "polyglot-hello" polyglot.packages.hello "example/hello/0.1.0";
  # The version comes from the git shim: the tag, without its `v`.
  polyglot-versioned =
    hasArtifacts "polyglot-versioned" polyglot.packages.versioned
      "example/versioned_3/1.2.3";
  # After the tag: <tag>-<commits>-<six digits of the commit>.
  polyglot-versioned-untagged =
    hasArtifacts "polyglot-versioned-untagged" polyglotUntagged.packages.versioned
      "example/versioned_3/1.2.3-5-012345";
  polyglot-hello-run = prints "polyglot-hello-run" (polyglot.runTask "polyglot-hello-output" [
    "hello.run"
  ]) "hello java, kotlin";
  polyglot-versioned-run = prints "polyglot-versioned-run" (polyglot.runTask
    "polyglot-versioned-output"
    [ "versioned.run" ]
  ) "built with Scala 3.7.3";
  polyglot-release = pkgs.runCommand "polyglot-release" { nativeBuildInputs = [ pkgs.python3 ]; } ''
    export REPOSITORY=${polyglot.projections.maven}
    export CATALOG=${polyglot.projections.maven.catalog}
    export RELEASE=${polyglot.release}
    python - <<'PYTHON'
    import json, os
    from pathlib import Path
    modules = json.loads(Path(os.environ['CATALOG']).read_text())['modules']
    own = {'greeter', 'hello', 'versioned_3'}
    assert own <= {module['name'] for module in modules}
    import xml.etree.ElementTree as ET
    hello = next(module for module in modules if module['name'] == 'hello')
    pom = Path(os.environ['REPOSITORY']) / 'example/hello' / hello['version'] / ('hello-'+hello['version']+'.pom')
    model = ET.parse(pom).getroot()
    greeter = next(dep for dep in model.findall('.//{*}dependencies/{*}dependency')
                   if dep.findtext('{*}artifactId') == 'greeter')
    selected = greeter.findtext('{*}version').strip('[]')
    assert '-nix-' in selected
    assert (Path(os.environ['REPOSITORY'])/'example/greeter'/selected/('greeter-'+selected+'.jar')).is_file()
    for module in modules:
        directory = Path(module['group'].replace('.', '/')) / module['name'] / module['version']
        if module['name'] in own:
            assert '-nix-' in module['version']
        for source in (Path(os.environ['REPOSITORY']) / directory).iterdir():
            if source.suffix in {'.jar', '.pom'}:
                assert (Path(os.environ['RELEASE']) / directory / source.name).read_bytes() == source.read_bytes()
    PYTHON
    touch $out
  '';
}
