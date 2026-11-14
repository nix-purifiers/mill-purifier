# SPDX-License-Identifier: MIT
"""mill-purifier: locks the downloads of a Mill build and runs Mill on them.

  mill-purifier lock [--tests SELECTOR]
                              runs Mill with an empty cache and writes
                              mill-purifier.lock.json at the build root. The
                              lock run runs the test tasks SELECTOR names
                              (`none` for none); the default is the selector
                              of the existing lockfile, or __.testForked. It
                              keeps the resolution of the existing lockfile:
                              a version range resolves against the version
                              listings it recorded
  mill-purifier update [ORG:NAME...]
                              locks like lock, with the version ranges of
                              the build resolved again against the current
                              version listings: of every dependency, or of
                              the dependencies named
  mill-purifier prefetch      builds everything the lockfile lists for this
                              system with Nix, under GC roots in
                              .mill-purifier beside the lockfile, so Mill
                              and editors then run without the network
  mill-purifier mill ARGS...  runs Mill offline, from the locked downloads
  mill-purifier publish PUBLISHER ARGS...
                              builds the packages with Nix and runs a Mill
                              publisher on what Nix built, such as
                              mill.javalib.SonatypeCentralPublishModule/

Every verb takes `--lockfile PATH` first, for a lockfile kept outside the
build. Without it, the command reads MILL_PURIFIER_LOCKFILE, and then the
lockfile at the build root.

The command runs from the root of the Mill build (the directory with
build.mill or build.mill.yaml). Its Nix package sets MILL_PURIFIER_JDK (the
JDK that runs Mill's launcher), MILL_PURIFIER_LIB (the Nix library),
MILL_PURIFIER_NIXPKGS (the nixpkgs the library builds with) and
MILL_PURIFIER_PURIFIER_SOURCE (the purifier library), and puts curl in
PATH. prefetch and publish, and mill before a prefetch, run the nix-build
command of the Nix installation in PATH.
"""

import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import coursier_cache

LOCK_VERSION = 2
LOCK_FILE = "mill-purifier.lock.json"
ROOT_BUILD_FILES = ("build.mill.yaml", "build.mill")
MAVEN_CENTRAL = "https://repo1.maven.org/maven2"
# Google's mirror of Maven Central, which serves the same files. Every lock
# entry for a Maven Central file names it in `mirrors`. A lock run resolves
# through the mirror that MILL_PURIFIER_MAVEN_CENTRAL_MIRROR names, when it
# is set, and records Maven Central's own paths and URLs.
MAVEN_CENTRAL_MIRROR = "https://maven-central.storage-download.googleapis.com/maven2"
# Directories that never take part in a build, as buildMillProject's
# default source filter leaves them out.
SKIPPED_DIRS = {"out", ".git", ".bsp", ".idea", ".metals", ".bloop", ".mill-purifier"}
# The XDG base directories of a lock run, under its home directory.
XDG_DIRECTORIES = {
    "XDG_CACHE_HOME": ".cache",
    "XDG_CONFIG_HOME": ".config",
    "XDG_DATA_HOME": ".local/share",
    "XDG_STATE_HOME": ".local/state",
}
# The directory beside the lockfile that holds the GC roots of a prefetch.
PREFETCH_DIR = ".mill-purifier"


class Failure(Exception):
    pass


def log(message):
    print(f"[mill-purifier] {message}", file=sys.stderr)


# --- The build root and its configuration ---------------------------------


def build_file(root):
    for name in ROOT_BUILD_FILES:
        if (root / name).is_file():
            return root / name
    raise Failure(f"{root} has no {' or '.join(ROOT_BUILD_FILES)}; run mill-purifier from the root of a Mill build")


def header_config(file):
    """The `key: value` settings of a build file's header: the leading `//|`
    lines of build.mill, or the top-level keys of build.mill.yaml."""
    settings = {}
    lines = file.read_text().splitlines()
    if file.name.endswith(".yaml"):
        header = [l for l in lines if l and not l[0].isspace()]
    else:
        header = []
        for line in lines:
            if line.startswith("//|"):
                header.append(line[3:].strip())
            elif line.strip() and not line.startswith("#!"):
                break
    for line in header:
        key, sep, value = line.partition(":")
        if sep:
            settings[key.strip()] = value.strip().strip("\"'")
    return settings


def mill_config(root, key):
    """A Mill setting as Mill's launcher reads it: from .<key>, then from
    .config/<key>, then from the build file header."""
    for file in (root / f".{key}", root / ".config" / key):
        if file.is_file():
            lines = [l.strip() for l in file.read_text().splitlines() if l.strip() and not l.startswith("#")]
            return lines[0] if lines else None
    return header_config(build_file(root)).get(key)


def definition_files(root):
    """The files that define a Mill build: every *.mill and *.mill.yaml file,
    everything under mill-build/, and Mill's .mill-* and .config/mill-*
    settings files."""
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIPPED_DIRS)
        rel_dir = Path(dirpath).relative_to(root)
        in_meta_build = rel_dir.parts[:1] == ("mill-build",)
        in_config = rel_dir == Path(".config")
        for name in filenames:
            if (
                in_meta_build
                or name.endswith((".mill", ".mill.yaml"))
                or (rel_dir == Path(".") and name.startswith(".mill-"))
                or (in_config and name.startswith("mill-"))
            ):
                found.append((rel_dir / name).as_posix())
    return sorted(found)


def definition_hash(root):
    """The hash of the build definition: the SHA-256 of the compact JSON
    list of [path, SHA-256 of the file] for each definition file, in
    hexadecimal. lib-overlays/default/definition-hash.nix computes the same value, so a
    Nix build can tell a stale lockfile from a current one."""
    files = [[rel, hashlib.sha256((root / rel).read_bytes()).hexdigest()] for rel in definition_files(root)]
    listing = json.dumps(files, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(listing.encode()).hexdigest()


# --- Running Mill ---------------------------------------------------------


def jdk_home():
    home = os.environ.get("MILL_PURIFIER_JDK")
    if not home:
        raise Failure("MILL_PURIFIER_JDK is not set; run mill-purifier through its Nix package")
    return Path(home)


def launcher_url(version):
    return f"{MAVEN_CENTRAL}/com/lihaoyi/mill-dist/{version}/mill-dist-{version}.exe"


def launcher_cache_path(root):
    """The place in the coursier cache of the launcher of the Mill version
    the build pins, where the lock run downloaded it (pkgs/build-mill-project/mill-version.nix
    derives the same)."""
    version = mill_config(root, "mill-version")
    if not version:
        raise Failure("the build does not pin a Mill version; set `//| mill-version: <version>` in build.mill or write .mill-version")
    return coursier_cache.cache_path(launcher_url(version))


def mill_env(cache, archive_cache, home, tmp, offline):
    jdk = jdk_home()
    env = dict(os.environ)
    env["PATH"] = f"{jdk / 'bin'}{os.pathsep}{env.get('PATH', '')}"
    env["JAVA_HOME"] = str(jdk)
    env["COURSIER_CACHE"] = str(cache)
    env["COURSIER_ARCHIVE_CACHE"] = str(archive_cache)
    if offline:
        env["COURSIER_MODE"] = "offline"
    jvm = [f"-Djava.io.tmpdir={tmp}"]
    if home is not None:
        jvm.append(f"-Duser.home={home}")
        # The launcher and Mill put caches and settings under HOME and the
        # XDG directories, which a lock run takes from its own directory and
        # not from the user running it.
        env["HOME"] = str(home)
        for variable, subdirectory in XDG_DIRECTORIES.items():
            env[variable] = str(home / subdirectory)
        env.pop("XDG_CONFIG_DIRS", None)
        env.pop("XDG_DATA_DIRS", None)
    if not offline:
        jvm += coursier_cache.java_proxy_properties(env)
    # Mill passes JAVA_OPTS to the JVM it starts for its daemon.
    env["JAVA_OPTS"] = " ".join([*jvm, env.get("JAVA_OPTS", "")]).strip()
    return env, jvm


def mill_command(launcher, jvm, args):
    return [str(jdk_home() / "bin" / "java"), *jvm, "-jar", str(launcher), *args]


def plus(tasks):
    """The arguments that run several tasks in one Mill invocation, which
    takes `+` between them."""
    args = []
    for t in tasks:
        if args:
            args.append("+")
        args.append(t)
    return args


# --- lock -----------------------------------------------------------------


def copy_build(root, dest):
    def ignore(directory, names):
        skipped = SKIPPED_DIRS if Path(directory) == root else {".git"}
        return [n for n in names if n in skipped]

    shutil.copytree(root, dest, ignore=ignore, symlinks=True)


# The Nix systems a lock covers, by the platform name of the coursier JVM
# index for that system (io.get-coursier.jvm.indices:index-<platform>).
JVM_INDEX_PLATFORMS = {"x86_64-linux": "linux-amd64", "aarch64-linux": "linux-arm64"}
JVM_INDEX_DIR = "https/repo1.maven.org/maven2/io/get-coursier/jvm/indices"
_JVM_INDEX_JAR = re.compile(
    rf"^{re.escape(JVM_INDEX_DIR)}/index-(?P<platform>[^/]+)/(?P<version>[^/]+)/index-(?P=platform)-(?P=version)\.jar$"
)


def jvm_index_files(index_platform, version):
    """The cache paths of the coursier JVM index for `index_platform`, a jar
    holding coursier/jvm/indices/v1/<index_platform>.json and its POM."""
    base = f"{JVM_INDEX_DIR}/index-{index_platform}/{version}/index-{index_platform}-{version}"
    return [f"{base}.jar", f"{base}.pom"]


def read_jvm_index(jar, index_platform):
    """The JVM index in `jar`: {name: {version: "<type>+<url>"}}."""
    with zipfile.ZipFile(jar) as z:
        return json.loads(z.read(f"coursier/jvm/indices/v1/{index_platform}.json"))


def archive_url(spec):
    """The URL of an index entry such as `tgz+https://…`, without its type."""
    return spec.split("+", 1)[1] if "+" in spec.split("://", 1)[0] else spec


def jvms_of(index, urls):
    """The (name, version) each URL has in the JVM index."""
    by_url = {archive_url(spec): (name, version) for name, versions in index.items() for version, spec in versions.items()}
    missing = [u for u in urls if u not in by_url]
    if missing:
        raise Failure(f"the coursier JVM index names no JVM for {', '.join(missing)}")
    return [by_url[u] for u in urls]


def resolution_mirror():
    """The Maven Central mirror a lock run resolves through, or None."""
    mirror = os.environ.get("MILL_PURIFIER_MAVEN_CENTRAL_MIRROR", "").rstrip("/")
    return mirror or None


def download(url, dest):
    mirror = resolution_mirror()
    source = mirror + url[len(MAVEN_CENTRAL) :] if mirror and url.startswith(MAVEN_CENTRAL + "/") else url
    dest.parent.mkdir(parents=True, exist_ok=True)
    if subprocess.run(["curl", "-fsSL", "--retry", "3", "-o", str(dest), source]).returncode != 0:
        raise Failure(f"cannot download {source}")


# The mirrors every lock entry names, by repository (see coursier_cache.entries).
MIRRORS = {MAVEN_CENTRAL: [MAVEN_CENTRAL_MIRROR]}


def lock_entries(cache, paths):
    """The lock entries of `paths` in `cache`, as the lockfile records them:
    { path, hash }. A URL follows from its cache path, and the mirrors of
    Maven Central from the URL, so lib-overlays/default/read-lock.nix derives both, and the
    lockfile holds only what the lock run observed."""
    return [{"path": e["path"], "hash": e["hash"]} for e in coursier_cache.entries(cache, paths, mirrors=MIRRORS)]


def mirror_env(env, state):
    """`env` for a lock run that resolves through the Maven Central mirror:
    COURSIER_MIRRORS for what Mill resolves, COURSIER_REPOSITORIES for the
    launcher, which resolves Mill itself and reads only that."""
    mirror = resolution_mirror()
    if mirror is None:
        return env
    conf = state / "coursier-mirror.properties"
    conf.write_text(f"central.from={MAVEN_CENTRAL}\ncentral.to={mirror}\n")
    return {**env, "COURSIER_MIRRORS": str(conf), "COURSIER_REPOSITORIES": f"ivy2Local|{mirror}"}


def canonical_cache(cache):
    """Moves what a lock run downloaded through the Maven Central mirror to
    the cache paths of Maven Central itself, where the offline builds, which
    use no mirror, read them."""
    mirror = resolution_mirror()
    if mirror is None:
        return
    source = cache / coursier_cache.cache_path(mirror + "/")
    source = source.parent  # cache_path of a URL ending in / ends in .directory
    target = (cache / coursier_cache.cache_path(MAVEN_CENTRAL + "/")).parent
    if not source.is_dir():
        return
    for dirpath, _, filenames in os.walk(source):
        for name in filenames:
            src = Path(dirpath, name)
            dest = target / src.relative_to(source)
            dest.parent.mkdir(parents=True, exist_ok=True)
            os.replace(src, dest)
    shutil.rmtree(source)


def lock_platforms(kept, archives, cache, state):
    """The platform-specific part of a lock whose build unpacked JVM
    archives: for each system of JVM_INDEX_PLATFORMS, the JVM index and the
    archives of the same JVMs, as {system: {"artifacts", "archives"}}. The
    lock run downloaded them for its own system; for each other system it
    downloads the index of the same version and the archives that index
    names for the same JVM names and versions."""
    own = nix_system()
    own_platform = JVM_INDEX_PLATFORMS.get(own)
    indices = [m for m in (_JVM_INDEX_JAR.match(p) for p in kept) if m and m["platform"] == own_platform]
    if own_platform is None or len(indices) != 1:
        raise Failure(f"the lock run unpacked JVM archives, but found no single coursier JVM index for {own}")
    index_version = indices[0]["version"]
    own_files = jvm_index_files(own_platform, index_version)
    index = read_jvm_index(cache / own_files[0], own_platform)
    jvms = jvms_of(index, [coursier_cache.url_of(a) for a in archives])
    platforms = {own: {"artifacts": lock_entries(cache, [*own_files, *archives])}}
    for system, index_platform in sorted(JVM_INDEX_PLATFORMS.items()):
        if system == own:
            continue
        platform_cache = state / "lock-platform-cache" / system
        shutil.rmtree(platform_cache, ignore_errors=True)
        files = jvm_index_files(index_platform, index_version)
        for f in files:
            download(coursier_cache.url_of(f), platform_cache / f)
        other = read_jvm_index(platform_cache / files[0], index_platform)
        other_archives = []
        for name, version in jvms:
            spec = other.get(name, {}).get(version)
            if spec is None:
                raise Failure(
                    f"the coursier JVM index for {index_platform} has no {name} {version}, which the build uses on {own}"
                )
            path = coursier_cache.cache_path(archive_url(spec))
            log(f"downloading {name} {version} for {system}")
            download(archive_url(spec), platform_cache / path)
            other_archives.append(path)
        platforms[system] = {
            "artifacts": lock_entries(platform_cache, [*files, *other_archives]),
        }
    # The lockfile does not depend on the system of the lock run, so the
    # systems come in alphabetical order, not with the own system first.
    return dict(sorted(platforms.items())), set(own_files) | set(archives)


def nix_system():
    """The Nix system name of this machine, such as x86_64-linux."""
    machine = {"amd64": "x86_64", "arm64": "aarch64"}.get(platform.machine().lower(), platform.machine().lower())
    kernel = {"darwin": "darwin"}.get(sys.platform, "linux")
    return f"{machine}-{kernel}"


def run_mill(work, env, jvm, launcher, args, capture=False):
    cmd = mill_command(launcher, jvm, ["--no-daemon", "--ticker", "false", *args])
    return subprocess.run(
        cmd,
        cwd=work,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE if capture else None,
        text=True,
    )


class Bsp:
    """A client of the Build Server Protocol (BSP), the JSON-RPC protocol
    editors use to talk to Mill, over the standard input and output of a
    Mill started with --bsp."""

    def __init__(self, cmd, cwd, env):
        self.proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        self.next_id = 0

    def send(self, message):
        body = json.dumps(message).encode()
        self.proc.stdin.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
        self.proc.stdin.flush()

    def receive(self):
        headers = {}
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise Failure("Mill's BSP server stopped before it answered")
            line = line.decode().strip()
            if not line:
                break
            key, _, value = line.partition(":")
            headers[key.strip().lower()] = value.strip()
        return json.loads(self.proc.stdout.read(int(headers["content-length"])))

    def request(self, method, params):
        self.next_id += 1
        self.send({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params})
        while True:
            reply = self.receive()
            # Notifications (logs, progress) arrive between the replies.
            if reply.get("id") == self.next_id:
                if "error" in reply:
                    raise Failure(f"Mill's BSP server answered {method} with {reply['error']}")
                return reply.get("result")

    def close(self):
        try:
            self.request("build/shutdown", None)
            self.send({"jsonrpc": "2.0", "method": "build/exit"})
            self.proc.wait(timeout=60)
        finally:
            if self.proc.poll() is None:
                self.proc.kill()
                self.proc.wait()
            self.proc.stdin.close()
            self.proc.stdout.close()


def lock_editor_support(work, env, jvm, launcher):
    """Starts Mill's BSP server as an editor does, and asks it what an editor
    asks first: the build targets and the sources of their dependencies.
    The server's own libraries and the dependency sources it resolves then
    are in the lock, so an editor gets a working build server from
    interactive mode."""
    bsp = Bsp(mill_command(launcher, jvm, ["--bsp", "--jobs", "1"]), work, env)
    try:
        bsp.request(
            "build/initialize",
            {
                "displayName": "mill-purifier",
                "version": "1",
                "bspVersion": "2.2.0",
                "rootUri": work.resolve().as_uri(),
                "capabilities": {"languageIds": ["java", "scala", "kotlin"]},
            },
        )
        bsp.send({"jsonrpc": "2.0", "method": "build/initialized", "params": {}})
        targets = bsp.request("workspace/buildTargets", {})["targets"]
        ids = [t["id"] for t in targets]
        if ids:
            bsp.request("buildTarget/dependencySources", {"targets": ids})
    finally:
        bsp.close()


def resolve(work, env, jvm, launcher, selector):
    """The tasks a selector names, or none if it names nothing."""
    result = run_mill(work, env, jvm, launcher, ["resolve", selector], capture=True)
    if result.returncode != 0:
        return []
    return [l.strip() for l in result.stdout.splitlines() if l.strip()]


# These are declarations in project modules, not Mill's bootstrap classpath.
# Evaluating them does not resolve the project dependencies or compile sources.
PROJECT_DEPENDENCIES = "{__.mvnDeps,__.runMvnDeps,__.compileMvnDeps,__.mandatoryMvnDeps}"


def stage_project_inputs(cache, views, work, env, jvm, launcher, lock_file=None, resolve_metadata=False):
    import project_inputs
    supplied = project_inputs.stage_coursier(cache, views, work, lock_file, evaluated=[])
    shown = run_mill(work, env, jvm, launcher, ["show", PROJECT_DEPENDENCIES], capture=True)
    if shown.returncode != 0:
        raise Failure("Mill could not evaluate the project's native dependency declarations:\n" + shown.stderr)
    try:
        evaluated = json.loads(shown.stdout)
    except ValueError as error:
        raise Failure(f"Mill returned invalid dependency JSON: {error}") from None
    supplied.update(project_inputs.stage_coursier(cache, views, work, lock_file, evaluated=evaluated))
    if resolve_metadata:
        # Mill discovers constraints in dependency POMs during resolution. A
        # first lock has none cached yet. Restage after that native discovery,
        # including when an unavailable supplied version made resolution fail.
        # Retry only while new exact supplied paths make progress.
        while True:
            resolved = run_mill(work, env, jvm, launcher,
                ["show", "{__.resolvedMvnDeps,__.resolvedRunMvnDeps}"], capture=True)
            aliases = project_inputs.stage_coursier(cache, views, work, lock_file, evaluated=evaluated)
            added = aliases - supplied
            supplied.update(aliases)
            if added:
                continue
            if resolved.returncode != 0:
                raise Failure("Mill could not resolve project dependencies after selecting Nix inputs:\n" + resolved.stderr)
            break
    return supplied


def artifact_key(name, artifact_id, version):
    """A published file name without the version: core_3-0.1.0-sources.jar
    becomes core_3-sources.jar."""
    prefix = f"{artifact_id}-{version}"
    if not name.startswith(prefix):
        raise Failure(f"published file {name} does not start with {prefix}")
    return artifact_id + name[len(prefix) :]


def offline_check(root, state, kept, cache, launcher_path, views=()):
    """Whether the build compiles offline from only the `kept` downloads."""
    check_cache = state / "check-cache"
    check_work = state / "check-run"
    for d in (check_cache, check_work, state / "check-archive-cache"):
        shutil.rmtree(d, ignore_errors=True)
    for rel in kept:
        target = check_cache / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(cache / rel, target)
    copy_build(root, check_work)
    env, jvm = mill_env(check_cache, state / "check-archive-cache", state / "lock-home", state / "lock-tmp", offline=True)
    if views:
        stage_project_inputs(check_cache, views, check_work, env, jvm, check_cache / launcher_path)
    ok = run_mill(check_work, env, jvm, check_cache / launcher_path, ["__.compile"], capture=True).returncode == 0
    log("an offline build without the version listings " + ("passed" if ok else "failed"))
    return ok


DEFAULT_TESTS = "__.testForked"


def content_hash(lock_data):
    """The hash of a lockfile's content: the SHA-256 of the lockfile without
    `contentHash`, as compact JSON with sorted keys, which is what Nix's
    builtins.toJSON writes, so lib-overlays/default/read-lock.nix computes the same value."""
    rest = {k: v for k, v in lock_data.items() if k != "contentHash"}
    text = json.dumps(rest, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def mill_version_key(version):
    """A sort key for a Mill version: its numbers, then 1 for a release and
    0 for a pre-release such as 1.1.0-RC1, which is older than 1.1.0."""
    numbers = tuple(int(n) for n in re.findall(r"\d+", version.split("-", 1)[0]))
    return numbers, 0 if "-" in version else 1


FIRST_MILL_1 = ((1, 0, 0), 0)
# Mill 1.1.0 is the first release with mill.javalib.MavenPublishModule. The
# same decision is in pkgs/build-mill-project/publisher-available.nix.
MIN_PUBLISHER_MILL_VERSION = ((1, 1, 0), 1)


def listing_of(path, name):
    """Whether the cache path of a version listing is the listing of the
    Maven module `name`, written `org:artifact`."""
    org, _, artifact = name.partition(":")
    return f"/{org.replace('.', '/')}/{artifact}/maven-metadata" in path


def seed_listings(cache, current, updated):
    """Places the version listings that the lockfile `current` recorded into
    the cache of a lock run, so the version ranges of the build resolve as
    they did. `updated` is None to place every listing, or the `org:artifact`
    names of the modules whose listings the run reads afresh, which is every
    module when it is empty. Each listing goes to the cache path the run
    reads, that of the resolution mirror when one is set, with the hidden
    `.checked` marker by which coursier counts a download as fresh."""
    if updated == []:
        return
    recorded = [e for e in current.get("artifacts", []) if "contents" in e and coursier_cache.is_version_listing(e["path"])]
    mirror = resolution_mirror()
    for name in updated or []:
        if not any(listing_of(e["path"], name) for e in recorded):
            log(
                f"{name} has no version listing in the lockfile: the build gives it no version range, so only an "
                f"edit of its version in the build moves it"
            )
    for entry in recorded:
        if updated and any(listing_of(entry["path"], name) for name in updated):
            continue
        path = entry["path"]
        central = coursier_cache.cache_path(MAVEN_CENTRAL + "/").removesuffix(".directory")
        if mirror and path.startswith(central):
            path = coursier_cache.cache_path(mirror + "/").removesuffix(".directory") + path[len(central) :]
        target = cache / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(entry["contents"], encoding="utf-8")
        # Coursier checks a file against a checksum, and fetches the file
        # again when the checksum differs from the content. The listing in
        # the lockfile can be older than the one the repository serves, so
        # the cache gets a SHA-1 file that matches the listing placed here.
        checksum = target.parent / f"{target.name}.sha1"
        checksum.write_text(hashlib.sha1(target.read_bytes()).hexdigest() + "\n")
        for name in (target.name, checksum.name):
            (target.parent / f".{name}.checked").write_bytes(b"")


def lock(root, lock_file, tests=None, updated=None, input_refs=None):
    """Writes the lockfile. `tests` is the Mill selector of the test tasks
    the lock run runs, or "none"; without it, the lock run uses the
    selector of the existing lockfile, or every test task. `updated` is
    None to keep the resolution of the existing lockfile, or the modules
    whose version ranges resolve afresh, as seed_listings takes it."""
    wanted_hash = definition_hash(root)
    try:
        current = read_lock(root, lock_file, quiet=True) if lock_file.is_file() else {}
    except Failure as e:
        if updated is None:
            log(f"{e.args[0]}; the lock run resolves the build afresh")
        current = {}
    if tests is None:
        tests = current.get("testSelector", DEFAULT_TESTS) if isinstance(current, dict) else DEFAULT_TESTS

    previous_inputs = current.get("projectInputs", [])
    if input_refs is None:
        input_refs = [entry["reference"] for entry in previous_inputs]
    inputs = []
    if input_refs:
        import project_inputs
        inputs = project_inputs.pin(input_refs, previous_inputs, refresh=updated == [])

    version = mill_config(root, "mill-version")
    if not version:
        raise Failure("the build does not pin a Mill version; set `//| mill-version: <version>` in build.mill or write .mill-version")
    if mill_version_key(version) < FIRST_MILL_1:
        raise Failure(f"the build pins Mill {version}; this purifier supports Mill 1.0.0 and later")
    has_publisher = mill_version_key(version) >= MIN_PUBLISHER_MILL_VERSION
    if not has_publisher:
        log(
            f"Mill {version} has no mill.javalib.MavenPublishModule, which is in Mill 1.1.0 and later: "
            f"the lock run skips the publish step, and `release` is not available for this build"
        )

    state = root / "out" / "mill-purifier"
    work = state / "lock-run"
    cache = state / "lock-cache"
    for d in (work, cache, state / "lock-archive-cache", state / "lock-home", state / "lock-tmp"):
        shutil.rmtree(d, ignore_errors=True)
    copy_build(root, work)
    for d in (cache, state / "lock-home", state / "lock-tmp"):
        d.mkdir(parents=True)

    url = launcher_url(version)
    launcher_path = coursier_cache.cache_path(url)
    launcher = cache / launcher_path
    log(f"downloading Mill {version} from {url}")
    download(url, launcher)
    seed_listings(cache, current, updated)

    env, jvm = mill_env(cache, state / "lock-archive-cache", state / "lock-home", state / "lock-tmp", offline=False)
    env = mirror_env(env, state)

    views, provided = [], set()
    if inputs:
        import project_inputs
        views = project_inputs.maven_views(inputs, state / "project-inputs")
        provided = stage_project_inputs(cache, views, work, env, jvm, launcher, resolve_metadata=True)
    publish_tasks = resolve(work, env, jvm, launcher, "__.publishArtifacts")
    test_tasks = [] if tests == "none" else resolve(work, env, jvm, launcher, tests)
    log("compiling every module with an empty coursier cache")
    if run_mill(work, env, jvm, launcher, ["__.compile"]).returncode != 0:
        raise Failure("the lock run failed in `__.compile`")
    projects = []
    if publish_tasks:
        shown = run_mill(work, env, jvm, launcher, ["show", "__.publishArtifacts"], capture=True)
        if shown.returncode != 0:
            raise Failure("the lock run failed in `show __.publishArtifacts`")
        published = json.loads(shown.stdout)
        if len(publish_tasks) == 1:
            published = {publish_tasks[0]: published}
        for task, value in sorted(published.items()):
            meta = value["meta"]
            keys = sorted(artifact_key(name, meta["id"], meta["version"]) for _, name in value["payload"])
            projects.append(
                {
                    "id": task[: -len(".publishArtifacts")],
                    "name": meta["id"],
                    "artifacts": [{"key": k} for k in keys],
                }
            )
    log("starting Mill's BSP server, so what editors need is locked")
    lock_editor_support(work, env, jvm, launcher)
    if test_tasks:
        log("running the tests, so their runtime dependencies are locked")
        if run_mill(work, env, jvm, launcher, plus(test_tasks)).returncode != 0:
            raise Failure("the lock run failed running the tests")
    if publish_tasks and has_publisher:
        # buildMillProject's `repository` publishes with Mill's Maven
        # publisher, offline; publishing once here locks what it needs.
        log("publishing to a local repository with mill.javalib.MavenPublishModule, so its dependencies are locked")
        local_repo = (state / "lock-repository").as_uri()
        shutil.rmtree(state / "lock-repository", ignore_errors=True)
        publish_env = {**env, "MILL_MAVEN_USERNAME": "local", "MILL_MAVEN_PASSWORD": "local"}
        published_run = run_mill(
            work,
            publish_env,
            jvm,
            launcher,
            [
                "mill.javalib.MavenPublishModule/publishAll",
                "--publishArtifacts",
                "__.publishArtifacts",
                "--releaseUri",
                local_repo,
                "--snapshotUri",
                local_repo,
            ],
        )
        if published_run.returncode != 0:
            raise Failure("the lock run failed publishing with mill.javalib.MavenPublishModule")

    canonical_cache(cache)
    provided = {coursier_cache.canonical(path, MIRRORS)[0] for path in provided}
    files = [path for path in coursier_cache.downloaded_files(cache) if path not in provided]
    listings = [p for p in files if coursier_cache.is_version_listing(p)]
    kept = [p for p in files if not coursier_cache.is_version_listing(p)]
    inline = []
    if listings and not offline_check(root, state, kept, cache, launcher_path, views=views):
        # A listing changes upstream whenever a version is published, so no
        # hash of it holds; the lock keeps it as the lock run read it, and
        # the offline build resolves to the versions the lock run picked.
        log("the offline build needs the maven-metadata.xml version listings; locking them as the lock run read them")
        inline = coursier_cache.inline_entries(cache, listings)

    archive_cache = state / "lock-archive-cache"
    archives = [p for p in kept if (archive_cache / p).is_dir()]
    platforms = {}
    if archives:
        platforms, specific = lock_platforms(kept, archives, cache, state)
        kept = [p for p in kept if p not in specific]

    result = {
        "version": LOCK_VERSION,
        "definitionHash": wanted_hash,
        "projects": projects,
        "testSelector": tests,
        "testTasks": sorted(test_tasks),
        "platforms": platforms,
        "artifacts": lock_entries(cache, kept) + inline,
    }
    if inputs:
        result["projectInputs"] = inputs
    result["contentHash"] = content_hash(result)
    lock_file.write_text(json.dumps(result, indent=2) + "\n")
    log(f"wrote {lock_file}")


# --- prefetch, mill and publish -------------------------------------------


def nix_build(root, lock_file, state, attribute):
    """Builds an attribute of buildMillProject for the build at `root` (see
    pkgs/cli/nix/cli.nix) and returns its store path. The result link under
    `state` is the GC root that keeps it."""
    lib = os.environ.get("MILL_PURIFIER_LIB")
    nixpkgs = os.environ.get("MILL_PURIFIER_NIXPKGS")
    purifier_source = os.environ.get("MILL_PURIFIER_PURIFIER_SOURCE")
    if not lib or not nixpkgs or not purifier_source:
        raise Failure(
            "MILL_PURIFIER_LIB, MILL_PURIFIER_NIXPKGS or MILL_PURIFIER_PURIFIER_SOURCE is not set; "
            "run mill-purifier through its Nix package"
        )
    state.mkdir(parents=True, exist_ok=True)
    built = subprocess.run(
        [
            "nix-build",
            str(Path(lib) / "cli.nix"),
            "--arg", "nixpkgs", nixpkgs,
            "--arg", "purifierSource", purifier_source,
            "--argstr", "src", str(root.resolve()),
            "--argstr", "lockFile", str(lock_file.resolve()),
            "-A", attribute,
            "--out-link", str(state / f"{attribute}-root"),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    if built.returncode != 0:
        raise Failure(
            f"nix-build could not build {attribute} from {lock_file}. A `hash mismatch` above means a download "
            f"no longer matches its hash in the lockfile: run `mill-purifier lock`."
        )
    return Path(built.stdout.strip().splitlines()[-1])


def link_tree(source, dest):
    """A writable copy of a tree of links into the store: the same
    directories, with each link (to a file, or to an unpacked archive)
    recreated. Coursier writes lock and marker files next to what it reads,
    so it needs writable directories."""
    shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True)
    for dirpath, dirnames, filenames in os.walk(source):
        rel = Path(dirpath).relative_to(source)
        for name in dirnames + filenames:
            entry = Path(dirpath, name)
            if entry.is_symlink():
                (dest / rel / name).symlink_to(os.readlink(entry))
            elif entry.is_dir():
                (dest / rel / name).mkdir()


def user_cache(root):
    """The directory for the coursier caches of the build at `root`, outside
    the build: Mill refuses a task that reads files in the workspace which
    the task does not declare as sources, and the caches are read by every
    resolution."""
    base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    key = hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:16]
    return base / "mill-purifier" / key


PREFETCH_STATE = "prefetch.json"
CACHE_ATTRIBUTES = (("dependencies", "coursier-cache"), ("archiveCache", "coursier-archive-cache"))


def prefetch(root, lock_file):
    """Builds, with Nix, everything the lockfile lists for this system: the
    downloads of the build and of its tests, the JVMs, and what an editor's
    BSP server needs. The GC roots are links in .mill-purifier beside the
    lockfile, and .mill-purifier/prefetch.json records the store paths and
    the lockfile they were built from, so the mill verb needs neither Nix
    nor the network while the lockfile stays the same."""
    current = read_lock(root, lock_file)
    state = lock_file.parent / PREFETCH_DIR
    built = {attribute: str(nix_build(root, lock_file, state, attribute)) for attribute, _ in CACHE_ATTRIBUTES}
    if current.get("projectInputs"):
        import project_inputs
        built["projectInputs"] = [{key: str(value) for key, value in view.items()}
                                  for view in project_inputs.maven_views(current["projectInputs"], state / "project-inputs")]
    (state / PREFETCH_STATE).write_text(json.dumps({"contentHash": current["contentHash"], **built}, indent=2) + "\n")
    log(f"prefetched {lock_file} for {nix_system()}; Mill now runs without the network")
    return built


def prefetched(lock_file, current):
    """The store paths of the last prefetch, when it was made from the
    lockfile `current` and they still exist; otherwise None."""
    path = lock_file.parent / PREFETCH_DIR / PREFETCH_STATE
    try:
        state = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if state.get("contentHash") != current["contentHash"]:
        return None
    if not all(Path(state.get(a, "")).is_dir() for a, _ in CACHE_ATTRIBUTES):
        return None
    return state


def caches(root, lock_file, current):
    """Writable coursier caches of links into the store, from the last
    prefetch of this lockfile, or from a new prefetch."""
    state = prefetched(lock_file, current) or prefetch(root, lock_file)
    cache_dir = user_cache(root)
    result = []
    for attribute, name in CACHE_ATTRIBUTES:
        built = state[attribute]
        stamp = cache_dir / f"{name}.source"
        if not ((cache_dir / name).is_dir() and stamp.is_file() and stamp.read_text() == built):
            link_tree(Path(built), cache_dir / name)
            stamp.write_text(built)
        result.append(cache_dir / name)
    if state.get("projectInputs"):
        temporary = root / "out" / "mill-purifier" / "tmp"
        temporary.mkdir(parents=True, exist_ok=True)
        env, jvm = mill_env(result[0], result[1], None, temporary, offline=True)
        stage_project_inputs(result[0], state["projectInputs"], root, env, jvm,
                             result[0] / launcher_cache_path(root), lock_file)
    return result


def read_lock(root, lock_file, quiet=False):
    if not lock_file.is_file():
        raise Failure(f"{lock_file} does not exist; run `mill-purifier lock` first")
    try:
        current = json.loads(lock_file.read_text())
    except ValueError as e:
        raise Failure(f"{lock_file} is not valid JSON ({e}); run `mill-purifier lock`") from None
    if not isinstance(current, dict) or "definitionHash" not in current or "artifacts" not in current:
        raise Failure(f"{lock_file} is not a mill-purifier lockfile; run `mill-purifier lock`")
    if current.get("version") != LOCK_VERSION:
        raise Failure(f"{lock_file} has lock format version {current.get('version')}; run `mill-purifier lock`")
    if current.get("contentHash") != content_hash(current):
        raise Failure(
            f"{lock_file} does not match its contentHash: it was changed after `mill-purifier lock` wrote it. "
            f"Run `mill-purifier lock`."
        )
    if not quiet and current.get("definitionHash") != definition_hash(root):
        log(
            f"the build definition changed since {lock_file} was written. Mill runs from the old lock, so a "
            f"dependency added since then fails to resolve offline: run `mill-purifier lock`."
        )
    return current


def mill_executable(root):
    """A `mill` command that runs `mill-purifier mill` in the build at
    `root`. Interactive mode passes it to Mill as MILL_EXECUTABLE_PATH, the
    command Mill's BSP install (mill.bsp.BSP/install) writes into
    .bsp/mill-bsp.json, so an editor's Mill also runs offline from the
    lock."""
    own = os.environ.get("MILL_PURIFIER_BIN")
    if not own:
        raise Failure("MILL_PURIFIER_BIN is not set; run mill-purifier through its Nix package")
    script = user_cache(root) / "mill"
    script.parent.mkdir(parents=True, exist_ok=True)
    text = f"#!/bin/sh\ncd {shlex.quote(str(root.resolve()))} && exec {shlex.quote(own)} mill \"$@\"\n"
    if not script.is_file() or script.read_text() != text:
        script.write_text(text)
        script.chmod(0o755)
    return script


# What Mill prints when coursier cannot resolve a dependency, which offline
# means that the lockfile lacks it.
RESOLUTION_FAILURES = (b"Resolution failed", b"CantDownloadModule", b"Error downloading")
MISSING_HINT = "Mill could not resolve a dependency offline, so the lockfile lacks it: run `mill-purifier lock`."


def run_watching(cmd, env):
    """Runs Mill and returns its exit code. Mill keeps its terminal: at a
    terminal it runs in a pseudo-terminal, otherwise its error output passes
    through line by line. When Mill reports a resolution failure, the command
    names `mill-purifier lock` after it."""
    seen = []

    def watch(data):
        if any(marker in data for marker in RESOLUTION_FAILURES):
            seen.append(True)
        return data

    if sys.stdout.isatty() and sys.stdin.isatty():
        import pty

        os.environ.clear()
        os.environ.update(env)
        status = pty.spawn(cmd, lambda fd: watch(os.read(fd, 1 << 16)))
        code = os.waitstatus_to_exitcode(status)
    else:
        proc = subprocess.Popen(cmd, env=env, stderr=subprocess.PIPE)
        for line in proc.stderr:
            sys.stderr.buffer.write(watch(line))
            sys.stderr.buffer.flush()
        code = proc.wait()
    if seen:
        log(MISSING_HINT)
    return code


def interactive(root, lock_file, args):
    current = read_lock(root, lock_file)
    state = root / "out" / "mill-purifier"
    cache, archive_cache = caches(root, lock_file, current)
    (state / "tmp").mkdir(parents=True, exist_ok=True)
    env, jvm = mill_env(cache, archive_cache, None, state / "tmp", offline=True)
    env["MILL_EXECUTABLE_PATH"] = str(mill_executable(root))
    cmd = mill_command(cache / launcher_cache_path(root), jvm, args)
    if "--bsp" in args:
        # An editor speaks JSON-RPC over standard input and output.
        os.execve(cmd[0], cmd, env)
    return run_watching(cmd, env)


def publish(root, lock_file, args):
    """Runs the Mill publisher command `args` on the files Nix built: on the
    publish build of buildMillProject, whose `publishArtifacts` tasks return
    them. Resolution runs online, since a publisher can need dependencies
    the lock run did not use; it starts from the locked downloads."""
    if not args:
        raise Failure("name a Mill publisher, such as `mill-purifier publish mill.javalib.SonatypeCentralPublishModule/`")
    current = read_lock(root, lock_file)
    state = root / "out" / "mill-purifier"
    cache, archive_cache = caches(root, lock_file, current)
    publish_build = nix_build(root, lock_file, state, "publishBuild")
    work = state / "publish-run"
    shutil.rmtree(work, ignore_errors=True)
    shutil.copytree(publish_build, work)
    for d in (work, *work.rglob("*")):
        d.chmod(d.stat().st_mode | 0o200)
    (state / "tmp").mkdir(parents=True, exist_ok=True)
    env, jvm = mill_env(cache, archive_cache, None, state / "tmp", offline=False)
    if "--publishArtifacts" not in args:
        args = [*args, "--publishArtifacts", "__.publishArtifacts"]
    cmd = mill_command(cache / launcher_cache_path(root), jvm, ["--no-daemon", *args])
    return subprocess.run(cmd, cwd=work, env=env).returncode


def main(argv):
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__.strip())
        return 0 if argv else 2
    verb, rest = argv[0], argv[1:]
    root = Path.cwd()
    named = os.environ.get("MILL_PURIFIER_LOCKFILE")
    lock_file = Path(named).resolve() if named else root / LOCK_FILE
    # Every verb takes --lockfile <path>, for a lockfile kept outside the
    # build, before its own arguments.
    if rest[:1] == ["--lockfile"]:
        if len(rest) < 2:
            print(__doc__.strip(), file=sys.stderr)
            return 2
        lock_file = Path(rest[1]).resolve()
        rest = rest[2:]
    try:
        if verb == "mill" and not lock_file.exists():
            log('warning: no mill-purifier.lock.json found; running mill impurely. Run `mill-purifier lock` to use Nix-resolved dependencies.')
            command = os.environ.get("MILL_PURIFIER_MILL") or shutil.which("mill")
            if command is None:
                raise Failure("no ordinary mill found; install Mill or set MILL_PURIFIER_MILL")
            os.execvp(command, [command, *rest])
        build_file(root)
        input_refs = None
        if verb in ("lock", "update"):
            remaining = []
            arguments = iter(rest)
            for argument in arguments:
                if argument == "--input":
                    reference = next(arguments, None)
                    if reference is None:
                        raise Failure("--input needs a flake package")
                    if input_refs is None:
                        input_refs = []
                    input_refs.append(reference)
                else:
                    remaining.append(argument)
            rest = remaining
        if verb == "lock":
            tests = None
            if rest[:1] == ["--tests"] and len(rest) == 2:
                tests = rest[1]
            elif rest:
                print(__doc__.strip(), file=sys.stderr)
                return 2
            lock(root, lock_file, tests=tests, **({"input_refs": input_refs} if input_refs is not None else {}))
        elif verb == "update" and not any(":" not in name for name in rest):
            lock(root, lock_file, updated=rest, **({"input_refs": input_refs} if input_refs is not None else {}))
        elif verb == "prefetch" and not rest:
            prefetch(root, lock_file)
            caches(root, lock_file, read_lock(root, lock_file))
        elif verb == "mill":
            return interactive(root, lock_file, rest)
        elif verb == "publish":
            return publish(root, lock_file, rest)
        else:
            print(__doc__.strip(), file=sys.stderr)
            return 2
    except Failure as e:
        log(str(e))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
