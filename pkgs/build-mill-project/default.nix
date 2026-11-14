# buildMillProject: the sandboxed builds of a Mill build, from the lockfile
# that `mill-purifier lock` writes at the build root.
#
# The file is a package for callPackage: the first argument set is what it
# takes from the package set, and the result is the builder. The
# mill-purifier overlay (`overlays.default`) adds the scope `mill-purifier`
# to the package set, and the scope holds the builder, so a package written
# for callPackage takes the scope by name:
#
#   { mill-purifier }:
#   (mill-purifier.buildMillProject { src = ./.; }).packages.core
#
# `mill-purifier.buildMillProject.override` replaces what the builder takes,
# and `mill-purifier.overrideScope` replaces what the scope shares. The
# builder returns
#
#   packages.<id>  the published artifacts of the Mill module <id> (its
#                  module path, such as `core` or `foo.bar`), in Maven
#                  layout
#   tests          the test tasks the lock run ran, run in the sandbox
#   release        every package published by Mill's own Maven publisher
#                  (mill.javalib.MavenPublishModule) into a Maven
#                  repository. Mill 1.1.0 is the first release with that
#                  publisher, so for a build on an older Mill, `release`
#                  fails when it is evaluated and says so
#   fetches        the entries of the lock: the purifier library entries the
#                  build fetches, those of one system carrying `systems`
#   runTask        runTask name [ args ]: a derivation that runs Mill with
#                  `args` offline in the sandbox, such as [ "core.run" ],
#                  with what Mill prints in `out`
#   publishBuild   a Mill build whose `publishArtifacts` tasks return the
#                  files of the packages, for Mill's publishers
#   dependencies   the coursier download cache of the fetches
#   archiveCache   the coursier archive cache: the locked archives (JDKs),
#                  unpacked and patched for Nix
#   lock           the lock in memory, as readLock (read-lock.nix) gives it
#   jdk            the JDK that runs Mill
{
  lib,
  stdenvNoCC,
  runCommand,
  callPackage,
  fetchurl,
  # What fetchEntry of the purifier library takes for the steps of an entry.
  unzip,
  zstd,
  python3,
  writeText,
  jq,
  strip-nondeterminism,
  which,
  # The purifier library (lib.purifier): fetchEntry, tree, checkLock and
  # entriesFor.
  purifier,
}:
{
  # The root of the Mill build.
  src,
  # The fetchers of the purifier library (fetchEntry and tree), such as a
  # `fetchurl` with credentials or a company mirror. The default is
  # nixpkgs' own.
  fetchers ? { },
  # Nix-built JVM inputs; their catalogs select the actual artifact bytes.
  projectDependencies ? purifier.lockedProjectInputs {
    inputs = (builtins.fromJSON (builtins.readFile lockFile)).projectInputs or [ ];
    system = stdenvNoCC.hostPlatform.system;
  },
  # The lockfile; the default is the one `mill-purifier lock` writes at the
  # build root.
  lockFile ? src + "/mill-purifier.lock.json",
  # The JDK that runs Mill's launcher, and Mill's daemon when the build sets
  # `mill-jvm-version: system`. The default is the JDK of the lock run
  # (pkgs/build-mill-project/jdk.nix).
  jdk ? null,
  # The git commit the source is of, for a build that computes its version
  # from git (Mill's VcsVersion): { rev, revCount, tag, commitsSinceTag }.
  # A flake passes { inherit (self) rev revCount; } and, at or after a tag,
  # `tag` and `commitsSinceTag`. See git-shim.nix. The default gives the
  # build no git, which VcsVersion reports as `no-vcs`.
  git ? null,
  # Filters the build root before it is copied into the store, as the
  # `filter` of builtins.path. The default leaves out build outputs and
  # editor state.
  srcFilter ? null,
  # Builds from a lock whose build definition differs from the one the lock
  # was made from (a stale lock), which otherwise stops the build.
  allowStaleLock ? false,
}:
let
  # The lock in memory (read-lock.nix), which checkLock of the purifier
  # library stops at with its problems.
  lock = purifier.checkLock (
    import ../../lib-overlays/default/read-lock.nix { inherit src lockFile allowStaleLock; }
  );
  system = stdenvNoCC.hostPlatform.system;

  # Build outputs and editor state never take part in a build.
  defaultSrcFilter =
    path: type:
    let
      name = baseNameOf path;
    in
    !(
      (
        type == "directory"
        && (
          name == "out" || name == ".bsp" || name == ".metals" || name == ".bloop" || name == ".mill-purifier"
        )
      )
      || name == ".git"
      || name == ".idea"
      || lib.hasPrefix "result" name
    );

  filteredSrc = builtins.path {
    path = src;
    name = "mill-source";
    filter = if srcFilter != null then srcFilter else defaultSrcFilter;
  };

  jdk' = if jdk != null then jdk else callPackage ./jdk.nix { };

  # The Mill version the build pins, and the launcher of it, which the lock
  # run downloaded from Maven Central to this place in the coursier cache.
  millVersion = import ./mill-version.nix src;
  launcher = "https/repo1.maven.org/maven2/com/lihaoyi/mill-dist/${millVersion}/mill-dist-${millVersion}.exe";

  # The downloads of this system, and the JVM archives coursier unpacks on
  # it. A lock whose build downloads a JVM holds the archives of each system
  # it was made for.
  systemEntries = purifier.entriesFor system lock;
  archives =
    if lock.mill.archives == { } then
      [ ]
    else
      lock.mill.archives.${system}
        or (throw "mill-purifier: ${toString lockFile} locks the JVMs of the build for ${lib.concatStringsSep " and " (builtins.attrNames lock.mill.archives)}, not for ${system}.");
  # What fetchEntry and tree of the purifier library take: the tools of the
  # package set by name, and the fetchers of the caller.
  fetcherArgs = {
    inherit
      fetchurl
      runCommand
      unzip
      zstd
      python3
      writeText
      ;
  }
  // fetchers;

  dependencies = purifier.tree (
    fetcherArgs
    // {
      name = "mill-coursier-cache";
      entries = map (e: removeAttrs e [ "systems" ]) systemEntries;
    }
  );
  archiveCache = callPackage ./archive-cache.nix {
    inherit archives;
    entries = systemEntries;
    fetchEntry = purifier.fetchEntry fetcherArgs;
  };
  provided = purifier.jvmProjections {
    repository = null;
    dependencies = projectDependencies;
    inherit runCommand python3;
    name = "mill-provided-dependencies";
  };
  gitShim = if git == null then null else callPackage ./git-shim.nix { } git;

  # The Mill invocation shared by every build from the lock;
  # `mill-purifier lock` runs Mill the same way, so a lock made from it holds
  # what this needs. It is one subshell, so a redirection after it takes the
  # output of Mill.
  runMill = runMillWithInputs true;
  runMillWithInputs = stageInputs: args: ''
    (
    export HOME="$TMPDIR/home"
    export XDG_RUNTIME_DIR="$TMPDIR/run"
    export XDG_CACHE_HOME="$HOME/cache" XDG_CONFIG_HOME="$HOME/config"
    export XDG_DATA_HOME="$HOME/data" XDG_STATE_HOME="$HOME/state"
    export COURSIER_CACHE="$TMPDIR/coursier-cache"
    export COURSIER_ARCHIVE_CACHE="$TMPDIR/coursier-archive-cache"
    export COURSIER_MODE=offline
    # Mill passes JAVA_OPTS to the JVM it starts for its daemon.
    # java.properties.date replaces the time of the build in the comment
    # that java.util.Properties.store writes (Java 18 and later), as in the
    # files Mill's BuildInfo plugin writes.
    export JAVA_OPTS="-Duser.home=$HOME -Djava.io.tmpdir=$TMPDIR/java-tmp -Djava.properties.date=1980-01-01T00:00:00Z"
    # The launcher of Mill 1.0 runs `which`.
    export PATH="${which}/bin:$PATH"
    ${lib.optionalString (gitShim != null) ''export PATH="${gitShim}/bin:$PATH"''}
    if [ ! -d "$COURSIER_CACHE" ]; then
      mkdir -p "$HOME" "$XDG_RUNTIME_DIR" "$TMPDIR/java-tmp" "$COURSIER_CACHE" "$COURSIER_ARCHIVE_CACHE"
      # Coursier writes lock and marker files next to what it reads, so the
      # caches are writable trees of links into the store.
      cp -rs --no-preserve=mode ${dependencies}/. "$COURSIER_CACHE"
      cp -rs --no-preserve=mode ${archiveCache}/. "$COURSIER_ARCHIVE_CACHE"
    fi
    ${lib.optionalString (stageInputs && projectDependencies != [ ]) ''
      ${python3}/bin/python ${purifier.coursierInputsTool}/coursier_inputs.py \
        --cache "$COURSIER_CACHE" --repository ${provided.maven} \
        --catalog ${provided.maven.catalog} --sources "$PWD" --bootstrap
      java $JAVA_OPTS -jar "$COURSIER_CACHE/${launcher}" \
        --no-daemon --ticker false show \
        '{__.mvnDeps,__.runMvnDeps,__.compileMvnDeps,__.mandatoryMvnDeps}' \
        </dev/null > "$TMPDIR/mill-project-dependencies.json"
      ${python3}/bin/python ${purifier.coursierInputsTool}/coursier_inputs.py \
        --cache "$COURSIER_CACHE" --repository ${provided.maven} \
        --catalog ${provided.maven.catalog} --sources "$PWD" \
        --evaluated "$TMPDIR/mill-project-dependencies.json"
    ''}
    java $JAVA_OPTS -jar "$COURSIER_CACHE/${launcher}" \
      --no-daemon --ticker false ${args} </dev/null || {
      echo "mill-purifier: Mill failed in the Nix sandbox, offline, with only the downloads of the lockfile." >&2
      echo "mill-purifier: if it reports a file it could not find or download, the lockfile lacks it: run \`mill-purifier lock\` in the build root and commit the lockfile." >&2
      exit 1
    }
    )'';

  # The build links each artifact at a name without the version of the
  # module: <linkDirectory>/<key>, such as nix-support/artifacts/core_3.jar.
  linkDirectory = "nix-support/artifacts";
  artifactFile = package: key: "${package}/${linkDirectory}/${key}";

  mkPackage =
    project:
    let
      publicationSource = runCommand "${project.name}-publication-source" { } ''
        ${python3}/bin/python ${./publication-views.py} ${purifier.projectInputsTool} \
          scope "$out" ${nativePublications} ${package}/nix-support/publish.json
      '';
      package = stdenvNoCC.mkDerivation (finalAttrs: {
        inherit (project) name;
        src = filteredSrc;
        nativeBuildInputs = [
          jdk'
          jq
          strip-nondeterminism
        ];
        dontConfigure = true;
        # The outputs are jars and POMs; fixup has nothing to patch, and it
        # must not rewrite them.
        dontFixup = true;
        buildPhase = ''
          runHook preBuild
          ${
            runMill (
              lib.escapeShellArgs [
                "show"
                "${project.id}.publishArtifacts"
              ]
            )
          } > "$TMPDIR/publish.json"
                    ${
                      runMill (
                        lib.escapeShellArgs [
                          "show"
                          "${project.id}.resolvedRunMvnDeps"
                        ]
                      )
                    } \
              > "$TMPDIR/runtime-classpath.json"
          runHook postBuild
        '';
        installPhase = ''
          runHook preInstall
          group=$(jq -r .meta.group "$TMPDIR/publish.json")
          id=$(jq -r .meta.id "$TMPDIR/publish.json")
          version=$(jq -r .meta.version "$TMPDIR/publish.json")
          dir="''${group//.//}/$id/$version"
          mkdir -p "$out/$dir" "$out/${linkDirectory}"
          # Each payload entry is [path reference, published file name]; a
          # path reference is `ref:<version>:<signature>:<path>` (or `qref:`).
          jq -r '.payload[] | [(.[0] | sub("^q?ref:[^:]*:[^:]*:"; "")), .[1]] | @tsv' \
            "$TMPDIR/publish.json" |
            while IFS=$'\t' read -r file name; do
              cp "$file" "$out/$dir/$name"
              rest=''${name#"$id-$version"}
              ln -s "../../$dir/$name" "$out/${linkDirectory}/$id$rest"
            done
          ${python3}/bin/python ${purifier.projectInputsTool}/coursier_runtime.py \
            --cache "$TMPDIR/coursier-cache" --classpath "$TMPDIR/runtime-classpath.json" \
            --catalog ${provided.maven.catalog} --destination "$out/nix-support/runtime"
          ${lib.optionalString (projectDependencies != [ ]) ''
            for pom in "$out/$dir"/*.pom; do
              ${python3}/bin/python ${purifier.jvmRepositoryTool} \
                --rewrite-pom "$pom" --catalog ${provided.maven.catalog}
            done
          ''}
          # Mill writes the time of the build into its jars (zip entry times,
          # and the dates javadoc writes into its pages); the package sets them
          # to SOURCE_DATE_EPOCH, so a build of the same source gives the same
          # files.
          find "$out/$dir" -name '*.jar' -exec strip-nondeterminism --type jar {} +
          for key in ${lib.escapeShellArgs (map (a: a.key) project.artifacts)}; do
            if [ ! -e "$out/${linkDirectory}/$key" ]; then
              echo "mill-purifier: the lock names $key, which this build of ${project.id} did not publish; run mill-purifier lock" >&2
              exit 1
            fi
          done
          # What publishBuild publishes: the coordinates and the files, by the
          # names they are published under.
          jq --arg dir "$out/$dir" \
            '{group: .meta.group, id: .meta.id, version: .meta.version,
              files: [.payload[] | {name: .[1], path: ($dir + "/" + .[1])}]}' \
            "$TMPDIR/publish.json" > "$out/nix-support/publish.json"
          runHook postInstall
        '';
        passthru = {
          millProject = project;
          projections = purifier.jvmProjections {
            repository = publicationSource;
            producer = nativePublications;
            runtimeRepository = "${finalAttrs.finalPackage}/nix-support/runtime";
            dependencies = projectDependencies;
            inherit runCommand python3;
            name = "mill-jvm-publications";
          };
          # The artifact files of the package by key, such as
          # artifacts."core_3-sources.jar". They are known from the lock
          # alone, before the package is built.
          artifacts = builtins.listToAttrs (
            map (a: {
              name = a.key;
              value = artifactFile finalAttrs.finalPackage a.key;
            }) project.artifacts
          );
        };
        meta.description = "${project.name}, built by mill-purifier";
      });
    in
    package;

  packages = builtins.mapAttrs (_: mkPackage) lock.mill.projectsById;
  nativePublications = runCommand "mill-native-publications" { } ''
    ${python3}/bin/python ${./publication-views.py} ${purifier.projectInputsTool} \
      native "$out" ${lib.escapeShellArgs (map toString (builtins.attrValues packages))}
  '';

  runTask =
    name: args:
    stdenvNoCC.mkDerivation {
      inherit name;
      src = filteredSrc;
      nativeBuildInputs = [ jdk' ];
      dontConfigure = true;
      dontFixup = true;
      buildPhase = ''
        runHook preBuild
        ${runMill (lib.escapeShellArgs args)} > "$TMPDIR/stdout"
        runHook postBuild
      '';
      installPhase = ''
        cp "$TMPDIR/stdout" $out
      '';
    };

  # The test tasks the lock run ran (`testTasks`), in one Mill run. A build
  # whose lock ran no tests has an empty test run.
  tests =
    if lock.mill.testTasks == [ ] then
      runCommand "mill-tests" { } "echo 'the lock ran no test tasks' > $out"
    else
      runTask "mill-tests" (lib.intersperse "+" lock.mill.testTasks);

  # A Mill build with one module per package, whose `publishArtifacts` task
  # returns the files of the package. Mill's publishers take the tasks to
  # publish as `--publishArtifacts`, so `__.publishArtifacts` publishes what
  # Nix built. Its modules are named m0, m1, … in the order of the lock;
  # each has a comment naming the module it publishes.
  publicationClosure =
    runCommand "mill-publication-closure"
      {
        outputs = [
          "out"
          "metadata"
        ];
        producerIdentity = (purifier.inputIdentity nativePublications).hash;
        passthru.catalog = "${publicationClosure.metadata}/catalog.json";
      }
      ''
        ${python3}/bin/python ${./publication-views.py} ${purifier.projectInputsTool} \
          merge "$out" "$metadata" ${
            lib.escapeShellArgs (
              lib.concatMap (package: [
                (toString package.projections.maven)
                package.projections.maven.catalog
              ]) (builtins.attrValues packages)
            )
          }
      '';
  rootProjections.maven = publicationClosure;
  publishBuild =
    runCommand "mill-publish-build"
      {
        preferLocalBuild = true;
      }
      ''
        mkdir -p $out
        ${python3}/bin/python ${./publish-build.py} \
          ${rootProjections.maven} ${rootProjections.maven.catalog} "$out/build.mill" ${lib.escapeShellArg millVersion}
      '';

  # Mill's MavenPublishModule publishes publishBuild into a file:
  # repository, offline: the lock run publishes the same way once, so the
  # publisher's dependencies are in the lock. The publisher wants a user
  # name and a password even for a file: repository, and reads neither.
  release =
    if import ./publisher-available.nix { inherit lib; } millVersion then
      releaseDerivation
    else
      throw "mill-purifier: the build pins Mill ${millVersion}, which has no mill.javalib.MavenPublishModule, the Maven publisher that `release` uses; Mill 1.1.0 and later have it. The packages and the tests of this build work.";
  releaseDerivation = stdenvNoCC.mkDerivation {
    name = "mill-release";
    src = publishBuild;
    nativeBuildInputs = [ jdk' ];
    dontConfigure = true;
    dontFixup = true;
    buildPhase = ''
      runHook preBuild
      chmod -R u+w .
      export MILL_MAVEN_USERNAME=mill-purifier
      export MILL_MAVEN_PASSWORD="$MILL_MAVEN_USERNAME"
      ${runMillWithInputs false ''
        mill.javalib.MavenPublishModule/publishAll \
          --publishArtifacts __.publishArtifacts \
          --releaseUri "file://$out" --snapshotUri "file://$out"''}
      runHook postBuild
    '';
    dontInstall = true;
  };
in
{
  projections = rootProjections;
  inherit
    lock
    packages
    tests
    release
    runTask
    publishBuild
    dependencies
    archiveCache
    ;
  # The downloads of the lock, for every system it was made for.
  fetches = lock.entries;
  jdk = jdk';
}
