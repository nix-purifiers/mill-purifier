# The hash of the definition of the Mill build at `root`, as
# `mill-purifier lock` computes it (definition_hash in
# pkgs/cli/mill_purifier.py): the SHA-256 of the compact JSON list of
# [path, SHA-256 of the file] for each definition file, sorted by path. The
# definition files are every *.mill and *.mill.yaml file, everything under
# mill-build/, and the .mill-* files and .config/mill-* files at the root.
# Directories named like Mill's and editors' output (out, .git, .bsp,
# .idea, .metals, .bloop) are skipped at every level.
root:
let
  skipped = [
    "out"
    ".git"
    ".bsp"
    ".idea"
    ".metals"
    ".bloop"
  ];
  hasSuffix =
    suffix: s:
    let
      ls = builtins.stringLength s;
      lx = builtins.stringLength suffix;
    in
    ls >= lx && builtins.substring (ls - lx) lx s == suffix;
  hasPrefix = prefix: s: builtins.substring 0 (builtins.stringLength prefix) s == prefix;

  # The definition files under `dir`, whose path relative to the root is
  # `rel` ("" for the root).
  walk =
    rel: dir:
    let
      entries = builtins.readDir dir;
      inMetaBuild = rel == "mill-build" || hasPrefix "mill-build/" rel;
      path = name: if rel == "" then name else "${rel}/${name}";
      wanted =
        name:
        inMetaBuild
        || hasSuffix ".mill" name
        || hasSuffix ".mill.yaml" name
        || (rel == "" && hasPrefix ".mill-" name)
        || (rel == ".config" && hasPrefix "mill-" name);
    in
    builtins.concatLists (
      map (
        name:
        let
          type = entries.${name};
        in
        if type == "directory" then
          (if builtins.elem name skipped then [ ] else walk (path name) (dir + "/${name}"))
        else if wanted name then
          [ (path name) ]
        else
          [ ]
      ) (builtins.attrNames entries)
    );

  files = builtins.sort builtins.lessThan (walk "" root);
  listing = builtins.toJSON (
    map (rel: [
      rel
      (builtins.hashFile "sha256" (root + "/${rel}"))
    ]) files
  );
in
{
  inherit files;
  hash = "sha256:" + builtins.hashString "sha256" listing;
}
