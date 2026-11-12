# The Mill version of the build at `src`, read as Mill's launcher reads it
# (mill_config in pkgs/cli/mill_purifier.py): from .mill-version, then from
# .config/mill-version, then from the `mill-version` key of the header of
# build.mill (its leading `//|` lines) or of build.mill.yaml. It names the
# launcher the lock run downloaded.
src:
let
  inherit (builtins)
    filter
    head
    match
    pathExists
    readFile
    split
    ;
  lines = text: filter builtins.isString (split "\n" text);
  firstValue =
    regex: ls:
    let
      found = filter (m: m != null) (map (match regex) ls);
    in
    if found == [ ] then null else head (head found);
  # The leading lines of build.mill that hold its header: `//|` lines, after
  # an optional `#!` line, up to the first other line that is not blank.
  header =
    ls:
    if ls == [ ] then
      [ ]
    else
      let
        line = head ls;
        rest = builtins.tail ls;
      in
      if match "//\\|.*" line != null then
        [ line ] ++ header rest
      else if match "[[:space:]]*" line != null || match "#!.*" line != null then
        header rest
      else
        [ ];
  fromFile =
    file:
    if pathExists file then
      firstValue "[[:space:]]*([^#[:space:]][^[:space:]]*)[[:space:]]*" (lines (readFile file))
    else
      null;
  unquote = v: if v == null then null else head (match "[\"']?([^\"']*)[\"']?" v);
  fromHeader =
    if pathExists (src + "/build.mill.yaml") then
      unquote (
        firstValue "mill-version:[[:space:]]*(.*[^[:space:]])[[:space:]]*" (
          lines (readFile (src + "/build.mill.yaml"))
        )
      )
    else if pathExists (src + "/build.mill") then
      unquote (
        firstValue "//\\|[[:space:]]*mill-version:[[:space:]]*(.*[^[:space:]])[[:space:]]*" (
          header (lines (readFile (src + "/build.mill")))
        )
      )
    else
      null;
  version =
    let
      dotFile = fromFile (src + "/.mill-version");
      configFile = fromFile (src + "/.config/mill-version");
    in
    if dotFile != null then
      dotFile
    else if configFile != null then
      configFile
    else
      fromHeader;
in
if version == null then
  throw "mill-purifier: the Mill build at ${toString src} pins no Mill version; set `//| mill-version: <version>` in build.mill or write .mill-version, then run `mill-purifier lock`."
else
  version
