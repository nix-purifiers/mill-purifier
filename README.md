# mill-purifier

mill-purifier keeps Mill's development workflow on locked,
Nix-resolved dependencies. Use the normal build and test commands in your
working tree; building the project itself in the Nix sandbox is an
optional next step.

## Quickstart

From the project root:

```zsh
nix shell 'github:nix-purifiers/mill-purifier'
mill-purifier lock
mill-purifier mill core.compile
mill-purifier mill core.test
```

Commit the lockfiles that the lock command writes, including the build
tool's own lockfile when it has one. Keep using the project's usual build,
test and editor commands with this setup. Nix provides the locked
dependencies; the development commands above run outside the Nix sandbox.
No project Nix files are needed for this workflow.

Replace `core` with a module of your build.

Sandboxed builds of the whole project are an optional
[next step](#building-the-project-with-nix).

The required wrapper is `mill-purifier mill`; pass your usual tool
arguments after it. With no purifier lockfile, it warns on standard error
and runs the ordinary tool with impure resolution.

For persistent use, add these packages through your usual Nix configuration.


## Development loop

Mill has no plugin mechanism that reaches every build, so the command
wraps Mill from outside, and the development loop goes through
`mill-purifier mill`, which runs Mill's own launcher with its arguments,
offline, on the locked downloads:

```zsh
mill-purifier mill core.compile
mill-purifier mill core.test
mill-purifier mill core.run
```

To add or upgrade a dependency, edit `build.mill`, run `mill-purifier
lock`, and commit the lockfile. Until then `mill-purifier mill` warns that
the build definition changed, and a dependency that is not in the lock
fails to resolve, after which the command names `mill-purifier lock`.

`mill-purifier lock` runs the build in a copy without its `out/` directory,
so a lock never depends on what an earlier run left. `--tests <selector>`
chooses the test tasks the lock run runs, so that their runtime
dependencies are locked, and `--tests none` runs none:

```zsh
mill-purifier lock --tests 'core.test.testForked'
```

The default is every `testForked` task (`__.testForked`). The lockfile
records the selector (`testSelector`), a later `lock` without `--tests`
uses it again, and `tests` runs the test tasks the lock run ran. A test
that needs tools outside the JVM, such as a Scala Native test that needs
clang, needs them in the lock run and in the sandbox.

Mill has no lockfile of its own, and a version in `build.mill` names one
version, except for a version range such as `[2.9.0,2.11.0)`. Coursier
resolves a range against the version listing of the module
(`maven-metadata.xml`), and the lockfile records each listing that the
build needs. `lock` keeps the resolution: the lock run reads the listings
from the lockfile, so a range resolves as it did. `update` reads them
afresh and relocks, and with `org:artifact` names it does so only for
those modules:

```zsh
mill-purifier update
mill-purifier update com.lihaoyi:upickle_3
```

Every command takes `--lockfile <path>` first, for a lockfile kept outside
the build. Without it, a command reads the environment variable
`MILL_PURIFIER_LOCKFILE`, so the dev shell of a packaging flake can set it
to the path the flake passes as `lockFile`.

A Mill daemon keeps the environment it was started with. Stop a daemon
that plain `./mill` started (`./mill shutdown`) before using
`mill-purifier mill`, and the other way around.

### Publishing

```zsh
mill-purifier publish mill.javalib.SonatypeCentralPublishModule/
```

builds every package with Nix, writes a Mill build whose `publishArtifacts`
tasks return the files of the packages, and runs the publisher that you
name on it, so the upload goes through the publisher of your choice and
not through `release`.
Every Mill publisher takes the tasks to publish as `--publishArtifacts`,
and the command adds `--publishArtifacts __.publishArtifacts` unless the
arguments name it. Credentials and signing work as they do for the
publisher in a plain Mill build, through its environment variables and
arguments. Resolution runs online and starts from the locked downloads,
because a publisher can need libraries that the lock run did not use.

## Dependencies from another purified project

Pass a producer's flake package to `lock`:

```zsh
mill-purifier lock \
  --input 'github:owner/library/<rev>#library'
mill-purifier prefetch
```

The producer must export a Maven view through `projections.maven`. Its
catalog selects the supplied modules automatically, including their Nix-derived
versions. Keep ordinary dependency declarations in the project; no version
rewrite table is needed. Repeat `--input` for additional producers.

The lock records the producer's pinned Nix source, rather than listing its
artifacts as downloads. Relocking retains that pin; `update` refreshes it.
Conflicting providers of the same module are rejected. For a multi-module
producer, select its individual package when modules use different versions
of the same dependency; each package exports its own runtime closure.

For a Nix build definition, the corresponding builder argument is
`projectDependencies = [ producer.packages.${system}.library ];`. By default,
the builder reads the inputs recorded in the lock.

Projected repositories use immutable, Nix-derived versions. The purifier's
publish command deploys those coordinates and supplied dependencies, with
POM references rewritten so ordinary Maven consumers can resolve them.

## Building the project with Nix

Once the native development workflow is working, you can also build and
test the whole project in the Nix sandbox. For this step, keep a flake or
another Nix build definition in the project's repository, or maintain an
external packaging repository that points at its sources and lockfiles.
The builder consumes the same lockfiles as the development workflow.

The command builds the module `core`. The overlay of the flake,
`overlays.default`, adds one attribute to the package set: the scope
`mill-purifier`, which holds the builder `buildMillProject` beside the
`mill-purifier` command.

A project whose authors want a flake that packages it can have one, in the
repository of the project or in another repository. The flake calls the
builder directly:

```nix
{
  inputs.mill-purifier.url =
    "github:nix-purifiers/mill-purifier";
  inputs.nixpkgs.follows = "mill-purifier/nixpkgs";

  outputs =
    { nixpkgs, mill-purifier, ... }:
    let
      system = "x86_64-linux";
      pkgs =
        nixpkgs.legacyPackages.${system}.extend
          mill-purifier.overlays.default;
      build = pkgs.mill-purifier.buildMillProject {
        src = ./.;
      };
    in
    {
      packages.${system}.core = build.packages.core;
      checks.${system}.tests = build.tests;
    };
}
```

`nix build '.#core'` builds the module `core`, and `nix flake check` runs
the tests. The builder takes the packages it needs by name, such as
`stdenvNoCC` and `jq`. `mill-purifier.buildMillProject.override` replaces any
of them, and `mill-purifier.overrideScope` replaces what everything in the
scope shares.

### What the builder takes and returns

`buildMillProject` takes `src` and these arguments, which
`pkgs/build-mill-project/default.nix` documents:

- `fetchers`: the fetchers of the purifier library, such as a `fetchurl`
  that sends credentials or uses a company mirror. The default is the
  fetchers of nixpkgs.
- `lockFile`: the lockfile. The default is `mill-purifier.lock.json` in
  `src`, which is the recommended setup: commit the lockfile to the
  repository of the project. A packaging flake for a project whose author
  keeps no lockfile passes its own here, and it replaces any copy in `src`.
- `jdk`: the JDK that runs Mill's launcher. The default is the headless
  OpenJDK 21 that `pkgs/build-mill-project/jdk.nix` names, which the `mill-purifier`
  command also uses.
- `git`: the commit the source is of, for a version from git (see
  [Versions from git](#versions-from-git)).
- `srcFilter`, and `allowStaleLock = true` to build from a stale lockfile.

It returns:

- `packages.<module>`, one per module that extends `PublishModule`, named
  by its module path, such as `core` or `foo.bar`. A package holds the
  files the module publishes in Maven layout, and its `artifacts`
  attribute holds each file by a name without the version, such as
  `packages.core.artifacts."core_3.jar"`. The names come from the
  lockfile, so Nix knows them when it evaluates.
- `tests`: the test tasks the lock run ran, run in the sandbox.
- `release`: every package published by Mill's own Maven publisher
  (`mill.javalib.MavenPublishModule`) into a Maven repository, with its
  checksums and `maven-metadata.xml`. Mill 1.1.0 is the first release with
  that publisher: on an older Mill, `release` fails when evaluated, with a
  message that names Mill 1.1.0, and the packages and the tests build.
- `fetches`: the entries of the lock, the downloads of the build as
  purifier library entries. An entry needed on one system only, such as a
  JVM archive, carries `systems`.
- `runTask "name" [ "core.run" ]`: a derivation that runs Mill with those
  arguments offline in the sandbox, and holds what Mill printed.
- `lock`: the lock in memory, as `readLock` gives it.

### Reading the lock

`lib.mill-purifier.readLock { src, lockFile ? src + "/mill-purifier.lock.json" }`
reads the lockfile into the shape that the purifier contract gives every
purifier: `{ purifier, entries, outputs, problems }`. A tool that reads
the downloads of every purifier, such as a software bill of materials
(SBOM) generator, calls it rather than parsing the lockfile.

- `entries` are the downloads, with their URLs and mirrors derived.
- `outputs.<module>` holds the `name` and the `artifacts` keys of each
  package.
- `problems` holds a message for a stale, tampered, malformed or
  other-version lockfile, each naming `mill-purifier lock`. It is empty
  for a usable lockfile. A lockfile that is not JSON fails evaluation.

The builder passes the result to `checkLock` of the purifier library,
which stops evaluation with the problems, and takes the entries of its
system from `entriesFor`.

The packages are deterministic. Mill writes the time of the build into its
jars, and the package build normalizes them with `strip-nondeterminism`.
It also sets `java.properties.date`, which replaces the date in the
`.properties` files that `java.util.Properties` writes, such as those of
Mill's BuildInfo plugin.

### One-off builds without a project Nix file

For an ad hoc build, the builder can also be evaluated directly from the
project root. Replace the example package name with one from your project:

```zsh
nix build --impure --expr 'let
  purifier = builtins.getFlake "github:nix-purifiers/mill-purifier";
  pkgs = import purifier.inputs.nixpkgs {
    system = builtins.currentSystem;
    overlays = [ purifier.overlays.default ];
  };
in
(pkgs.mill-purifier.buildMillProject { src = ./.; })
.packages.core'
```

This uses the purifier's pinned nixpkgs, matching the lock command above.
To build with ambient `<nixpkgs>` from your Nix search path instead,
replace `import purifier.inputs.nixpkgs` with `import <nixpkgs>`.
Both forms need no Nix files in your project. The build tool and language
versions must match the lockfile; with ambient nixpkgs, lock with the
purifier from that package set or pass a compatible toolchain to the builder.

## Working offline

```zsh
mill-purifier prefetch
```

builds, with Nix, everything the lockfile lists for this system: the
downloads of the build and of its tests, the JVMs, and what an editor's
build server needs. Their garbage-collector roots are links in
`.mill-purifier/` beside the lockfile, and `.mill-purifier/prefetch.json`
records the store paths and the lockfile they were built from. After a prefetch,
`mill-purifier mill` needs neither Nix nor the network while the lockfile
stays the same, so the development loop and the editor work with the network
cut off. `mill-purifier mill` prefetches by itself when no prefetch of the
current lockfile exists.

The coursier caches Mill reads are trees of links into the store under
`~/.cache/mill-purifier`, outside the build, because Mill stops a task that
reads files in the workspace that the task does not declare as sources.

## Editor

Editors such as Metals (in VS Code) and IntelliJ IDEA talk to Mill through
the Build Server Protocol (BSP), and start Mill with the command in
`.bsp/mill-bsp.json`. Write that file through the purifier:

```zsh
mill-purifier mill mill.bsp.BSP/install
```

The command it names is a script under `~/.cache/mill-purifier` that runs
`mill-purifier mill` in the build, so the editor's Mill also runs offline
from the lockfile. The lock run starts Mill's BSP server and asks it what
an editor asks first (the build targets and the sources of their
dependencies), so the server's own libraries and the source jars an editor
shows are in the lock. The script names the mill-purifier package it was
written by; run the install again after updating mill-purifier.

## Troubleshooting

| What happened | What the error says | What to run |
|---|---|---|
| The build definition changed since the lock | `the build definition (…) changed since mill-purifier.lock.json was written` from Nix, and a warning from `mill-purifier mill` | `mill-purifier lock` |
| A dependency is not in the lock | Mill's resolution error, then `the lockfile lacks it` | `mill-purifier lock` |
| The lockfile was edited by hand | `does not match its contentHash` | `mill-purifier lock` |
| A download no longer matches its hash | Nix's `hash mismatch in fixed-output derivation`, then the command's note about it | `mill-purifier lock` |
| The lockfile is not a lockfile | `is not valid JSON` or `is not a mill-purifier lockfile` | `mill-purifier lock` |
| The lockfile is from another mill-purifier | `has lock format version …` | `mill-purifier lock` |
| No lockfile | `… does not exist; run mill-purifier lock first` | `mill-purifier lock` |

The lockfile records the hash of its own content (`contentHash`), and Nix
and the command both check it, so a hand edit fails before anything is
fetched.

## Supported language repositories

The lock run records what coursier, the dependency resolver of Mill,
downloads, so it supports every source coursier resolves from:

- **Maven repositories**: Maven Central and every repository a module
  names in `repositories`, such as `https://maven.google.com`.
- **The meta-build**: the plugins and libraries of the build definition,
  such as `//| mvnDeps: ["com.lihaoyi::mill-contrib-buildinfo:$MILL_VERSION"]`.
- **Mill itself**: its launcher jar, `mill-dist-<version>.exe`, which the
  command downloads, and the Mill daemon, which the launcher resolves
  through coursier.
- **JVMs** that Mill downloads for its daemon (`mill-jvm-version`) or for a
  module (`jvmId`, `jvmVersion`), for x86_64-linux and aarch64-linux (see
  [JVMs](#jvms)).
- **Mill's Maven publisher**, which the lock run runs once into a local
  repository, so that `release` publishes offline.
- **Mill's BSP server** and the source jars editors read.
- **Version listings** (`maven-metadata.xml`) for version ranges and
  `latest` selectors, as described below.

These limits follow from how the lock is made:

- **Mill 0.x**: Mill 1.x changed the build file format, the launcher and
  the way Mill resolves itself, and the lock run and the sandboxed build
  rely on the 1.x behavior.
- **`release` on Mill before 1.1.0**: Mill 1.1.0 is the first release with
  `mill.javalib.MavenPublishModule`, the Maven publisher that the lock run
  runs once and that `release` uses. A build on Mill 1.0.x or `1.1.0-RC1`
  locks without that step, and its packages and tests build. Its `release`
  fails when evaluated, with a message that names Mill 1.1.0.
- **Downloads that need credentials**: the lockfile records URLs and
  hashes, and no credentials. The build fetches them with credentials
  through `fetchers.fetchurl`, but a lock run cannot record them.
- **Files a build downloads outside coursier**: the lock run records what
  coursier downloads, so a file that build code fetches with its own HTTP
  client, such as a task that calls `requests.get`, is in no lockfile. The
  sandbox has no network, so such a task fails, and so does a build that
  depends on it. Kotlin's documentation tool, Dokka, fetches the package
  lists of external API documentation; in the sandbox it warns, and the
  generated documentation has no links to the Java and Kotlin standard
  library documentation.
- **Systems other than x86_64-linux and aarch64-linux**, for a build that
  asks for a JVM Mill downloads: the lock holds JVM archives for those two.
- Only modules that extend `PublishModule` become packages: Mill does not
  say which files any other module produces, while `publishArtifacts`
  names every file a published module produces.

### How the lock is made

`mill-purifier lock` copies the build to `out/mill-purifier/lock-run`, and
runs Mill there with an empty coursier cache. The run downloads Mill's
launcher from Maven Central, resolves the published modules
(`__.publishArtifacts`) and the test tasks, compiles every module, shows
the published files, starts the BSP server, runs the tests, and publishes
once into a local repository with Mill's Maven publisher. Every file the
run downloads goes into the lockfile as `{ path, hash }`, with `path` its
place in the coursier cache. The lockfile holds only what Nix cannot
derive: the URL follows from the path, as coursier stores
`https://host/a/b.jar` at `https/host/a/b.jar`, and a file from Maven
Central is also served by Google's mirror of it, so Nix expands each entry
into a file entry of the purifier library, `{ path, url, mirrors, hash }`.
The Mill version comes from the build, and the launcher from the version.

A `maven-metadata.xml` file lists the published versions of a module, and
its content changes whenever upstream publishes a version, so a hash of it
stops matching. The lock leaves listings out when an offline build of the
copy succeeds without them. When the build needs them, the lock keeps each
listing as the lock run read it, as an inline entry `{ path, contents }`,
and the offline build resolves to the versions the lock run picked.

The lockfile records a hash of the build definition (`definitionHash`):
the `*.mill` and `*.mill.yaml` files, everything under `mill-build/`, and
Mill's `.mill-*` and `.config/mill-*` settings files. `buildMillProject`
computes the same hash from the source, so it can tell a stale lockfile
from a current one.

When Maven Central rate-limits the machine, the lock run can resolve
through a mirror of it:

```zsh
MILL_PURIFIER_MAVEN_CENTRAL_MIRROR=https://maven-central.storage-download.googleapis.com/maven2 \
  mill-purifier lock
```

The lockfile is the same either way. It records each file at the path of
Maven Central, and the Nix build fetches it from Maven Central and then
from Google's mirror.

### JVMs

Mill runs its daemon on the JVM that `mill-jvm-version` names, which it
downloads through coursier by default, and a module that sets `jvmId` or
`jvmVersion` compiles and runs on a JVM that coursier downloads the same
way. Coursier unpacks such an archive into its archive cache, and the lock
records each archive the lock run unpacked. A prebuilt JDK expects a
filesystem hierarchy standard (FHS) system and does not run on Nix as
shipped, so `buildMillProject` unpacks each locked archive into the archive
cache itself and patches its executables and libraries with nixpkgs'
`autoPatchelfHook`. Coursier then finds the JVM unpacked and uses it.

Coursier chooses a JVM archive from a JVM index published for each
platform (`io.get-coursier.jvm.indices:index-linux-amd64`, and
`index-linux-arm64` for aarch64). The lock run finds the name and version
of each archive it unpacked in the index of its own platform, downloads the
index of the same version for the other Linux platform, and locks the
archive of the same JVM there. The lockfile holds both under `platforms`,
by Nix system, and a build reads only those of the system it builds for, so
one lockfile builds on x86_64-linux and aarch64-linux. A build that sets
`//| mill-jvm-version: system` and no module JVM runs Mill on a JDK from
nixpkgs and downloads no JVM.

On NixOS, a lock run for a build that asks for a downloaded JVM needs
nix-ld (or another way to run FHS binaries), because the lock run runs the
JVM before Nix has patched it.

### Versions from git

The Nix sandbox has no git repository: a flake's source has no `.git`
directory. Mill's `VcsVersion` (`mill.util.VcsVersionModule`) computes a
version from five git queries, and `buildMillProject` answers them with a
`git` command it puts in `PATH`, from what the caller says about the
commit:

```nix
pkgs.mill-purifier.buildMillProject {
  src = ./.;
  git = {
    inherit (self) rev revCount;
    # At or after a tag:
    tag = "v1.2.0";
    commitsSinceTag = 0;
  };
}
```

A commit at the tag gets the version `1.2.0`, and one 5 commits after it
gets `1.2.0-5-<six digits of rev>`, as VcsVersion formats them. The lock
cannot record the tag: tagging a release does not change the build
definition, so the lock would not be renewed, and the build would publish
the version of an old tag. Without `git`, VcsVersion sees no repository
and reports `no-vcs`, as it does for a source tree without git.

## Examples

For day-to-day work on these projects, use the native-tool setup and
commands in [Quickstart](#quickstart) and [Development loop](#development-loop).
The Nix expressions below show optional sandboxed packaging of those
projects.

### fansi

[fansi](https://github.com/com-lihaoyi/fansi) is a Scala library for
strings with ANSI colors. Its build is a Mill build for the JVM, Scala.js
and Scala Native, cross-built for three Scala versions. To build version
0.5.1, lock a checkout of the tag, and build the JVM package for Scala
2.13.15 from the same directory:

```zsh
nix shell 'github:nix-purifiers/mill-purifier'
git clone --branch 0.5.1 https://github.com/com-lihaoyi/fansi
cd fansi
mill-purifier lock --tests \
  'fansi.jvm[2.13.15].test.testForked'
nix build --impure --expr 'let
  purifier = builtins.getFlake "github:nix-purifiers/mill-purifier";
  pkgs = import purifier.inputs.nixpkgs {
    system = builtins.currentSystem;
    overlays = [ purifier.overlays.default ];
  };
  checkout = builtins.fetchGit ./.;
in
(pkgs.mill-purifier.buildMillProject {
  src = ./.;
  git = {
    inherit (checkout) rev revCount;
    tag = "0.5.1";
    commitsSinceTag = 0;
  };
}).packages."fansi.jvm[2.13.15]"'
```

`--tests` names the tests that the lock run runs, so that their
dependencies are in the lockfile: here, the JVM tests for Scala 2.13.15.
fansi takes its version from git, and the `git` argument tells the build
that the source is the commit of the tag, so the package is version 0.5.1.
The result holds the jar and the POM in Maven layout. The other modules are
`packages."fansi.js[3.3.6]"`, `packages."fansi.native[2.12.20]"` and so on.
fansi builds with Mill 1.0.0, which has no Maven publisher, so `release`
needs a project on Mill 1.1.0 or later.

## Credits

The purifier idea was inspired by nixcloud's
[cargo (libnix)](https://github.com/nixcloud/cargo).
