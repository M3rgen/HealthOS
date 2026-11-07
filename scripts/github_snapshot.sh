#!/usr/bin/env bash
# Build the GitHub `main` snapshot: one commit whose tree is a source revision's tree merged
# with every GitHub-only extension branch, minus the paths listed in the local git config, on
# top of the snapshot branch.
#
#   bash scripts/github_snapshot.sh [options]
#
#   --source <rev>          tree to publish (default: main)
#   --branch <name>         snapshot branch, fast-forwarded to the new commit
#                           (default: healthos.githubBranch, else github-main)
#   --no-extensions         publish the source tree alone, without the GitHub-only extensions
#   --override <path>=<rev> take <path> from <rev> instead of the merged tree (repeatable);
#                           a stopgap until the change reaches the source branch
#   -m, --message <text>    commit message
#                           (default: "HealthOS: snapshot of <source> [+ <extension> ...] (<date>)")
#   --allow-references      update the branch even if the tree still names an excluded file
#   --dry-run               build the tree, run the checks and print the diff, but create no commit
#
# The working tree and the real index are never touched: extensions are merged with
# `git merge-tree --write-tree` (git 2.38 or later) and the tree is assembled in a temporary
# index. Excluded paths, GitHub-only extensions and the commit identity live only in the local
# clone:
#   git config --add healthos.githubExclude <path>       # once per path, files or directories
#   git config --add healthos.githubExtension <branch>   # once per GitHub-only extension branch
#   git config healthos.githubAuthorName  <name>
#   git config healthos.githubAuthorEmail <address>
#
# Each GitHub-only extension branch is merged in turn (source + ext1, then + ext2, ...); a
# conflict stops the script: merge the source into the extension branch and resolve it there.
# A directory exclusion (written with or without a trailing slash) excludes everything under
# it. The reference check looks for a file's name and, as a whole word, its name without the
# extension (so `import helper` of an excluded `helper.py` is caught); for a directory it looks
# for its path. The final tree must have exactly one Alembic head in migrations/versions/.
#
# If the snapshot branch is checked out in a worktree, it is updated there with
# `git merge --ff-only`; otherwise its ref is moved after a fast-forward check. Nothing is pushed.
#
# Exit 0: branch updated, or nothing to do. Exit 1: an excluded file is still referenced.
# Exit 2: missing configuration, bad argument, missing extension branch, merge conflict, more
# than one Alembic head, or a non-fast-forward update.
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

source_rev=main
branch="$(git config --get healthos.githubBranch || echo github-main)"
message=""
allow_references=0
dry_run=0
use_extensions=1
overrides=()

die() {
  echo "github_snapshot: $*" >&2
  exit 2
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --source) source_rev="${2:?--source needs a revision}"; shift 2 ;;
    --branch) branch="${2:?--branch needs a name}"; shift 2 ;;
    --no-extensions) use_extensions=0; shift ;;
    --override) overrides+=("${2:?--override needs <path>=<rev>}"); shift 2 ;;
    -m | --message) message="${2:?--message needs text}"; shift 2 ;;
    --allow-references) allow_references=1; shift ;;
    --dry-run) dry_run=1; shift ;;
    -h | --help) awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$0"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

excludes=()
while IFS= read -r path; do [ -n "$path" ] && excludes+=("$path"); done \
  < <(git config --get-all healthos.githubExclude || true)
[ "${#excludes[@]}" -gt 0 ] || die "no healthos.githubExclude configured; refusing to publish the full tree"

extensions=()
while IFS= read -r name; do [ -n "$name" ] && extensions+=("$name"); done \
  < <(git config --get-all healthos.githubExtension || true)

author_name="$(git config --get healthos.githubAuthorName || true)"
author_email="$(git config --get healthos.githubAuthorEmail || true)"
[ -n "$author_name" ] && [ -n "$author_email" ] ||
  die "set healthos.githubAuthorName and healthos.githubAuthorEmail"
export GIT_AUTHOR_NAME="$author_name" GIT_AUTHOR_EMAIL="$author_email"
export GIT_COMMITTER_NAME="$author_name" GIT_COMMITTER_EMAIL="$author_email"

source_commit="$(git rev-parse --verify --quiet "$source_rev^{commit}")" || die "unknown revision: $source_rev"
parent="$(git rev-parse --verify --quiet "refs/heads/$branch^{commit}")" || die "unknown branch: $branch"

# Merge the GitHub-only extensions into the source, one after another. Each intermediate
# result becomes a throwaway commit object (no ref points at it) so that the next merge-tree
# call finds the right merge base; only the final tree is used.
merged="$source_commit"
merged_tree="$(git rev-parse "$source_commit^{tree}")"
merged_names=()
if [ "$use_extensions" -eq 0 ]; then
  for name in "${extensions[@]+"${extensions[@]}"}"; do echo "not merged (--no-extensions): $name"; done
else
  for name in "${extensions[@]+"${extensions[@]}"}"; do
    ext="$(git rev-parse --verify --quiet "refs/heads/$name^{commit}")" ||
      die "GitHub-only extension branch not found: $name (see healthos.githubExtension)"
    status=0
    output="$(git merge-tree --write-tree --name-only "$merged" "$ext" 2>&1)" || status=$?
    if [ "$status" -eq 1 ]; then
      echo "$output" | sed -n '2,/^$/p' | sed '/^$/d; s/^/conflict: /' >&2
      die "$name conflicts with $source_rev; merge $source_rev into $name first and resolve there"
    elif [ "$status" -ne 0 ]; then
      echo "$output" >&2
      die "cannot merge $name into $source_rev"
    fi
    merged_tree="$(echo "$output" | head -n 1)"
    merged="$(git commit-tree "$merged_tree" -p "$merged" -p "$ext" -m "snapshot merge $name")"
    merged_names+=("$name")
    echo "merged: $name @ $(git rev-parse --short "$ext")"
  done
fi
label="$source_rev"
for name in "${merged_names[@]+"${merged_names[@]}"}"; do label="$label + $name"; done

index="$(mktemp "${TMPDIR:-/tmp}/github-snapshot-index.XXXXXX")"
trap 'rm -f "$index"' EXIT
export GIT_INDEX_FILE="$index"

git read-tree "$merged_tree"
for path in "${excludes[@]}"; do
  if [ -z "$(git ls-files -- "$path")" ]; then
    echo "note: excluded path not in $label: $path" >&2
    continue
  fi
  git rm --cached -r -f -q -- "$path"
  echo "excluded: $path"
done
for item in "${overrides[@]+"${overrides[@]}"}"; do
  path="${item%=*}"
  rev="${item##*=}"
  [ -n "$path" ] && [ -n "$rev" ] && [ "$path" != "$item" ] || die "bad --override: $item"
  entry="$(git ls-tree "$rev" -- "$path")"
  [ -n "$entry" ] || die "--override: $path not found in $rev"
  mode="${entry%% *}"
  blob="$(git rev-parse "$rev:$path")"
  git update-index --add --cacheinfo "$mode,$blob,$path"
  echo "override: $path from $rev"
done
tree="$(git write-tree)"
unset GIT_INDEX_FILE

# Alembic must see exactly one head. Read migrations/versions/*.py from the tree's objects
# (never from the working tree) and find revisions no other migration names as down_revision.
command -v python3 >/dev/null || die "python3 is needed for the Alembic heads check"
python3 - "$tree" <<'PY' || die "Alembic heads check failed for the snapshot tree (see above)"
import ast
import subprocess
import sys

tree = sys.argv[1]
listing = subprocess.run(
    ["git", "ls-tree", "-z", f"{tree}:migrations/versions"],
    capture_output=True, check=False,
)
if listing.returncode != 0:
    sys.exit(0)  # no migrations directory in this tree
blobs = []
for entry in listing.stdout.split(b"\0"):
    if not entry:
        continue
    meta, name = entry.split(b"\t", 1)
    kind, oid = meta.split()[1:3]
    if kind == b"blob" and name.endswith(b".py"):
        blobs.append((name.decode(), oid.decode()))

revisions = {}
parents = set()
for name, oid in blobs:
    source = subprocess.run(["git", "cat-file", "blob", oid], capture_output=True, check=True).stdout
    try:
        module = ast.parse(source, filename=name)
    except SyntaxError as exc:
        print(f"cannot parse migrations/versions/{name}: {exc}", file=sys.stderr)
        sys.exit(1)
    values = {}
    for node in module.body:
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        for target in targets:
            if isinstance(target, ast.Name) and target.id in ("revision", "down_revision"):
                try:
                    values[target.id] = ast.literal_eval(value)
                except ValueError:
                    print(f"migrations/versions/{name}: {target.id} is not a literal", file=sys.stderr)
                    sys.exit(1)
    if not isinstance(values.get("revision"), str):
        continue
    revisions[values["revision"]] = name
    down = values.get("down_revision")
    if isinstance(down, str):
        parents.add(down)
    elif isinstance(down, (tuple, list)):
        parents.update(item for item in down if isinstance(item, str))

heads = sorted(rev for rev in revisions if rev not in parents)
if len(heads) > 1:
    for rev in heads:
        print(f"alembic head: {rev} (migrations/versions/{revisions[rev]})", file=sys.stderr)
    print(
        "add a merge migration on the extension branch whose down_revision is the tuple "
        "(<extension head>, <main head>); never edit an existing migration's down_revision",
        file=sys.stderr,
    )
    sys.exit(1)
PY

referenced=0
for path in "${excludes[@]}"; do
  path="${path%/}"
  if [ "$(git cat-file -t "$merged_tree:$path" 2>/dev/null || true)" = tree ]; then
    if git grep -n -F -e "$path" "$tree" --; then referenced=1; fi
    continue
  fi
  name="${path##*/}"
  stem="${name%.*}"
  if git grep -n -F -e "$name" "$tree" --; then referenced=1; fi
  if [ -n "$stem" ] && [ "$stem" != "$name" ] && git grep -n -w -F -e "$stem" "$tree" --; then
    referenced=1
  fi
done
if [ "$referenced" -ne 0 ]; then
  echo "warning: the snapshot still names an excluded file (lines above)" >&2
  [ "$allow_references" -eq 1 ] || { echo "not updating $branch; fix the source or pass --allow-references" >&2; exit 1; }
fi

echo "diff $source_rev -> snapshot:"
git diff --stat "$source_commit" "$tree"
if [ "$(git rev-parse "$parent^{tree}")" = "$tree" ]; then
  echo "$branch already has this tree; nothing to do"
  exit 0
fi
[ "$dry_run" -eq 0 ] || { echo "dry run: tree $tree"; exit 0; }

[ -n "$message" ] || message="HealthOS: snapshot of $label ($(date +%Y-%m-%d))"
commit="$(git commit-tree "$tree" -p "$parent" -m "$message")"

worktree=""
current=""
while IFS= read -r line; do
  case "$line" in
    "worktree "*) current="${line#worktree }" ;;
    "branch refs/heads/$branch") worktree="$current" ;;
  esac
done < <(git worktree list --porcelain)

if [ -n "$worktree" ]; then
  git -C "$worktree" merge --ff-only -q "$commit" || die "fast-forward of $branch failed in its worktree"
else
  git update-ref "refs/heads/$branch" "$commit" "$parent"
fi
echo "$branch -> $(git rev-parse --short "$commit") (parent $(git rev-parse --short "$parent"))"
