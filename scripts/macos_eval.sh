#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEFAULT_TASK="ds-sql-exercise"
DEFAULT_MODE="mock"
DEFAULT_SCOPE="smoke"
DEFAULT_MOCK_DURATION="1,2"
BASE_IMAGE="ghcr.io/haochengxia/theagentcompany-lite-base:latest"
UV_CACHE_DIR="$ROOT_DIR/.uv-cache"
UV_PYTHON="3.12"

MODE="$DEFAULT_MODE"
SCOPE="$DEFAULT_SCOPE"
TASK="$DEFAULT_TASK"
OUTPUTS_DIR=""
AUTO_INSTALL=0

usage() {
  cat <<EOF
Usage: $(basename "$0") <command> [options]

Commands:
  check           Verify macOS requirements for this repo
  install         Install project requirements and initialize local deps
  test            Run a validation workflow
  report          Show the latest report from summary.json / eval_*.json
  all             Run check -> install -> test -> report
  help            Show this help text

Options for 'test', 'report', and 'all':
  --mode mock|single       Run mock smoke/full validation or a real single task
  --scope smoke|full       For mock mode: one-task smoke test or full benchmark
  --task NAME              Task name for single mode or custom mock smoke task
  --outputs-dir PATH       Output directory override
  --auto-install           Let 'check' install missing brew-managed packages

Examples:
  $(basename "$0") check
  $(basename "$0") install
  $(basename "$0") test
  $(basename "$0") test --mode mock --scope full
  $(basename "$0") test --mode single --task ds-sql-exercise --outputs-dir "$ROOT_DIR/outputs"
  $(basename "$0") all
EOF
}

log() {
  printf '[INFO] %s\n' "$*"
}

warn() {
  printf '[WARN] %s\n' "$*" >&2
}

fail() {
  printf '[ERROR] %s\n' "$*" >&2
  exit 1
}

has_command() {
  command -v "$1" >/dev/null 2>&1
}

run_in_repo() {
  (
    cd "$ROOT_DIR"
    "$@"
  )
}

run_uv() {
  UV_CACHE_DIR="$UV_CACHE_DIR" run_in_repo uv "$@"
}

venv_python() {
  printf '%s\n' "$ROOT_DIR/.venv/bin/python"
}

default_outputs_dir() {
  case "$MODE" in
    mock)
      if [[ "$SCOPE" == "full" ]]; then
        printf '%s\n' "$ROOT_DIR/outputs_mock_full"
      else
        printf '%s\n' "$ROOT_DIR/outputs_mock_smoke"
      fi
      ;;
    single)
      printf '%s\n' "$ROOT_DIR/outputs"
      ;;
    *)
      printf '%s\n' "$ROOT_DIR/outputs"
      ;;
  esac
}

resolve_outputs_dir() {
  if [[ -z "$OUTPUTS_DIR" ]]; then
    OUTPUTS_DIR="$(default_outputs_dir)"
  fi
}

ensure_brew() {
  if ! has_command brew; then
    fail "Homebrew is required for automated installs. Install it from https://brew.sh and rerun."
  fi
}

ensure_git_repo() {
  if [[ ! -f "$ROOT_DIR/pyproject.toml" ]]; then
    fail "Could not find pyproject.toml under $ROOT_DIR"
  fi
}

detect_python_ok() {
  if ! has_command python3; then
    return 1
  fi

  python3 - <<'PY' >/dev/null 2>&1
import sys
sys.exit(0 if sys.version_info >= (3, 12) else 1)
PY
}

print_requirement_status() {
  local name="$1"
  local status="$2"
  local details="$3"
  printf '%-18s %-8s %s\n' "$name" "$status" "$details"
}

check_submodule_ready() {
  [[ -d "$ROOT_DIR/TheAgentCompany/workspaces/tasks" ]]
}

check_venv_ready() {
  [[ -x "$(venv_python)" ]]
}

check_openhands_ready() {
  if ! check_venv_ready; then
    return 1
  fi

  "$(venv_python)" - <<'PY' >/dev/null 2>&1
import openhands  # noqa: F401
PY
}

check_config_ready() {
  if [[ ! -f "$ROOT_DIR/config.toml" ]]; then
    return 1
  fi

  python3 - <<'PY' >/dev/null 2>&1
from pathlib import Path

text = Path("config.toml").read_text()
required = [
    "[llm.agent]",
    "[llm.env]",
    "model",
    "base_url",
    "api_key",
]
missing = [item for item in required if item not in text]
raise SystemExit(0 if not missing else 1)
PY
}

docker_cli_ready() {
  has_command docker
}

docker_daemon_ready() {
  docker_cli_ready && docker info >/dev/null 2>&1
}

docker_context_name() {
  if ! docker_cli_ready; then
    return 1
  fi
  docker context show 2>/dev/null || true
}

docker_context_endpoint() {
  if ! docker_cli_ready; then
    return 1
  fi
  docker context ls --format '{{if .Current}}{{.DockerEndpoint}}{{end}}' 2>/dev/null || true
}

docker_context_hint() {
  local context
  context="$(docker_context_name)"
  case "$context" in
    colima)
      if has_command colima; then
        printf '%s\n' "Run 'colima start' or switch contexts with 'docker context use default'."
      else
        printf '%s\n' "Docker is using the 'colima' context. Start Colima or switch to Docker Desktop with 'docker context use default'."
      fi
      ;;
    default)
      printf '%s\n' "Start Docker Desktop and wait for 'docker info' to succeed."
      ;;
    "")
      printf '%s\n' "No Docker context is active. Check 'docker context ls'."
      ;;
    *)
      printf '%s\n' "Start the daemon backing the '$context' context, or switch to another context with 'docker context use default'."
      ;;
  esac
}

buildx_ready() {
  docker_cli_ready && docker buildx version >/dev/null 2>&1
}

compose_ready() {
  docker_cli_ready && docker compose version >/dev/null 2>&1
}

colima_memory_gib() {
  if ! has_command colima; then
    return 1
  fi
  colima list 2>/dev/null | awk '$1 == "default" {print $5}' | sed 's/GiB$//'
}

prepare_docker_env() {
  local endpoint
  endpoint="$(docker_context_endpoint)"
  if [[ -n "$endpoint" ]]; then
    export DOCKER_HOST="$endpoint"
  fi
}

prepare_runtime_limits() {
  if [[ -n "${TAC_RUNTIME_MAX_MEMORY_GB:-}" || -n "${TAC_RUNTIME_MEM_LIMIT:-}" ]]; then
    return
  fi

  local colima_mem
  colima_mem="$(colima_memory_gib || true)"

  if [[ -n "$colima_mem" ]]; then
    if [[ "$colima_mem" -ge 8 ]]; then
      export TAC_RUNTIME_MAX_MEMORY_GB=3
      export TAC_RUNTIME_MEM_LIMIT=6g
    elif [[ "$colima_mem" -ge 6 ]]; then
      export TAC_RUNTIME_MAX_MEMORY_GB=2
      export TAC_RUNTIME_MEM_LIMIT=4g
    elif [[ "$colima_mem" -ge 4 ]]; then
      export TAC_RUNTIME_MAX_MEMORY_GB=2
      export TAC_RUNTIME_MEM_LIMIT=3g
    else
      export TAC_RUNTIME_MAX_MEMORY_GB=1
      export TAC_RUNTIME_MEM_LIMIT=1536m
    fi
    return
  fi

  export TAC_RUNTIME_MAX_MEMORY_GB=1
  export TAC_RUNTIME_MEM_LIMIT=1536m
}

ensure_submodule() {
  ensure_git_repo
  if check_submodule_ready; then
    log "TheAgentCompany submodule appears initialized."
    return
  fi

  log "Initializing git submodules..."
  run_in_repo git submodule update --init --recursive
}

ensure_buildx() {
  if buildx_ready; then
    return
  fi

  ensure_brew
  log "Installing docker-buildx with Homebrew..."
  brew install docker-buildx
  mkdir -p "$HOME/.docker/cli-plugins"
  ln -sfn "$(brew --prefix)/opt/docker-buildx/bin/docker-buildx" "$HOME/.docker/cli-plugins/docker-buildx"

  if ! buildx_ready; then
    fail "docker-buildx is still not ready. Please verify installation manually."
  fi
}

ensure_uv() {
  if has_command uv; then
    return
  fi

  ensure_brew
  log "Installing uv with Homebrew..."
  brew install uv
}

ensure_python() {
  if detect_python_ok; then
    return
  fi

  ensure_brew
  log "Installing python@3.12 with Homebrew..."
  brew install python@3.12

  if ! detect_python_ok; then
    warn "python3 is still not resolving to Python 3.12+."
    warn "You may need to adjust PATH, for example:"
    warn '  export PATH="/opt/homebrew/opt/python@3.12/bin:$PATH"'
    fail "Python 3.12+ is required for the OpenHands setup path."
  fi
}

sync_project_deps() {
  mkdir -p "$UV_CACHE_DIR"
  log "Syncing Python dependencies with OpenHands extras using uv and Python $UV_PYTHON..."
  run_uv sync --python "$UV_PYTHON" --extra openhands
}

prepare_outputs_dir() {
  resolve_outputs_dir
  rm -rf "$OUTPUTS_DIR"
  mkdir -p "$OUTPUTS_DIR"
}

check_requirements() {
  ensure_git_repo

  print_requirement_status "Platform" "$(uname -s)" "$(uname -m)"

  if [[ "$(uname -s)" != "Darwin" ]]; then
    fail "This helper is intended for macOS only."
  fi

  if has_command brew; then
    print_requirement_status "Homebrew" "OK" "$(brew --prefix)"
  else
    print_requirement_status "Homebrew" "MISSING" "Needed for automated package installs"
  fi

  if detect_python_ok; then
    print_requirement_status "Python" "OK" "$(python3 --version 2>/dev/null)"
  elif has_command python3; then
    print_requirement_status "Python" "OLD" "$(python3 --version 2>/dev/null) (need 3.12+)"
  else
    print_requirement_status "Python" "MISSING" "Need python3 3.12+"
  fi

  if has_command uv; then
    print_requirement_status "uv" "OK" "$(uv --version)"
  else
    print_requirement_status "uv" "MISSING" "Run install or brew install uv"
  fi

  if check_venv_ready; then
    print_requirement_status "Virtualenv" "OK" "$("$(venv_python)" --version 2>/dev/null)"
  else
    print_requirement_status "Virtualenv" "MISSING" "Run install to create .venv"
  fi

  if check_openhands_ready; then
    print_requirement_status "OpenHands" "OK" "Installed in .venv"
  else
    print_requirement_status "OpenHands" "MISSING" "Run install to add openhands-ai"
  fi

  if docker_cli_ready; then
    print_requirement_status "Docker CLI" "OK" "$(docker --version)"
  else
    print_requirement_status "Docker CLI" "MISSING" "Install Docker Desktop for Mac"
  fi

  if docker_daemon_ready; then
    print_requirement_status "Docker Daemon" "OK" "Running"
  else
    local endpoint
    endpoint="$(docker_context_endpoint)"
    print_requirement_status "Docker Daemon" "WAIT" "${endpoint:-daemon unavailable}"
  fi

  local context
  context="$(docker_context_name)"
  if [[ -n "$context" ]]; then
    print_requirement_status "Docker Context" "INFO" "$context"
  fi

  if [[ "$context" == "colima" ]]; then
    local colima_mem
    colima_mem="$(colima_memory_gib || true)"
    if [[ -n "$colima_mem" ]]; then
      if [[ "$colima_mem" -lt 4 ]]; then
        print_requirement_status "Colima Memory" "LOW" "${colima_mem}GiB (recommend 6-8GiB for real eval)"
      else
        print_requirement_status "Colima Memory" "OK" "${colima_mem}GiB"
      fi
    fi
  fi

  if buildx_ready; then
    print_requirement_status "Buildx" "OK" "$(docker buildx version | head -n 1)"
  elif docker_cli_ready; then
    print_requirement_status "Buildx" "MISSING" "Run install to configure docker-buildx plugin"
  else
    print_requirement_status "Buildx" "MISSING" "Install Docker Desktop or Docker CLI first"
  fi

  if compose_ready; then
    print_requirement_status "Compose" "OK" "$(docker compose version | head -n 1)"
  else
    print_requirement_status "Compose" "WAIT" "Usually available once Docker Desktop is running"
  fi

  if check_submodule_ready; then
    local task_count
    task_count="$(find "$ROOT_DIR/TheAgentCompany/workspaces/tasks" -maxdepth 1 -mindepth 1 -type d | wc -l | tr -d ' ')"
    print_requirement_status "Submodule" "OK" "Tasks available: $task_count"
  else
    print_requirement_status "Submodule" "MISSING" "Run install to init TheAgentCompany"
  fi

  print_requirement_status "uv cache" "OK" "$UV_CACHE_DIR"

  if (( AUTO_INSTALL )); then
    log "Auto-install requested. Installing missing brew-managed dependencies where possible..."
    ensure_python
    ensure_uv
    if docker_cli_ready; then
      ensure_buildx
    fi
  fi
}

install_requirements() {
  ensure_git_repo
  ensure_brew
  ensure_python
  ensure_uv
  ensure_submodule
  sync_project_deps

  if docker_cli_ready; then
    ensure_buildx
  fi

  if ! docker_cli_ready; then
    warn "Docker CLI is not installed."
    warn "Mock eval is ready, but real eval still needs Docker Desktop:"
    warn "  https://docs.docker.com/desktop/setup/install/mac-install/"
    return
  fi

  if ! docker_daemon_ready; then
    warn "Docker is installed but the daemon is not reachable."
    warn "Mock eval is ready. Start Docker Desktop or Colima before real evals."
    return
  fi

  log "Ensuring base Docker image is available..."
  prepare_docker_env
  local need_build=0
  if ! docker image inspect "$BASE_IMAGE" >/dev/null 2>&1; then
    need_build=1
  else
    local host_arch
    host_arch="$(uname -m)"
    local image_arch
    image_arch="$(docker image inspect --format='{{.Architecture}}' "$BASE_IMAGE" 2>/dev/null || echo "")"
    if [[ "$host_arch" == "arm64" || "$host_arch" == "aarch64" ]]; then
      if [[ "$image_arch" == "amd64" || "$image_arch" == "x86_64" ]]; then
        log "Host is ARM64 but base image is AMD64. Forcing local rebuild to run natively."
        docker rmi -f "$BASE_IMAGE" >/dev/null 2>&1 || true
        need_build=1
      fi
    fi
  fi

  if (( need_build )); then
    local host_arch
    host_arch="$(uname -m)"
    local pulled=0
    if [[ "$host_arch" != "arm64" && "$host_arch" != "aarch64" ]]; then
      log "Attempting to pull base image..."
      if docker pull "$BASE_IMAGE"; then
        pulled=1
      fi
    fi

    if (( ! pulled )); then
      log "Building base image locally..."
      run_in_repo make build-base
    fi
  else
    log "Base image already present and matches host architecture: $BASE_IMAGE"
  fi
}

ensure_mock_ready() {
  if ! check_venv_ready; then
    fail "Virtual environment is missing. Run: $(basename "$0") install"
  fi
  if ! check_submodule_ready; then
    fail "TheAgentCompany tasks are missing. Run: $(basename "$0") install"
  fi
}

ensure_single_ready() {
  ensure_mock_ready
  if ! check_openhands_ready; then
    fail "OpenHands is not installed in .venv. Run: $(basename "$0") install"
  fi
  if ! check_config_ready; then
    fail "config.toml is missing required [llm.agent]/[llm.env] settings."
  fi
  if ! docker_daemon_ready; then
    fail "Docker daemon is not reachable for context '$(docker_context_name)'. $(docker_context_hint)"
  fi
}

print_single_run_plan() {
  prepare_runtime_limits
  log "Real eval preflight for task '$TASK'"
  log "Step 1/5: use Python env at $(venv_python)"
  log "Step 2/5: load LLM settings from config.toml sections [llm.agent] and [llm.env]"
  log "Step 3/5: start Docker-backed OpenHands runtime for task '$TASK'"
  log "Step 4/5: run evaluation_lite/run_eval.py with --verbose to expose agent steps"
  log "Step 5/5: write outputs under $OUTPUTS_DIR"
  log "Readiness:"
  log "  config.toml: $(check_config_ready && echo ready || echo missing)"
  log "  openhands: $(check_openhands_ready && echo ready || echo missing)"
  log "  docker daemon: $(docker_daemon_ready && echo ready || echo not-ready)"
  log "  docker context: $(docker_context_name)"
  log "  docker endpoint: $(docker_context_endpoint)"
  if [[ "$(docker_context_name)" == "colima" ]]; then
    log "  colima memory: $(colima_memory_gib 2>/dev/null || echo unknown)GiB"
    log "  runtime memory cap: ${TAC_RUNTIME_MAX_MEMORY_GB:-unset}GiB"
    log "  container mem limit: ${TAC_RUNTIME_MEM_LIMIT:-unset}"
  fi
  if ! docker_daemon_ready; then
    log "  suggested fix: $(docker_context_hint)"
  fi
  log "Command:"
  log "  $(venv_python) evaluation_lite/run_eval.py --task $TASK --agent-llm-config agent --env-llm-config env --server-hostname localhost --verbose --outputs-path $OUTPUTS_DIR"
}

run_mock_test() {
  local python_bin
  python_bin="$(venv_python)"

  prepare_outputs_dir
  if [[ "$SCOPE" == "full" ]]; then
    log "Running full mock benchmark into $OUTPUTS_DIR..."
    run_in_repo "$python_bin" evaluation_lite/scheduler.py \
      --agent-llm-config agent \
      --env-llm-config env \
      --mock \
      --mock-duration "$DEFAULT_MOCK_DURATION" \
      --outputs-path "$OUTPUTS_DIR"
  else
    log "Running mock smoke test for task '$TASK' into $OUTPUTS_DIR..."
    run_in_repo "$python_bin" evaluation_lite/scheduler.py \
      --agent-llm-config agent \
      --env-llm-config env \
      --mock \
      --mock-duration "$DEFAULT_MOCK_DURATION" \
      --tasks "$TASK" \
      --outputs-path "$OUTPUTS_DIR"
  fi

  log "Mock mode note: task PASS/FAIL is simulated. A completed run means the local workflow is working."
}

run_single_test() {
  local python_bin
  python_bin="$(venv_python)"

  prepare_outputs_dir
  prepare_docker_env
  prepare_runtime_limits
  print_single_run_plan
  log "Running single real task '$TASK' into $OUTPUTS_DIR..."
  run_in_repo "$python_bin" evaluation_lite/run_eval.py \
    --task "$TASK" \
    --agent-llm-config agent \
    --env-llm-config env \
    --server-hostname localhost \
    --verbose \
    --outputs-path "$OUTPUTS_DIR"
}

run_test() {
  ensure_git_repo
  case "$MODE" in
    mock)
      ensure_mock_ready
      run_mock_test
      ;;
    single)
      resolve_outputs_dir
      print_single_run_plan
      ensure_single_ready
      run_single_test
      ;;
    *)
      fail "Unsupported mode: $MODE"
      ;;
  esac
}

show_report() {
  resolve_outputs_dir
  local summary_path="$OUTPUTS_DIR/summary.json"

if [[ -f "$summary_path" ]]; then
  log "Summary report from $summary_path"
  python3 - "$summary_path" <<'PY'
import json
import pathlib
import sys

summary_path = pathlib.Path(sys.argv[1])
data = json.loads(summary_path.read_text())
print(f"Total tasks : {data.get('total', 0)}")
print(f"Passed      : {data.get('passed', 0)}")
print(f"Failed      : {data.get('failed', 0)}")
print(f"Duration(s) : {data.get('duration_seconds', 'n/a')}")
print(f"Instances   : {data.get('num_instances', 'n/a')}")
results = data.get("results", [])
if results:
    print("")
    print("Task results:")
    for item in results:
        status = "PASS" if item.get("success") else "FAIL"
        task = item.get("task", "<unknown>")
        duration = item.get("duration", "n/a")
        suffix = " (skipped)" if item.get("skipped") else ""
        print(f"  {status:<4} {task} [{duration}s]{suffix}")
PY
    if [[ "$MODE" == "mock" ]]; then
      log "Mock mode note: PASS/FAIL is randomized by the simulator and does not mean setup is broken."
    fi
    return
  fi

  log "No summary.json found. Falling back to eval_*.json files in $OUTPUTS_DIR"
  python3 - "$OUTPUTS_DIR" <<'PY'
import glob
import json
import os
import sys

root = sys.argv[1]
paths = sorted(glob.glob(os.path.join(root, "eval_*.json")))
if not paths:
    print("No report files found.")
    sys.exit(1)

passed = 0
failed = 0
for path in paths:
    with open(path) as f:
        data = json.load(f)
    task = os.path.basename(path)[5:-5]
    if "error" in data:
        failed += 1
        print(f"FAIL {task}: {data.get('error')}")
    else:
        passed += 1
        score = data.get("final_score", {})
        print(f"PASS {task}: score={score.get('result', '?')}/{score.get('total', '?')}")
print("")
print(f"Passed: {passed}")
print(f"Failed: {failed}")
PY
}

parse_args() {
  local command="${1:-help}"
  shift || true

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --mode)
        MODE="${2:-}"
        shift 2
        ;;
      --scope)
        SCOPE="${2:-}"
        shift 2
        ;;
      --task)
        TASK="${2:-}"
        shift 2
        ;;
      --outputs-dir)
        OUTPUTS_DIR="${2:-}"
        shift 2
        ;;
      --auto-install)
        AUTO_INSTALL=1
        shift
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      *)
        fail "Unknown option: $1"
        ;;
    esac
  done

  case "$MODE" in
    mock|single) ;;
    *)
      fail "Unsupported mode: $MODE"
      ;;
  esac

  case "$SCOPE" in
    smoke|full) ;;
    *)
      fail "Unsupported scope: $SCOPE"
      ;;
  esac

  case "$command" in
    check)
      check_requirements
      ;;
    install)
      install_requirements
      ;;
    test)
      run_test
      ;;
    report)
      show_report
      ;;
    all)
      check_requirements
      install_requirements
      run_test
      show_report
      ;;
    help)
      usage
      ;;
    *)
      fail "Unknown command: $command"
      ;;
  esac
}

parse_args "$@"
