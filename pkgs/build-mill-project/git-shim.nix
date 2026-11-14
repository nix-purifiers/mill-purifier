# A `git` command that answers the queries of Mill's VcsVersion
# (mill.util.VcsVersion.calcVcsState) from a description of the
# repository, for a sandboxed build, which has no .git directory:
#
#   git rev-parse HEAD
#   git describe --exact-match --tags --always <rev>
#   git describe --abbrev=0 --tags
#   git rev-list <rev> [--not <tag>] --count
#   git diff HEAD   (Mill 1.0 asks for git diff, with no argument)
#
# `git` is the attribute set buildMillProject takes: `rev` (the commit),
# `revCount` (the number of commits it has, which a flake's `self` holds
# as `revCount`), and, when the commit is at or after a tag, `tag` and
# `commitsSinceTag`. The workspace is clean. Any other query fails, as it
# would in a directory that is not a repository.
{
  lib,
  writeShellScriptBin,
}:
git:
let
  rev = git.rev or (throw "mill-purifier: `git` needs `rev`, the commit the build is of");
  tag = git.tag or null;
  commitsSinceTag = git.commitsSinceTag or 0;
  revCount = git.revCount or null;
  q = lib.escapeShellArg;
  fail = ''
    echo "fatal: mill-purifier's git answers only the queries of Mill's VcsVersion (git $*)" >&2
    exit 128
  '';
in
writeShellScriptBin "git" ''
  case "$*" in
    "rev-parse HEAD")
      echo ${q rev} ;;
    "describe --exact-match --tags --always "${q rev})
      ${
        if tag != null && commitsSinceTag == 0 then
          "echo ${q tag}"
        else
          # With --exact-match, git fails when no tag names the commit,
          # --always or not.
          ''
            echo "fatal: no tag exactly matches '"${q rev}"'" >&2
            exit 128''
      } ;;
    "describe --abbrev=0 --tags")
      ${
        if tag != null then
          "echo ${q tag}"
        else
          ''
            echo "fatal: No names found, cannot describe anything." >&2
            exit 128''
      } ;;
    ${lib.optionalString (tag != null) ''
      "rev-list "${q rev}" --not "${q tag}" --count")
        echo ${toString commitsSinceTag} ;;
    ''}
    ${lib.optionalString (revCount != null) ''
      "rev-list "${q rev}" --count")
        echo ${toString revCount} ;;
    ''}
    "diff HEAD" | "diff")
      ;;
    *)
      ${fail} ;;
  esac
''
