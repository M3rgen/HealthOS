#!/usr/bin/env bash
# Local check that branches carry no trace of an extension branch that must not reach them.
#
#   bash scripts/check_no_extension.sh [--github-only] [rev ...]     # default: main dev
#
# Two kinds of extension exist (see docs/BRANCHES.md), configured once per clone and kept out
# of the repository so that the public tree never names them:
#   git config --add healthos.extensionMarker <word>          # private extensions (eklenti/*)
#   git config --add healthos.githubExtension <branch>        # GitHub-only extension branches
#   git config --add healthos.githubExtensionMarker <word>    # words of GitHub-only code
#
# Private checks, per revision: every tracked file whose path or content contains a
# healthos.extensionMarker word (case-insensitive), and every file a private `eklenti/*` branch
# (one not listed in healthos.githubExtension) adds on top of main that also exists in the
# revision (a copied file under a new name).
#
# GitHub-only checks, per revision: every file a healthos.githubExtension branch adds on top of
# main that exists in the revision, and every file whose path or content contains a
# healthos.githubExtensionMarker word. They are skipped for the snapshot branch
# (healthos.githubBranch, default github-main) and for the GitHub-only extension branches
# themselves, where that code belongs; revisions are matched by branch name.
#
#   --github-only   run only the GitHub-only checks; `--github-only live` proves that no
#                   GitHub-only extension reached the VM (private markers belong in live)
#
# Set EXTENSION_BASE to compare against another branch than main.
#
# Exit 0: clean. Exit 1: a trace was found. Exit 2: nothing configured for the checks that
# run (refusing to report clean), unknown revision or extension branch, or bad argument.
# Not part of CI: extension branches and markers exist only in the maintainer's clone.
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

github_only=0
revs=()
while [ "$#" -gt 0 ]; do
  case "$1" in
    --github-only) github_only=1; shift ;;
    -h | --help) awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$0"; exit 0 ;;
    -*) echo "unknown argument: $1" >&2; exit 2 ;;
    *) revs+=("$1"); shift ;;
  esac
done
[ "${#revs[@]}" -gt 0 ] || revs=(main dev)
base="${EXTENSION_BASE:-main}"
snapshot_branch="$(git config --get healthos.githubBranch || echo github-main)"

read_config() { git config --get-all "$1" || true; }

markers=()
while IFS= read -r word; do [ -n "$word" ] && markers+=("$word"); done \
  < <(read_config healthos.extensionMarker)
github_markers=()
while IFS= read -r word; do [ -n "$word" ] && github_markers+=("$word"); done \
  < <(read_config healthos.githubExtensionMarker)
github_extensions=()
while IFS= read -r name; do [ -n "$name" ] && github_extensions+=("$name"); done \
  < <(read_config healthos.githubExtension)

if [ "$github_only" -eq 1 ]; then
  if [ "$((${#github_markers[@]} + ${#github_extensions[@]}))" -eq 0 ]; then
    echo "no healthos.githubExtension or healthos.githubExtensionMarker configured; refusing to report clean" >&2
    exit 2
  fi
elif [ "${#markers[@]}" -eq 0 ]; then
  echo "no healthos.extensionMarker configured; refusing to report clean" >&2
  exit 2
fi

for name in "${github_extensions[@]+"${github_extensions[@]}"}"; do
  git rev-parse --verify --quiet "refs/heads/$name^{commit}" >/dev/null || {
    echo "GitHub-only extension branch not found: $name (see healthos.githubExtension)" >&2
    exit 2
  }
done

is_github_extension() {
  local name
  for name in "${github_extensions[@]+"${github_extensions[@]}"}"; do
    [ "$1" = "$name" ] && return 0
  done
  return 1
}

private_extensions=()
if [ "$github_only" -eq 0 ]; then
  while IFS= read -r ref; do
    is_github_extension "$ref" || private_extensions+=("$ref")
  done < <(git for-each-ref --format='%(refname:short)' 'refs/heads/eklenti/*')
fi

# Report files of <rev> whose path or content contains one of the given words.
check_markers() {
  local rev="$1" word path
  shift
  for word in "$@"; do
    if git grep -il -F -e "$word" "$rev" --; then hit=1; fi
    while IFS= read -r path; do
      echo "$rev:$path (path)"
      hit=1
    done < <(git ls-tree -r --name-only "$rev" | grep -iF -e "$word" || true)
  done
}

# Report files that <ext> adds on top of the base and that also exist in <rev>.
check_added_files() {
  local rev="$1" ext="$2" label="$3" path
  while IFS= read -r path; do
    [ -z "$path" ] && continue
    if git cat-file -e "$rev:$path" 2>/dev/null; then
      echo "$rev:$path (added by $ext$label)"
      hit=1
    fi
  done < <(git diff --diff-filter=A --name-only "$base...$ext")
}

found_private=0
found_github=0
for rev in "${revs[@]}"; do
  git rev-parse --verify --quiet "$rev^{commit}" >/dev/null || {
    echo "unknown revision: $rev" >&2
    exit 2
  }
  if [ "$github_only" -eq 0 ]; then
    hit=0
    check_markers "$rev" "${markers[@]}"
    for ext in "${private_extensions[@]+"${private_extensions[@]}"}"; do
      check_added_files "$rev" "$ext" ""
    done
    [ "$hit" -eq 0 ] || found_private=1
  fi

  name="$(git rev-parse --symbolic-full-name "$rev" 2>/dev/null || true)"
  name="${name#refs/heads/}"
  if [ "$name" = "$snapshot_branch" ] || is_github_extension "$name"; then
    [ "$github_only" -eq 0 ] || echo "note: $rev may hold GitHub-only extension code; not checked" >&2
    continue
  fi
  hit=0
  check_markers "$rev" "${github_markers[@]+"${github_markers[@]}"}"
  for ext in "${github_extensions[@]+"${github_extensions[@]}"}"; do
    check_added_files "$rev" "$ext" ", GitHub-only"
  done
  [ "$hit" -eq 0 ] || found_github=1
done

if [ "$found_private" -ne 0 ]; then
  echo "private extension traces found; move them back to their eklenti/* branch" >&2
fi
if [ "$found_github" -ne 0 ]; then
  echo "GitHub-only extension traces found; they belong only on their branch and the GitHub snapshot" >&2
fi
[ "$((found_private + found_github))" -eq 0 ] || exit 1
echo "clean: ${revs[*]}"
