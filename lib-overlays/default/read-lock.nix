# readLock: the lockfile of a Mill build, in the shape every purifier reads
# its lockfiles into (CONTRACT.md of the purifier library, "The lock in
# memory"):
#
#   {
#     purifier = "mill-purifier";
#     entries  = [ <download entry of the purifier library> ];
#     outputs  = { <module> = { name; artifacts = [ <key> ]; }; };
#     problems = [ <why the lockfile cannot be used, naming the fix> ];
#     mill     = { testTasks; testSelector; archives.<system>; projectsById; };
#   }
#
# Mill has no lockfile of its own, so everything comes from
# mill-purifier.lock.json. A download is recorded there as { path, hash }:
# its place in the coursier cache, and the hash of the file. Its URL
# follows from the path (coursier stores `https://host/a/b.jar` at
# `https/host/a/b.jar`), and a file from Maven Central is also served by
# Google's mirror of it, so this expands each one into a file entry,
# { path, url, mirrors, hash }. An inline entry { path, contents } holds a
# version listing as the lock run read it. The coursier JVM index and the
# JVM archives of a platform carry `systems`.
#
# A lockfile that is not valid JSON fails evaluation in builtins.fromJSON,
# which no expression can catch; every other problem is in `problems`.
{
  # The root of the Mill build, whose build definition the lock must match.
  src,
  lockFile ? src + "/mill-purifier.lock.json",
  # Leaves a stale lock (a build definition that changed since the lock)
  # out of `problems`.
  allowStaleLock ? false,
}:
let
  inherit (builtins)
    attrNames
    concatLists
    elemAt
    filter
    hashString
    head
    isAttrs
    isList
    listToAttrs
    match
    split
    stringLength
    substring
    toJSON
    ;
  name = toString lockFile;

  raw = builtins.fromJSON (builtins.readFile lockFile);
  data = if isAttrs raw then raw else { };
  supportedVersion = import ./lock-version.nix;

  required = [
    "version"
    "contentHash"
    "definitionHash"
    "projects"
    "artifacts"
  ];
  missing = filter (field: !(data ? ${field})) required;

  formatProblems =
    if !isAttrs raw then
      [
        "mill-purifier: ${name} is not a mill-purifier lockfile (it holds no JSON object); run `mill-purifier lock` in the build root."
      ]
    else if missing != [ ] then
      [
        "mill-purifier: ${name} is not a mill-purifier lockfile; it has no ${builtins.concatStringsSep ", " missing}. Run `mill-purifier lock` in the build root."
      ]
    else if data.version != supportedVersion then
      [
        "mill-purifier: ${name} has lock format version ${toString data.version}; this library reads version ${toString supportedVersion}. Run `mill-purifier lock` with the mill-purifier version that provides this library."
      ]
    # The content hash is the SHA-256 of the lockfile without `contentHash`,
    # as compact JSON with sorted keys (content_hash in
    # pkgs/cli/mill_purifier.py), which is what builtins.toJSON writes.
    else if
      data.contentHash != "sha256:" + hashString "sha256" (toJSON (removeAttrs data [ "contentHash" ]))
    then
      [
        "mill-purifier: ${name} does not match its contentHash: it was changed after `mill-purifier lock` wrote it, as by a hand edit. Run `mill-purifier lock` in the build root and commit the lockfile."
      ]
    else
      [ ];

  definition = import ./definition-hash.nix src;
  staleProblems =
    if formatProblems != [ ] || allowStaleLock || definition.hash == data.definitionHash then
      [ ]
    else
      [
        "mill-purifier: the build definition (${builtins.concatStringsSep ", " definition.files}) changed since ${name} was written, so the lock may miss what the build now downloads. Run `mill-purifier lock` in the build root and commit the lockfile, or build with `allowStaleLock = true`."
      ];

  usable = formatProblems == [ ];

  mavenCentral = "https://repo1.maven.org/maven2";
  mavenCentralMirror = "https://maven-central.storage-download.googleapis.com/maven2";
  hasPrefix = prefix: s: substring 0 (stringLength prefix) s == prefix;
  dropPrefix = prefix: s: substring (stringLength prefix) (-1) s;

  # Coursier escapes a character as % and two hex digits of its character
  # code (url_of in pkgs/cli/coursier_cache.py), and a JSON string decodes
  # \u00XX to that character.
  unescape =
    s:
    builtins.concatStringsSep "" (
      map (part: if isList part then builtins.fromJSON ''"\u00${head part}"'' else part) (
        split "%([0-9A-Fa-f]{2})" s
      )
    );

  # The URL a cache path was downloaded from.
  urlOf =
    path:
    let
      m = match "([^/]+)/(([^/]*).*)" (unescape path);
      rest = elemAt m 1;
      rest' =
        if match ".*/\\.directory" rest != null then substring 0 (stringLength rest - 10) rest else rest;
    in
    assert
      m != null
      || throw "mill-purifier: ${name} holds ${path}, which is not a coursier cache path; run `mill-purifier lock`.";
    assert
      match ".*@.*" (elemAt m 2) == null
      || throw "mill-purifier: ${name} holds ${path}, which was downloaded with credentials; run `mill-purifier lock`.";
    "${elemAt m 0}://${rest'}";

  expand =
    extra: entry:
    if entry ? contents then
      entry // extra
    else
      let
        url = urlOf entry.path;
      in
      {
        inherit (entry) path hash;
        inherit url;
        mirrors =
          if hasPrefix "${mavenCentral}/" url then
            [ "${mavenCentralMirror}${dropPrefix mavenCentral url}" ]
          else
            [ ];
      }
      // extra;

  # The coursier JVM index, the one download of a platform that is not a JVM
  # archive.
  jvmIndexDir = "https/repo1.maven.org/maven2/io/get-coursier/jvm/indices/";
  platforms = if usable then data.platforms or { } else { };
  projects = if usable then data.projects else [ ];
in
{
  purifier = "mill-purifier";
  problems = formatProblems ++ staleProblems;

  entries =
    if !usable then
      [ ]
    else
      map (expand { }) data.artifacts
      ++ concatLists (
        map (system: map (expand { systems = [ system ]; }) platforms.${system}.artifacts) (
          attrNames platforms
        )
      );

  outputs = listToAttrs (
    map (p: {
      name = p.id;
      value = {
        inherit (p) name;
        artifacts = map (a: a.key) p.artifacts;
      };
    }) projects
  );

  mill = {
    testSelector = data.testSelector or null;
    testTasks = data.testTasks or [ ];
    # The JVM archives coursier unpacks on each system: the entries of its
    # platform but the JVM index. A lock whose build downloads no JVM has
    # none for any system.
    archives = builtins.mapAttrs (
      _: part: map (a: a.path) (filter (a: !(hasPrefix jvmIndexDir a.path)) part.artifacts)
    ) platforms;
    projectsById = listToAttrs (
      map (p: {
        name = p.id;
        value = p;
      }) projects
    );
  };
}
