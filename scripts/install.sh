#!/usr/bin/env sh
# Install skills from this repository into an agent skills directory.
# Existing directories/links are skipped by default unless --force is passed.

set -eu

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SKILLS_SRC="$REPO_ROOT/skills"
TARGET_DIR="${HOME}/.agents/skills"
MODE="symlink"
FORCE=0

usage() {
  cat <<EOF
Usage: $(basename "$0") [options] [skill-name ...]

Options:
  --target <dir>   Destination skills directory (default: ~/.agents/skills)
  --copy           Copy skill folders instead of symlinking
  --force          Replace existing destination entries
  -h, --help       Show this help
EOF
}

SELECTED=""
while [ $# -gt 0 ]; do
  case "$1" in
    --target)
      TARGET_DIR="$2"
      shift 2
      ;;
    --copy)
      MODE="copy"
      shift
      ;;
    --force)
      FORCE=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    -*)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 1
      ;;
    *)
      SELECTED="$SELECTED $1"
      shift
      ;;
  esac
done

mkdir -p "$TARGET_DIR"

if [ -z "$SELECTED" ]; then
  for d in "$SKILLS_SRC"/*; do
    [ -d "$d" ] && SELECTED="$SELECTED $(basename "$d")"
  done
fi

for name in $SELECTED; do
  src="$SKILLS_SRC/$name"
  dst="$TARGET_DIR/$name"
  if [ ! -d "$src" ]; then
    echo "WARN: skill not found: $name" >&2
    continue
  fi
  if [ -e "$dst" ] || [ -L "$dst" ]; then
    if [ "$FORCE" -eq 1 ]; then
      rm -rf "$dst"
    else
      echo "SKIP (already exists): $dst"
      continue
    fi
  fi
  if [ "$MODE" = "copy" ]; then
    cp -R "$src" "$dst"
    echo "COPIED: $name -> $dst"
  else
    ln -s "$src" "$dst"
    echo "LINKED: $name -> $dst"
  fi
done
