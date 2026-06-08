#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BASE_IMAGE="ghcr.io/haochengxia/theagentcompany-lite-base:latest"
REMOVE_DOCKER=0

usage() {
  cat <<EOF
Usage: $(basename "$0") [--with-docker]

Removes local project artifacts created by setup and test runs:
  - .venv
  - .uv-cache
  - outputs
  - outputs*
  - .pytest_cache
  - temporary test result folders created by the macOS helper

Options:
  --with-docker    Also remove the local base image and prune Docker builder cache
  -h, --help       Show this help text
EOF
}

log() {
  printf '[INFO] %s\n' "$*"
}

remove_path() {
  local path="$1"
  if [[ -e "$path" ]]; then
    rm -rf "$path"
    log "Removed $path"
  else
    log "Skipped $path (not present)"
  fi
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --with-docker)
        REMOVE_DOCKER=1
        shift
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      *)
        printf '[ERROR] Unknown option: %s\n' "$1" >&2
        exit 1
        ;;
    esac
  done
}

clean_local() {
  remove_path "$ROOT_DIR/.venv"
  remove_path "$ROOT_DIR/.uv-cache"
  remove_path "$ROOT_DIR/.pytest_cache"

  shopt -s nullglob
  local path
  for path in "$ROOT_DIR"/outputs*; do
    remove_path "$path"
  done
  shopt -u nullglob
}

clean_docker() {
  if ! command -v docker >/dev/null 2>&1; then
    log "Docker not available; skipping image and cache cleanup."
    return
  fi

  if docker image inspect "$BASE_IMAGE" >/dev/null 2>&1; then
    docker rmi "$BASE_IMAGE" || true
    log "Requested removal of $BASE_IMAGE"
  else
    log "Base image not present: $BASE_IMAGE"
  fi

  docker builder prune -f || true
  log "Requested Docker builder cache prune"
}

parse_args "$@"
clean_local
if (( REMOVE_DOCKER )); then
  clean_docker
fi
