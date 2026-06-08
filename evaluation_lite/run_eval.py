import os
import shutil
import subprocess
import sys
import json
import yaml
import tempfile
import base64
import logging

def _setup_docker_host():
    if "DOCKER_HOST" in os.environ:
        return
    try:
        res = subprocess.run(
            ["docker", "context", "ls", "--format", "{{if .Current}}{{.DockerEndpoint}}{{end}}"],
            capture_output=True, text=True, timeout=5
        )
        if res.returncode == 0:
            endpoint = res.stdout.strip()
            if endpoint:
                os.environ["DOCKER_HOST"] = endpoint
                logging.getLogger(__name__).info(f"Auto-configured DOCKER_HOST to active context endpoint: {endpoint}")
    except Exception:
        pass

_setup_docker_host()

from harness import BaseHarness, OpenHandsHarness, DockerHarness

logger = logging.getLogger(__name__)

BASE_IMAGE = os.environ.get("TAC_BASE_IMAGE", "ghcr.io/haochengxia/theagentcompany-lite-base:latest")

COMPOSE_SERVICE_MAP = {
    "gitlab": ["gitlab"],
    "rocketchat": ["rocketchat", "mongodb", "redis-stack"],
    "owncloud": ["owncloud", "owncloud-collabora"],
    "plane": ["plane", "plane-pg", "plane-redis"],
}


def ensure_services(dependencies: list[str], servers_dir: str | None = None):
    """Start only the docker-compose services needed by this task's dependencies."""
    if not dependencies:
        return

    if servers_dir is None:
        from pathlib import Path
        candidates = [
            Path(__file__).parent.parent / "TheAgentCompany" / "servers",
            Path(__file__).parent.parent / "servers",
        ]
        servers_dir = str(next((p for p in candidates if p.exists()), candidates[0]))

    compose_file = os.path.join(servers_dir, "docker-compose.yml")
    if not os.path.exists(compose_file):
        logger.warning(f"docker-compose.yml not found at {compose_file}, skipping service start")
        return

    services_to_start = []
    for dep in dependencies:
        if dep in COMPOSE_SERVICE_MAP:
            services_to_start.extend(COMPOSE_SERVICE_MAP[dep])

    if not services_to_start:
        return

    logger.info(f"Starting services: {services_to_start}")
    env = os.environ.copy()
    env.setdefault("GITLAB_PORT", "8929")

    result = subprocess.run(
        ["docker", "compose", "-p", "theagentcompany", "-f", compose_file,
         "up", "-d"] + services_to_start,
        capture_output=True, text=True, timeout=300, env=env,
    )
    if result.returncode != 0:
        logger.warning(f"docker compose up failed: {result.stderr[-500:]}")
    else:
        logger.info("Services started, waiting for healthchecks...")
        _wait_for_services(dependencies, timeout=180)


def _wait_for_services(services: list[str], timeout: int = 180):
    """Poll api-server healthcheck until all required services are healthy."""
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        all_ok = True
        for svc in services:
            try:
                r = subprocess.run(
                    ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                     f"http://localhost:2999/api/healthcheck/{svc}"],
                    capture_output=True, text=True, timeout=5,
                )
                if r.stdout.strip() != "200":
                    all_ok = False
                    break
            except Exception:
                all_ok = False
                break
        if all_ok:
            logger.info("All services healthy")
            return
        time.sleep(5)
    logger.warning(f"Timed out waiting for services after {timeout}s")


def load_dependencies(harness: BaseHarness, task_dir: str | None = None) -> list[str]:
    if task_dir:
        dep_path = os.path.join(task_dir, "dependencies.yml")
        if os.path.exists(dep_path):
            with open(dep_path) as f:
                dependencies = yaml.safe_load(f)
            if dependencies is None:
                dependencies = []
            return dependencies

    result = harness.run_command("cat /utils/dependencies.yml")
    assert result.exit_code == 0, f"Failed to load dependencies: {result.content}"
    lines = [l for l in result.content.splitlines() if not l.lstrip().startswith("#")]
    dependencies = yaml.safe_load("\n".join(lines)) or []
    return dependencies


def init_task_env(harness: BaseHarness, hostname: str, llm_api_key: str | None,
                  llm_base_url: str | None, llm_model: str | None,
                  port_overrides: dict | None = None):
    """Initialize task environment inside the container.

    Args:
        harness: The harness to run commands through.
        hostname: Server hostname for SERVICE_HOSTNAME.
        llm_api_key: LITELLM API key.
        llm_base_url: LITELLM base URL.
        llm_model: LITELLM model name.
        port_overrides: Optional dict of service -> port mappings for multi-instance.
    """
    env_vars = (
        f"SERVER_HOSTNAME={hostname} "
        f"LITELLM_API_KEY={llm_api_key} "
        f"LITELLM_BASE_URL={llm_base_url} "
        f"LITELLM_MODEL={llm_model} "
    )

    if port_overrides:
        env_vars += f"GITLAB_PORT={port_overrides.get('gitlab', 8929)} "
        env_vars += f"API_PORT={port_overrides.get('api_server', 2999)} "
        env_vars += f"ROCKETCHAT_PORT={port_overrides.get('rocketchat', 3000)} "
        env_vars += f"OWNCLOUD_PORT={port_overrides.get('owncloud', 8092)} "
        env_vars += f"PLANE_PORT={port_overrides.get('plane', 8091)} "

    command = (
        env_vars +
        "echo '' | sudo tee -a /etc/hosts && "
        "bash /utils/init.sh"
    )
    result = harness.run_command(command, timeout=900)
    assert result.exit_code == 0, f"init_task_env failed: {result.content}"


def run_solver(harness: BaseHarness, task_name: str, dependencies: list[str],
               save_final_state: bool, state_dir: str,
               save_screenshots: bool, screenshots_dir: str):
    instruction = "Complete the task in /instruction/task.md"
    if "gitlab" in dependencies:
        instruction += "\n\nGitlab username is 'root' and password is 'theagentcompany'"

    state = harness.run_agent(instruction=instruction, max_iterations=100,
                              dependencies=dependencies)

    if save_screenshots and getattr(state, 'screenshots', None):
        task_screenshots_dir = os.path.join(screenshots_dir, task_name)
        os.makedirs(task_screenshots_dir, exist_ok=True)
        for image_id, screenshot_data in enumerate(state.screenshots):
            image_data = base64.b64decode(screenshot_data.replace("data:image/png;base64,", ""))
            with open(os.path.join(task_screenshots_dir, f"{image_id}.png"), "wb") as f:
                f.write(image_data)

    if save_final_state:
        os.makedirs(state_dir, exist_ok=True)
        with open(os.path.join(state_dir, f"state_{task_name}.json"), "w") as f:
            json.dump(str(state), f, indent=4)

    return state


def run_evaluator(harness: BaseHarness, llm_api_key: str | None,
                  llm_base_url: str | None, llm_model: str | None,
                  trajectory_path: str, result_path: str):
    command = (
        f"LITELLM_API_KEY={llm_api_key} "
        f"LITELLM_BASE_URL={llm_base_url} "
        f"LITELLM_MODEL={llm_model} "
        f"DECRYPTION_KEY='theagentcompany is all you need' "
        f"python_default /utils/eval.py --trajectory_path {trajectory_path} --result_path {result_path}"
    )
    logger.info(f"Running evaluator: trajectory_path={trajectory_path}, result_path={result_path}")
    for attempt in range(3):
        result = harness.run_command(command, timeout=600)
        if result.exit_code == 0:
            break
        if attempt < 2:
            import time
            time.sleep(5)
    logger.info(f"Evaluator result: exit_code={result.exit_code}, content_len={len(result.content)}")
    if result.exit_code != 0:
        logger.warning(f"Evaluator in container failed (exit={result.exit_code}): {result.content[:500]}")


def _build_port_overrides(service_instance: dict | None) -> dict | None:
    if service_instance is None:
        return None
    return {
        "gitlab": service_instance.get("gitlab_port", 8929),
        "api_server": service_instance.get("api_port", 2999),
        "rocketchat": service_instance.get("rocketchat_port", 3000),
        "owncloud": service_instance.get("owncloud_port", 8092),
        "plane": service_instance.get("plane_port", 8091),
    }


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s:%(levelname)s - %(message)s")

    parser = argparse.ArgumentParser(description="TheAgentCompany V2 - Single task evaluation")
    parser.add_argument("--task", type=str, default=None,
                        help="Task name (e.g. gitlab-create-repo-1). Auto-resolves to task dir.")
    parser.add_argument("--task-dir", type=str, default=None,
                        help="Path to task directory (overrides --task)")
    parser.add_argument("--task-image-name", type=str, default=None,
                        help="(Legacy v1) Task image name. Ignored if --task or --task-dir is provided.")
    parser.add_argument("--outputs-path", type=str, default="./outputs",
                        help="Folder path to save trajectories and evaluation results")
    parser.add_argument("--server-hostname", type=str, default="localhost",
                        help="Server hostname")
    parser.add_argument("--harness", type=str, default="openhands",
                        choices=["openhands", "docker"],
                        help="Harness to use (default: openhands)")
    parser.add_argument("--base-image", type=str, default=None,
                        help="Base Docker image (default: TAC_BASE_IMAGE or tac-base-image:latest)")
    parser.add_argument("--agent-llm-config", type=str, default=None,
                        help="LLM config for agent (openhands harness only)")
    parser.add_argument("--env-llm-config", type=str, default=None,
                        help="LLM config for evaluation environment (NPC & evaluator)")
    parser.add_argument("--build-image-only", action="store_true",
                        help="Build runtime image and exit (openhands harness only)")
    parser.add_argument("--service-instance", type=str, default=None,
                        help="JSON string with service instance info (multi-instance mode)")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Print agent thinking and actions in color")
    args = parser.parse_args()

    service_instance = None
    if args.service_instance:
        service_instance = json.loads(args.service_instance)
        logger.info(f"Multi-instance mode: instance {service_instance.get('instance_id', 0)}")

    port_overrides = _build_port_overrides(service_instance)

    task_dir = None
    if args.task_dir:
        task_dir = os.path.abspath(args.task_dir)
    elif args.task:
        from pathlib import Path
        script_dir = Path(__file__).parent
        candidates = [
            script_dir.parent / "workspaces" / "tasks" / args.task,
            script_dir.parent / "TheAgentCompany" / "workspaces" / "tasks" / args.task,
        ]
        task_dir = str(next((p for p in candidates if p.exists()), candidates[-1]))

    if task_dir:
        task_short_name = os.path.basename(task_dir).replace("-image", "")
        base_image = args.base_image or BASE_IMAGE
        logger.info(f"V2 mode: task_dir={task_dir}, short_name={task_short_name}")
    elif args.task_image_name:
        base_image = args.task_image_name
        task_short_name = args.task_image_name.split("/")[-1].split(":")[0]
        logger.info(f"V1 compat mode: image={base_image}, short_name={task_short_name}")
    else:
        raise ValueError("Must provide --task, --task-dir, or --task-image-name")

    env_api_key = None
    env_base_url = None
    env_model = None
    outputs_path = os.path.abspath(args.outputs_path)
    os.makedirs(outputs_path, exist_ok=True)
    temp_dir = os.path.join(outputs_path, ".tmp")
    mount_path = os.path.join(temp_dir, f"mount_{task_short_name}")
    os.makedirs(mount_path, exist_ok=True)

    if args.harness == "openhands":
        from openhands.core.config import LLMConfig, get_llm_config_arg

        agent_llm_config = None
        if args.agent_llm_config:
            agent_llm_config = get_llm_config_arg(args.agent_llm_config)
        if agent_llm_config is None:
            raise ValueError(f"Could not find LLM config for agent: --agent-llm-config {args.agent_llm_config}")
        if agent_llm_config.api_key is None:
            raise ValueError("LLM API key is not set for agent")

        env_llm_config = None
        if args.env_llm_config:
            env_llm_config = get_llm_config_arg(args.env_llm_config)
        if env_llm_config is None:
            raise ValueError(f"Could not find LLM config for env: --env-llm-config {args.env_llm_config}")
        if env_llm_config.api_key is None:
            raise ValueError("LLM API key is not set for environment")

        env_api_key = env_llm_config.api_key.get_secret_value() if env_llm_config.api_key else None
        env_base_url = env_llm_config.base_url
        env_model = env_llm_config.model

        if args.build_image_only:
            logger.info("build-image-only mode, creating runtime and exiting")
            harness = OpenHandsHarness(base_image=base_image, llm_config=LLMConfig(),
                                       task_short_name=task_short_name)
            harness.start(mount_path=mount_path)
            logger.info("Runtime image built successfully")
            sys.exit()

        harness = OpenHandsHarness(base_image=base_image, llm_config=agent_llm_config,
                                   task_short_name=task_short_name, verbose=args.verbose,
                                   task_dir=task_dir)
        harness.start(mount_path=mount_path)

    elif args.harness == "docker":
        if args.env_llm_config:
            env_api_key = os.environ.get("LITELLM_API_KEY")
            env_base_url = os.environ.get("LITELLM_BASE_URL")
            env_model = os.environ.get("LITELLM_MODEL")

        harness = DockerHarness(base_image=base_image or BASE_IMAGE)
        harness.start(mount_path=mount_path)

    else:
        raise ValueError(f"Unknown harness: {args.harness}")

    assert isinstance(harness, BaseHarness)

    if task_dir:
        staging_dir = os.path.join(mount_path, "task_staging")
        if os.path.exists(staging_dir):
            try:
                shutil.rmtree(staging_dir)
            except PermissionError:
                subprocess.run(["sudo", "rm", "-rf", staging_dir], check=True)
        try:
            shutil.copytree(task_dir, staging_dir)
        except PermissionError:
            subprocess.run(["sudo", "mkdir", "-p", staging_dir], check=True)
            subprocess.run(["sudo", "cp", "-r", os.path.join(task_dir, "."), staging_dir], check=True)

        harness.setup_task_files(task_dir)

    dependencies = load_dependencies(harness, task_dir=task_dir)
    logger.info(f"Service dependencies: {dependencies}")

    ensure_services(dependencies)

    init_task_env(harness, args.server_hostname, env_api_key, env_base_url, env_model,
                  port_overrides=port_overrides)


    if isinstance(harness, OpenHandsHarness):
        from browsing import pre_login
        try:
            pre_login(harness._runtime, dependencies, save_screenshots=True,
                      screenshots_dir=os.path.join(outputs_path, "screenshots"))
        except Exception as e:
            logger.error(f"Failed to pre-login: {e}")
            init_task_env(harness, args.server_hostname, env_api_key, env_base_url, env_model,
                          port_overrides=port_overrides)
            pre_login(harness._runtime, dependencies, save_screenshots=True,
                      screenshots_dir=os.path.join(outputs_path, "screenshots"))

    state = run_solver(harness, task_short_name, dependencies,
                       save_final_state=True, state_dir=outputs_path,
                       save_screenshots=True,
                       screenshots_dir=os.path.join(outputs_path, "screenshots"))

    traj_tmp = os.path.join(tempfile.gettempdir(), f"traj_{task_short_name}.json")
    traj_in_mount = os.path.join(mount_path, f"traj_{task_short_name}.json")
    if os.path.exists(traj_tmp):
        try:
            shutil.copy2(traj_tmp, traj_in_mount)
        except PermissionError:
            subprocess.run(["sudo", "cp", traj_tmp, traj_in_mount], check=True)

    trajectory_path = f"/outputs/traj_{task_short_name}.json"
    result_path = f"/outputs/eval_{task_short_name}.json"

    run_evaluator(harness, env_api_key, env_base_url, env_model,
                  trajectory_path, result_path)

    if os.path.exists(traj_tmp):
        shutil.copy2(traj_tmp, os.path.join(outputs_path, f"traj_{task_short_name}.json"))

    for fname in [f"eval_{task_short_name}.json"]:
        src = os.path.join(mount_path, fname)
        dst = os.path.join(outputs_path, fname)
        if os.path.exists(src):
            try:
                shutil.move(src, dst)
            except PermissionError:
                subprocess.run(["sudo", "cp", src, dst], check=True)
                subprocess.run(["sudo", "rm", src], check=True)

    harness.stop()
    logger.info(f"Task {task_short_name} completed successfully")
