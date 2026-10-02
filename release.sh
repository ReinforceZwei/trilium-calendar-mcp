#!/usr/bin/env bash
# Tag a release: bump the version, commit, tag, and optionally push.
#
#   ./release.sh 0.2.0          # bump + commit + tag locally
#   ./release.sh 0.2.0 --push   # ... and push the commit and tag
#
# Pushing the v* tag makes GitHub Actions run the tests, build and publish the
# multi-arch image to GHCR (0.2.0, 0.2, latest) and open a release.
set -euo pipefail

usage() {
  echo "usage: ./release.sh <version> [--push|-p]   e.g. ./release.sh 0.2.0 --push" >&2
  exit 1
}

version="${1:-}"
push=false
for arg in "$@"; do
  case "$arg" in
    -p | --push) push=true ;;
  esac
done

version="${version#v}"
if [[ ! "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "error: version must look like 1.2.3 (got '${version:-<empty>}')" >&2
  usage
fi

cd "$(dirname "$0")"

if [[ -n "$(git status --porcelain)" ]]; then
  echo "error: working tree is dirty; commit or stash first" >&2
  exit 1
fi
if git rev-parse -q --verify "refs/tags/v$version" >/dev/null; then
  echo "error: tag v$version already exists" >&2
  exit 1
fi

python3 - "$version" <<'PY'
import re
import sys
from pathlib import Path

version = sys.argv[1]
for path, pattern, replacement in (
    (Path("pyproject.toml"), r'(?m)^version = "[^"]+"', f'version = "{version}"'),
    (
        Path("src/trilium_calendar_mcp/__init__.py"),
        r'(?m)^__version__ = "[^"]+"',
        f'__version__ = "{version}"',
    ),
):
    text = path.read_text(encoding="utf8")
    new, count = re.subn(pattern, replacement, text, count=1)
    if count != 1:
        sys.exit(f"could not update the version in {path}")
    path.write_text(new, encoding="utf8")
    print(f"  ~ {path} -> {version}")
PY

git add pyproject.toml src/trilium_calendar_mcp/__init__.py
if [[ -z "$(git status --porcelain)" ]]; then
  echo "  = version is already $version; tagging the current commit"
else
  git commit -m "chore: release v$version"
fi
git tag -a "v$version" -m "v$version"
echo "tagged v$version"

if [[ "$push" == true ]]; then
  git push
  git push --tags
  echo "pushed; GitHub Actions will build the image and open the release"
else
  echo "not pushed (re-run with --push, or: git push && git push --tags)"
fi
