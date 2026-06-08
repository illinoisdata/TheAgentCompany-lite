"""Harness interface for TheAgentCompany benchmark evaluation.

Two implementations: DockerHarness (plain Docker, custom agents) and
OpenHandsHarness (wraps OpenHands runtime, original behavior).
"""
import asyncio, json, os, shutil, subprocess, tempfile, time, logging, sys
from abc import ABC, abstractmethod
from dataclasses import dataclass

logger = logging.getLogger(__name__)

BOLD = "\033[1m"
DIM = "\033[2m"
CYAN = "\033[36m"
YELLOW = "\033[33m"
GREEN = "\033[32m"
RED = "\033[31m"
BLUE = "\033[34m"
MAGENTA = "\033[35m"
RESET = "\033[0m"


def _should_use_host_network() -> bool:
    """Use host networking only where it's dependable.

    OpenHands' host-network path works best on Linux. On macOS, especially with
    Colima, the runtime container may start but never become reachable from the
    client. Allow an explicit override via TAC_USE_HOST_NETWORK=1/0.
    """
    override = os.getenv("TAC_USE_HOST_NETWORK")
    if override is not None:
      return override.strip().lower() in {"1", "true", "yes", "on"}
    return sys.platform.startswith("linux")


def _runtime_startup_env_vars() -> dict[str, str]:
    """Conservative runtime defaults for local macOS/Colima setups."""
    env: dict[str, str] = {}
    max_memory = os.getenv("TAC_RUNTIME_MAX_MEMORY_GB")
    if max_memory:
        env["RUNTIME_MAX_MEMORY_GB"] = max_memory
    elif sys.platform == "darwin":
        env["RUNTIME_MAX_MEMORY_GB"] = "1"
    return env


def _docker_runtime_kwargs() -> dict:
    """Container runtime limits that help avoid OOM kills on small local VMs."""
    kwargs: dict = {}
    mem_limit = os.getenv("TAC_RUNTIME_MEM_LIMIT")
    if mem_limit:
        kwargs["mem_limit"] = mem_limit
    elif sys.platform == "darwin":
        kwargs["mem_limit"] = "1536m"
    kwargs["shm_size"] = "2g"
    return kwargs


def _format_runtime_start_failure(runtime, error: Exception) -> Exception:
    """Attach container logs when the runtime exits during connect()."""
    try:
        container_name = getattr(runtime, "container_name", None)
        docker_client = getattr(runtime, "docker_client", None)
        if container_name and docker_client is not None:
            container = docker_client.containers.get(container_name)
            logs = container.logs(tail=200).decode("utf-8", errors="replace").strip()
            state = container.attrs.get("State", {})
            status = state.get("Status", "unknown")
            exit_code = state.get("ExitCode", "unknown")
            oom_killed = state.get("OOMKilled", False)
            wrapped = RuntimeError(
                f"Runtime startup failed for container {container_name} "
                f"(status={status}, exit_code={exit_code}, oom_killed={oom_killed}).\n"
                f"Container logs:\n{logs}"
            )
            wrapped.__cause__ = error
            return wrapped
    except Exception:
        pass
    return error


def _truncate(s, max_len=300):
    s = s.strip()
    if len(s) <= max_len:
        return s
    return s[:max_len] + "..."


def _print_event(event):
    """Print a single OpenHands event with color coding."""
    from openhands.events.action import (
        Action, MessageAction, CmdRunAction, IPythonRunCellAction,
        BrowseInteractiveAction, FileWriteAction, FileEditAction,
    )
    from openhands.events.observation import (
        Observation, CmdOutputObservation, IPythonRunCellObservation,
        BrowserOutputObservation, FileWriteObservation, FileEditObservation,
    )

    if isinstance(event, MessageAction):
        role = getattr(event, "source", "agent")
        content = _truncate(event.content or "")
        if role == "user":
            print(f"  {BLUE}[user]{RESET} {content}")
        else:
            print(f"  {CYAN}{BOLD}[thinking]{RESET} {content}")

    elif isinstance(event, CmdRunAction):
        cmd = _truncate(event.command or "")
        print(f"  {YELLOW}[action:cmd]{RESET} {cmd}")

    elif isinstance(event, CmdOutputObservation):
        out = _truncate(event.content or "")
        code = getattr(event, "exit_code", "?")
        print(f"  {DIM}[obs:cmd exit={code}]{RESET} {_truncate(out, 200)}")

    elif isinstance(event, IPythonRunCellAction):
        code = _truncate(event.code or event.content or "")
        print(f"  {YELLOW}[action:ipython]{RESET} {code}")

    elif isinstance(event, IPythonRunCellObservation):
        out = _truncate(event.content or "")
        print(f"  {DIM}[obs:ipython]{RESET} {_truncate(out, 200)}")

    elif isinstance(event, BrowseInteractiveAction):
        act = _truncate(event.browser_actions or "")
        print(f"  {MAGENTA}[action:browse]{RESET} {act}")

    elif isinstance(event, BrowserOutputObservation):
        out = _truncate(getattr(event, "agent_obs_text", "") or event.content or "")
        print(f"  {DIM}[obs:browse]{RESET} {_truncate(out, 150)}")

    elif isinstance(event, (FileWriteAction, FileEditAction)):
        path = getattr(event, "path", "?")
        content = _truncate(getattr(event, "content", "") or "")
        print(f"  {YELLOW}[action:file] {path}{RESET} {content}")

    elif isinstance(event, (FileWriteObservation, FileEditObservation)):
        out = _truncate(event.content or "")
        print(f"  {DIM}[obs:file]{RESET} {_truncate(out, 150)}")

    elif isinstance(event, Observation):
        out = _truncate(event.content or str(event))
        print(f"  {DIM}[obs]{RESET} {_truncate(out, 200)}")

    elif isinstance(event, Action):
        out = _truncate(getattr(event, "thought", "") or getattr(event, "content", "") or str(event))
        print(f"  {YELLOW}[action]{RESET} {_truncate(out, 200)}")

    sys.stdout.flush()


@dataclass
class AgentState:
    success: bool = False
    trajectory_path: str = ""
    history: list | None = None


@dataclass
class CommandResult:
    exit_code: int = 0
    content: str = ""


class BaseHarness(ABC):
    @abstractmethod
    def start(self, mount_path=None):
        pass

    @abstractmethod
    def stop(self):
        pass

    def setup_task_files(self, task_dir: str):
        pass

    @abstractmethod
    def run_agent(self, instruction, max_iterations=100, **kwargs):
        pass


class DockerHarness(BaseHarness):
    """Plain Docker container harness. Subclass and override run_agent()."""

    def __init__(self, base_image="tac-base-image:latest",
                 container_name=None, network="host"):
        self.base_image = base_image
        self.container_name = container_name or f"tac-eval-{int(time.time())}"
        self.network = network
        self._mount_path = None

    def start(self, mount_path=None):
        self._mount_path = mount_path or tempfile.mkdtemp()
        os.makedirs(self._mount_path, exist_ok=True)
        subprocess.run(["docker", "run", "-d", "--name", self.container_name,
                        f"--network={self.network}",
                        "-v", f"{self._mount_path}:/outputs",
                        self.base_image, "tail", "-f", "/dev/null"],
                       check=True, capture_output=True)

    def stop(self):
        subprocess.run(["docker", "stop", self.container_name], capture_output=True)
        subprocess.run(["docker", "rm", "-f", self.container_name], capture_output=True)

    def run_command(self, command, timeout=300):
        result = subprocess.run(
            ["docker", "exec", self.container_name, "bash", "-c", command],
            capture_output=True, text=True, timeout=timeout)
        return CommandResult(exit_code=result.returncode,
                             content=result.stdout + result.stderr)

    def run_agent(self, instruction, max_iterations=100, **kwargs):
        raise NotImplementedError("Subclass DockerHarness and override run_agent().")


class OpenHandsHarness(BaseHarness):

    def __init__(self, base_image, llm_config=None, task_short_name="task",
                 verbose=False, config=None, task_dir=None):
        self.base_image = base_image
        self.llm_config = llm_config
        self.task_short_name = task_short_name
        self.verbose = verbose
        self.config = config
        self.task_dir = task_dir
        self._runtime = None
        self._state = None

    def _build_task_image(self, task_dir: str) -> str:
        """Build intermediate task image from base + task files.

        The lite base image uses ONBUILD directives that expect task files
        (evaluator.py, dependencies.yml, task.md) in the build context.
        We satisfy those ONBUILDs by building from the task directory,
        producing a task-specific image that OpenHands can then layer
        its runtime on top of.
        """
        tag = f"tac-task-{self.task_short_name}:latest"
        logger.info(f"Building task image {tag} from {task_dir}")

        dockerfile_path = os.path.join(task_dir, "Dockerfile")
        has_existing = os.path.exists(dockerfile_path)

        if has_existing:
            with open(dockerfile_path) as f:
                original = f.read()
            rewritten = []
            for line in original.splitlines():
                if line.strip().startswith("FROM"):
                    rewritten.append(f"FROM {self.base_image}")
                else:
                    rewritten.append(line)
            with open(dockerfile_path, "w") as f:
                f.write("\n".join(rewritten))
        else:
            with open(dockerfile_path, "w") as f:
                f.write(f"FROM {self.base_image}\n")

        try:
            result = subprocess.run(
                ["docker", "build", "--network", "host", "-t", tag, task_dir],
                capture_output=True, text=True, timeout=120,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    f"Failed to build task image:\n{result.stderr[-2000:]}"
                )
        finally:
            if has_existing:
                with open(dockerfile_path, "w") as f:
                    f.write(original)
            else:
                os.remove(dockerfile_path)

        logger.info(f"Task image {tag} built successfully")
        return tag

    def _build_runtime_image(self, base_image: str) -> str:
        """Pre-build the OpenHands runtime image on top of the task image.

        OpenHands 0.42.0's DockerRuntimeBuilder generates a Dockerfile that
        installs micromamba, poetry, playwright, etc. We build this ourselves
        and pass runtime_container_image to skip OpenHands' internal build.
        """
        from openhands.runtime.utils.runtime_build import prep_build_folder, BuildFromImageType

        tag = f"tac-runtime-{self.task_short_name}:latest"

        result = subprocess.run(
            ["docker", "images", "-q", tag],
            capture_output=True, text=True, timeout=30,
        )
        if result.stdout.strip():
            logger.info(f"Runtime image {tag} already exists, skipping build")
            return tag

        d = tempfile.mkdtemp(prefix="oh_runtime_")
        try:
            prep_build_folder(d, base_image, BuildFromImageType.SCRATCH, None)
            dockerfile_path = os.path.join(d, "Dockerfile")
            with open(dockerfile_path, "a") as f:
                f.write('RUN pip install "setuptools<70"\n')
            buildx_check = subprocess.run(
                ["docker", "buildx", "version"],
                capture_output=True, text=True, timeout=30,
            )
            if buildx_check.returncode == 0:
                build_cmd = ["docker", "buildx", "build", "--progress=plain",
                             "--network", "host", "-t", tag, "--load", d]
            else:
                logger.info("Docker buildx is unavailable; falling back to plain 'docker build'")
                build_cmd = ["docker", "build", "--network", "host", "-t", tag, d]
            result = subprocess.run(
                build_cmd,
                capture_output=True, text=True, timeout=1800,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    f"Failed to build runtime image with {' '.join(build_cmd)}:\n"
                    f"{(result.stderr or result.stdout)[-2000:]}"
                )
            logger.info(f"Runtime image {tag} built successfully")
        finally:
            shutil.rmtree(d, ignore_errors=True)

        return tag

    def start(self, mount_path=None):
        from openhands.core.config import OpenHandsConfig, SandboxConfig
        from openhands.core.main import create_runtime
        from openhands.utils.async_utils import call_async_from_sync

        effective_base = self.base_image
        runtime_image = None

        if self.task_dir and os.path.isdir(self.task_dir):
            effective_base = self._build_task_image(self.task_dir)
            runtime_image = self._build_runtime_image(effective_base)

        sandbox = SandboxConfig(
            base_container_image=effective_base,
            use_host_network=_should_use_host_network(),
            timeout=300,
            keep_runtime_alive=True,
            runtime_startup_env_vars=_runtime_startup_env_vars(),
            docker_runtime_kwargs=_docker_runtime_kwargs(),
        )
        if runtime_image:
            sandbox.runtime_container_image = runtime_image

        traj_path = os.path.join(tempfile.gettempdir(), f"traj_{self.task_short_name}.json")
        config = OpenHandsConfig(
            run_as_openhands=False,
            max_iterations=100,
            save_trajectory_path=traj_path,
            workspace_mount_path=mount_path,
            workspace_mount_path_in_sandbox="/outputs",
            sandbox=sandbox,
        )
        config.set_llm_config(self.llm_config)
        runtime = create_runtime(config=config)
        try:
            call_async_from_sync(runtime.connect)
        except Exception as e:
            raise _format_runtime_start_failure(runtime, e)
        self._runtime = runtime
        self.config = config
        self._mount_path = mount_path

    def stop(self):
        if self._runtime:
            try:
                self._runtime.close()
            except Exception:
                pass

    def run_command(self, command, timeout=300) -> CommandResult:
        if not self._runtime:
            return CommandResult(exit_code=1, content="Runtime not started")
        try:
            container = getattr(self._runtime, 'container', None)
            if container:
                exec_result = container.exec_run(
                    cmd=["/bin/bash", "-c", command],
                    demux=True,
                    workdir="/workspace",
                )
                exit_code = exec_result.exit_code
                stdout = (exec_result.output[0] or b"").decode("utf-8", errors="replace")
                stderr = (exec_result.output[1] or b"").decode("utf-8", errors="replace")
                content = stdout + stderr
                return CommandResult(exit_code=exit_code, content=content)
        except Exception:
            pass
        from openhands.events.action import CmdRunAction
        action = CmdRunAction(command=command)
        action.set_hard_timeout(timeout)
        try:
            obs = self._runtime.run(action)
        except Exception as e:
            return CommandResult(exit_code=-1, content=str(e))
        return CommandResult(exit_code=getattr(obs, 'exit_code', -1),
                             content=getattr(obs, 'content', ''))

    def run_agent(self, instruction, max_iterations=100, **kwargs):
        import asyncio
        from openhands.events.action import MessageAction
        from openhands.core.main import run_controller

        self.config.max_iterations = max_iterations

        def _fake_user_response(state):
            if state and state.history:
                user_msgs = [e for e in state.history
                             if hasattr(e, 'source') and e.source == 'user']
                if len(user_msgs) >= 2:
                    return ("Please continue working on the task. "
                            "If you want to give up, run: <execute_bash> exit </execute_bash>.\n")
            return ("Please continue working on the task on whatever approach "
                    "you think is suitable.\n"
                    "IMPORTANT: YOU SHOULD NEVER ASK FOR HUMAN HELP.\n")

        state = asyncio.run(run_controller(
            config=self.config,
            initial_user_action=MessageAction(content=instruction),
            runtime=self._runtime,
            fake_user_response_fn=_fake_user_response,
        ))

        if self.verbose and state and state.history:
            for event in state.history:
                try:
                    _print_event(event)
                except Exception:
                    pass

        self._state = state

        if self.config.save_trajectory_path and os.path.exists(self.config.save_trajectory_path):
            if self._mount_path:
                dest = os.path.join(self._mount_path, f"traj_{self.task_short_name}.json")
                try:
                    shutil.copy2(self.config.save_trajectory_path, dest)
                except PermissionError:
                    subprocess.run(["sudo", "cp", self.config.save_trajectory_path, dest], check=True)

        return state
