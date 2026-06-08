
<div style="text-align: center;">
  <img src="/docs/assets/logo.svg" alt="atc-lite-logo"/>
</div>

A faster, parallelized evaluation infrastructure for [TheAgentCompany](https://github.com/TheAgentCompany/TheAgentCompany) benchmarks.

**3.1 hours** instead of 17.5 hours. **4 GB** instead of 700 GB. Mock mode for 30-second infra testing.

Uses the same [OpenHands](https://github.com/All-Hands-AI/OpenHands) config and task format as upstream — just swap the runner.

> **Pre-requisite:** Before running any test commands, ensure you have **Docker** (Docker Desktop on macOS) installed and running, then run the installer to set up submodules, virtual environments, and base Docker images:
> ```bash
> # For macOS:
> ./scripts/macos_eval.sh install
> # For Linux / Generic setup:
> make setup-full
> ```

Quick test with no external services needed (after running setup):

```bash
./scripts/macos_eval.sh test   # macOS smoke test
# or:
make single TASK=ds-sql-exercise
```

See [docs/DESIGN.md](docs/DESIGN.md) for architecture and [docs/SETUP.md](docs/SETUP.md) for full setup instructions including service deployment.

## Quick Start

```bash
git clone git@github.com:illinoisdata/TheAgentCompany-lite.git && cd TheAgentCompany-lite
make setup-full  # submodule + uv deps (with openhands) + docker base image
make mock        # mock benchmark (no LLM, no services needed)
make dry-run     # see execution plan
make single TASK=ds-sql-exercise   # quick real task (no external services needed)
```

> **Note**: The first run of any task takes 10-20 minutes to build the OpenHands runtime image. Subsequent runs start in seconds.

## macOS Quick Start

If you are on macOS, the helper script gives you a safer first-run flow than the raw `make` targets:

```bash
git clone git@github.com:illinoisdata/TheAgentCompany-lite.git && cd TheAgentCompany-lite
./scripts/macos_eval.sh check
./scripts/macos_eval.sh install
./scripts/macos_eval.sh test
./scripts/macos_eval.sh report
```

What this does:

- Uses a repo-local `uv` cache so sandboxed or restricted environments do not fail on `~/.cache/uv`.
- Initializes the `TheAgentCompany` submodule automatically.
- Creates a local `.venv` with the OpenHands dependency set.
- Defaults `test` to a one-task mock smoke run, so a new user gets a fast validation before trying a full benchmark.
- Treats Docker as optional for mock setup, but still checks and reports Docker readiness for real evals.

Common follow-ups:

```bash
./scripts/macos_eval.sh test --mode mock --scope full
./scripts/macos_eval.sh test --mode single --task ds-sql-exercise
./scripts/macos_clean.sh
```

> **Note**: In mock mode, PASS/FAIL is simulated. A completed run means the local workflow is working even if the mock task result is `FAIL`.

If `config.toml` is already set up and you want to run the real SQL exercise with verbose internal steps:

```bash
./scripts/macos_eval.sh test --mode single --task ds-sql-exercise
```

That path uses `[llm.agent]` and `[llm.env]` from `config.toml`, enables `--verbose`, and writes results to `./outputs`.

Tasks that depend on GitLab, RocketChat, ownCloud, or Plane need those services running first. See [docs/SETUP.md](#2-server-setup-services) for service deployment. The harness starts services on-demand via `ensure_services()` when you run a task that needs them.

## Configuration

LLM config uses the same `config.toml` format as upstream OpenHands. Create it in the project root (after cloning and `make setup-full`):

```toml
[llm.agent]
model = "gpt-4o-mini"
base_url = "https://api.openai.com/v1"
api_key = "sk-..."

[llm.env]
model = "gpt-4o-mini"
base_url = "https://api.openai.com/v1"
api_key = "sk-..."
```

For Azure OpenAI:

```toml
[llm.agent]
model = "azure/<your-deployment-name>"
base_url = "https://<your-resource>.cognitiveservices.azure.com"
api_key = "<your-api-key>"
api_version = "2024-12-01-preview"

[llm.env]
model = "azure/<your-deployment-name>"
base_url = "https://<your-resource>.cognitiveservices.azure.com"
api_key = "<your-api-key>"
api_version = "2024-12-01-preview"
```

> **Important:** The `model` field must start with `azure/` prefix so litellm routes to Azure OpenAI. `base_url` should be just the endpoint, not the full deployment path.

For Azure AI Foundry / Azure AI Studio Project Unified Endpoints:

```toml
[llm.agent]
model = "openai/<your-deployment-name>"
base_url = "https://<your-resource>.services.ai.azure.com/api/projects/<your-project-name>/openai/v1"
api_key = "<your-api-key>"

[llm.env]
model = "openai/<your-deployment-name>"
base_url = "https://<your-resource>.services.ai.azure.com/api/projects/<your-project-name>/openai/v1"
api_key = "<your-api-key>"
```

> **Important:** For Azure AI Foundry unified project endpoints, use the `openai/` model prefix and append `/openai/v1` to your base URL. This enables standard OpenAI compatibility mode and bypasses date-based API versioning requirements.

`--agent-llm-config agent` maps to `[llm.agent]`, `--env-llm-config env` maps to `[llm.env]`. For mock mode, the config file is not needed.

## Prerequisites

| Requirement | Linux (Ubuntu/Debian) | macOS |
|---|---|---|
| **Docker** 24+ | `sudo apt-get install -y docker.io` | [Docker Desktop](https://docs.docker.com/desktop/setup/install/mac-install/) (includes BuildX + Compose) |
| **Docker BuildX** | `sudo apt-get install -y docker-buildx-plugin` | Included in Docker Desktop (or auto-configured via helper script if missing) |
| **Docker Compose v2** | `sudo apt-get install -y docker-compose-v2` | Included in Docker Desktop |
| **Python** 3.12+ | System package or pyenv | `brew install python@3.12` |
| **uv** | `curl -LsSf https://astral.sh/uv/install.sh \| sh` | `brew install uv` or same curl command |

> **Quick install (Linux):** `sudo apt-get install -y docker.io docker-buildx-plugin docker-compose-v2`
>
> **Quick install (macOS):** Install [Docker Desktop](https://docs.docker.com/desktop/setup/install/mac-install/) + `brew install uv`

## Install

```bash
make setup-full          # full: submodule + openhands deps + docker base image
# or:
make setup               # base only: mock mode + scheduler
```

Or manually:

```bash
git submodule update --init --recursive
uv sync --extra openhands  # requires Python >=3.12
docker pull ghcr.io/haochengxia/theagentcompany-lite-base:latest || make build-base
```

> **Note**: If `docker pull` fails (private registry or no access), `make build-base` builds the image locally from source (~5 min first time).
>
> **macOS note:** `./scripts/macos_eval.sh install` is recommended for first-time setup because it handles the submodule, local `uv` cache, automatically installs and configures the `docker-buildx` plugin (critical for Colima users where the legacy builder fails with network packet issues), and performs friendlier Docker checks.
>
> **Apple Silicon (M1/M2/M3/M4) Note:** Headless Chromium inside Playwright can crash under QEMU emulation if you use `amd64` images. The `./scripts/macos_eval.sh install` script automatically detects Apple Silicon hosts and builds the base image locally to run natively as `arm64`, and configures the container with `shm_size='2g'` to ensure smooth, crash-free browser operations.

## Usage

```bash
# Mock benchmark (no LLM, no services needed)
make mock

# Quick real task (no external services needed)
make single TASK=ds-sql-exercise
make single TASK=sde-install-go
make single TASK=sde-install-openjdk

# Tasks that need GitLab/RocketChat/ownCloud/Plane
# (services must be running first, see SETUP.md)
make single TASK=sde-add-wiki-page
make single TASK=gitlab-create-repo-1
make single TASK=admin-arrange-meeting-rooms

# Multiple tasks
uv run python evaluation_lite/scheduler.py \
  --agent-llm-config agent --env-llm-config env \
  --tasks "admin-arrange-meeting-rooms,pm-update-project-milestones" \
  --mock --mock-duration 5,8 --outputs-path ./outputs_mock

# Full benchmark (1 instance)
uv run python evaluation_lite/scheduler.py \
  --agent-llm-config agent --env-llm-config env \
  --server-hostname localhost --outputs-path ./outputs

# Full benchmark (6 instances)
uv run python evaluation_lite/scheduler.py \
  --agent-llm-config agent --env-llm-config env \
  --server-hostname tac_test \
  --num-instances 6 --full-stack-ids 0,4,5 \
  --outputs-path ./outputs
```

## Files

```
evaluation_lite/
  scheduler.py          # Round-based parallel scheduler
  service_manager.py    # Multi-instance port mapping and locking
  harness.py            # Docker and OpenHands harness interfaces
  run_eval.py           # Single task execution pipeline
  run_eval_mock.py      # Mock executor for infra testing
  browsing.py           # Browser automation for pre-login

.github/workflows/
  e2e-test.yml          # CI: E2E test with on-demand service startup
```

## Makefile Targets

| Target | Description |
|--------|-------------|
| `make setup` | Init submodule, install base deps, pull/build docker image |
| `make setup-full` | Same + openhands (Python >=3.12) |
| `make build-base` | Build base image locally (~5 min) |
| `make mock` | Run mock benchmark |
| `make dry-run` | Print execution plan |
| `make single TASK=name` | Run one task |
| `make clean` | Remove outputs and venv |
