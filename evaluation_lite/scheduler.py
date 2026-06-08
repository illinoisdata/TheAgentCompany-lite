#!/usr/bin/env python3
"""Smart parallel scheduler for TheAgentCompany benchmark evaluation.

Groups tasks by service dependency, runs non-conflicting groups in parallel.
With --num-instances N, distributes large groups across multiple service instances.
With --mock, simulates task execution without LLM API calls (for infra testing).
"""
import argparse, json, os, subprocess, sys, time, urllib.request, yaml
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
_TASKS_DIR_CANDIDATES = [
    SCRIPT_DIR.parent / "workspaces" / "tasks",
    SCRIPT_DIR.parent / "TheAgentCompany" / "workspaces" / "tasks",
]
TASKS_DIR = next((p for p in _TASKS_DIR_CANDIDATES if p.exists()), _TASKS_DIR_CANDIDATES[0])


class MockConfig:
    """Immutable mock mode configuration."""
    def __init__(self, enabled=False, duration_range=(10, 30)):
        self.enabled = enabled
        self.duration_range = duration_range
    def __bool__(self):
        return self.enabled


_MOCK_CONFIG = MockConfig()


def load_task_deps(task_dir):
    name = task_dir.name
    dep_file = task_dir / "dependencies.yml"
    if dep_file.exists():
        with open(dep_file) as f:
            deps = yaml.safe_load(f) or []
    else:
        deps = []
    return name, tuple(sorted(deps))


def group_tasks_by_deps(task_names=None):
    groups = defaultdict(list)
    for task_dir in sorted(TASKS_DIR.iterdir()):
        if not task_dir.is_dir():
            continue
        name = task_dir.name
        if task_names and name not in task_names:
            continue
        _, deps = load_task_deps(task_dir)
        groups[deps].append(name)
    return dict(groups)


def find_non_overlapping_groups(groups, max_groups=4):
    """Partition dependency groups into rounds of non-conflicting groups."""
    dep_sets = {k: set(k) for k in groups}
    rounds, assigned = [], {}
    for gk in sorted(groups.keys(), key=lambda g: len(groups[g]), reverse=True):
        my_deps = set(gk)
        placed = False
        for ri, rg in enumerate(rounds):
            if not any(my_deps & dep_sets[ek] for ek in rg):
                rg.append(gk)
                assigned[gk] = ri
                placed = True
                break
        if not placed:
            rounds.append([gk])
            assigned[gk] = len(rounds) - 1
    return rounds


def run_task(task_name, agent_llm_config, env_llm_config, outputs_path,
             server_hostname, script_dir, harness="openhands",
             base_image=None, service_instance=None, mock_enabled=False,
             mock_duration_range=(10, 30)):
    """Run a single task via run_eval.py (or mock), return result dict."""
    task_dir = str(TASKS_DIR / task_name)
    start = time.time()
    tmpdir = os.path.join(outputs_path, f".tmp_{task_name}")
    os.makedirs(tmpdir, exist_ok=True)
    try:
        if mock_enabled:
            lo, hi = mock_duration_range
            cmd = [sys.executable, str(Path(script_dir) / "run_eval_mock.py"),
                   "--task-dir", task_dir, "--outputs-path", outputs_path,
                   "--min-duration", str(lo), "--max-duration", str(hi)]
            if service_instance:
                cmd += ["--service-instance", json.dumps(service_instance)]
        else:
            cmd = [sys.executable,
                   str(Path(script_dir) / "run_eval.py"),
                   "--task-dir", task_dir, "--agent-llm-config", agent_llm_config,
                   "--env-llm-config", env_llm_config, "--outputs-path", outputs_path,
                   "--server-hostname", server_hostname, "--harness", harness]
            if base_image:
                cmd += ["--base-image", base_image]
            if service_instance:
                cmd += ["--service-instance", json.dumps(service_instance)]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=5400,
                                env={**os.environ, "TMPDIR": tmpdir})
        duration = time.time() - start
        success = result.returncode == 0
        if not success:
            err_path = os.path.join(outputs_path, f"eval_{task_name}.json")
            if not os.path.exists(err_path):
                with open(err_path, "w") as f:
                    json.dump({"error": "task_failed", "exit_code": result.returncode,
                               "stderr_tail": result.stderr[-500:] if result.stderr else "",
                               "stdout_tail": result.stdout[-500:] if result.stdout else ""}, f)
        return {"task": task_name, "success": success,
                "duration": round(duration, 1), "exit_code": result.returncode}
    except subprocess.TimeoutExpired:
        duration = time.time() - start
        err_path = os.path.join(outputs_path, f"eval_{task_name}.json")
        if not os.path.exists(err_path):
            with open(err_path, "w") as f:
                json.dump({"error": "timeout"}, f)
        return {"task": task_name, "success": False, "duration": round(duration, 1), "exit_code": -1}
    except Exception as e:
        duration = time.time() - start
        return {"task": task_name, "success": False, "duration": round(duration, 1), "exit_code": -2, "error": str(e)}


def run_group_sequential(group_key, task_names, agent_llm_config, env_llm_config,
                         outputs_path, server_hostname, script_dir, harness="openhands",
                         base_image=None, service_instance=None, mock_enabled=False,
                         mock_duration_range=(10, 30)):
    """Run tasks in a group sequentially (they share services, need reset between)."""
    svc = ", ".join(group_key) if group_key else "(no deps)"
    results = []
    for i, task_name in enumerate(task_names, 1):
        eval_file = os.path.join(outputs_path, f"eval_{task_name}.json")
        if os.path.exists(eval_file):
            with open(eval_file) as f:
                data = json.load(f)
            if "error" not in data:
                score = data.get("final_score", {})
                s = f"{score.get('result', '?')}/{score.get('total', '?')}"
                print(f"  [{svc}] [{i}/{len(task_names)}] {task_name}: SKIP (score={s})")
                results.append({"task": task_name, "success": True, "duration": 0,
                                "exit_code": 0, "skipped": True})
                continue
            else:
                err = data.get("error", "unknown")[:60]
                print(f"  [{svc}] [{i}/{len(task_names)}] {task_name}: RE-RUN (prev error: {err})")

        if service_instance and i > 1 and not mock_enabled:
            _reset_services_for_group(service_instance, group_key)

        print(f"  [{svc}] [{i}/{len(task_names)}] {task_name}: RUNNING...")
        result = run_task(task_name, agent_llm_config, env_llm_config, outputs_path,
                          server_hostname, script_dir, harness, base_image, service_instance,
                          mock_enabled, mock_duration_range)
        status = "OK" if result["success"] else "FAIL"
        print(f"  [{svc}] [{i}/{len(task_names)}] {task_name}: {status} ({result['duration']}s)")
        results.append(result)
    return results


def _reset_services_for_group(instance_info, group_key):
    """Reset services via api-server (inst 0) or docker restart (inst 1+)."""
    inst_id = instance_info["instance_id"] if instance_info else 0
    hostname = instance_info.get("hostname", "localhost") if instance_info else "localhost"
    if inst_id == 0:
        api_port = instance_info.get("api_port", 2999)
        for service in group_key:
            url = f"http://{hostname}:{api_port}/api/reset-{service}"
            try:
                req = urllib.request.Request(url, method="POST", data=b"")
                with urllib.request.urlopen(req, timeout=120) as resp:
                    print(f"    Reset {service}: {resp.read().decode()[:100]}")
            except Exception as e:
                print(f"    Reset {service} failed: {e}")
    else:
        for service in group_key:
            container = f"{service}-{inst_id}"
            try:
                subprocess.run(["docker", "restart", container], capture_output=True, timeout=60)
                print(f"    Reset {service}: {container} restarted")
            except Exception as e:
                print(f"    Reset {service} failed: {e}")

    if "gitlab" in group_key:
        gitlab_port = instance_info.get("gitlab_port", 8929) if instance_info else 8929
        _wait_for_gitlab(hostname, gitlab_port)


def _wait_for_gitlab(hostname, port, timeout=120):
    url = f"http://{hostname}:{port}/"
    deadline = time.time() + timeout
    print(f"    Waiting for gitlab:{port}...", end="", flush=True)
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(urllib.request.Request(url, method="GET"), timeout=10):
                print(" OK")
                return
        except Exception:
            pass
        print(".", end="", flush=True)
        time.sleep(5)
    print(" TIMEOUT")


def _split_groups_across_instances(round_groups, groups, instance_manager_ref):
    """Assign each group to the lightest-loaded matching instance(s)."""
    result = {}
    mgr = instance_manager_ref.get("manager")
    load = {}

    for gk in round_groups:
        task_names = groups[gk]
        svc_label = "+".join(gk) if gk else "no-deps"
        needed = list(gk) if gk else []

        if mgr is None:
            result[(gk, None)] = (task_names, None)
            continue

        matching = [iid for iid in mgr.instances
                    if all(s in mgr.instances[iid].services for s in needed)]
        is_gl_only = needed == ["gitlab"]
        if is_gl_only:
            targets = sorted([i for i in matching if i != 0], key=lambda i: load.get(i, 0))
            targets = targets or sorted(matching, key=lambda i: load.get(i, 0))
        else:
            targets = sorted(matching, key=lambda i: load.get(i, 0))
        if not targets:
            targets = [0]

        if len(targets) <= 1 or len(task_names) <= 1:
            tid = targets[0]
            load[tid] = load.get(tid, 0) + len(task_names)
            result[(gk, tid)] = (task_names, mgr.get_connection_info(tid))
            print(f"  [{svc_label}] {len(task_names)} tasks -> instance {tid}")
        else:
            targets.sort(key=lambda i: load.get(i, 0))
            for idx, tid in enumerate(targets):
                chunk = task_names[idx::len(targets)]
                if not chunk:
                    continue
                load[tid] = load.get(tid, 0) + len(chunk)
                result[(gk, tid)] = (chunk, mgr.get_connection_info(tid))
                print(f"  [{svc_label}] {len(chunk)} tasks -> instance {tid}")

    return result


def _pick_instance_for_group(group_key, instance_manager_ref):
    mgr = instance_manager_ref.get("manager")
    if mgr is None:
        return None
    needed = list(group_key) if group_key else []
    label = "+".join(group_key) if group_key else "no-deps"
    inst_id = mgr.acquire_instance(needed, locked_by=label)
    return mgr.get_connection_info(inst_id)


def _release_instance(instance_manager_ref, instance_info):
    if instance_info is None:
        return
    mgr = instance_manager_ref.get("manager")
    if mgr is None:
        return
    mgr.release_instance(instance_info["instance_id"])


def main():
    parser = argparse.ArgumentParser(description="Smart parallel benchmark scheduler")
    parser.add_argument("--agent-llm-config", required=True)
    parser.add_argument("--env-llm-config", required=True)
    parser.add_argument("--outputs-path", default="outputs")
    parser.add_argument("--server-hostname", default="localhost")
    parser.add_argument("--max-groups", type=int, default=4)
    parser.add_argument("--tasks", type=str, default=None,
                        help="Comma-separated task names to run")
    parser.add_argument("--list-groups", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--harness", type=str, default="openhands",
                        choices=["openhands", "docker"])
    parser.add_argument("--base-image", type=str, default=None)
    parser.add_argument("--num-instances", type=int, default=1)
    parser.add_argument("--full-stack-ids", type=str, default="0",
                        help="Comma-separated instance IDs with all services")
    parser.add_argument("--mock", action="store_true",
                        help="Mock mode (no LLM, simulated durations)")
    parser.add_argument("--mock-duration", type=str, default="10,30",
                        help="Mock duration range min,max")
    parser.add_argument("--tasks-dir", type=str, default=None,
                        help="Override tasks directory (default: auto-detect)")
    args = parser.parse_args()

    if args.tasks_dir:
        global TASKS_DIR
        TASKS_DIR = Path(args.tasks_dir)
    print(f"Tasks directory: {TASKS_DIR} (exists={TASKS_DIR.exists()})")

    if args.mock:
        parts = args.mock_duration.split(",")
        lo, hi = float(parts[0]), float(parts[1]) if len(parts) > 1 else float(parts[0]) + 20
        _MOCK_CONFIG.enabled = True
        _MOCK_CONFIG.duration_range = (lo, hi)
        print(f"MOCK MODE: simulating {lo}-{hi}s per task")

    task_names = None
    if args.tasks:
        task_names = [t.strip() for t in args.tasks.split(",")]
    groups = group_tasks_by_deps(task_names)
    total_tasks = sum(len(v) for v in groups.values())

    instance_manager_ref: dict = {"manager": None}
    full_stack_ids = [0]
    if args.num_instances > 1:
        from service_manager import ServiceManager
        full_stack_ids = [int(x) for x in args.full_stack_ids.split(",") if x]
        mgr = ServiceManager(num_instances=args.num_instances, hostname=args.server_hostname,
                             full_stack_ids=full_stack_ids)
        instance_manager_ref["manager"] = mgr

    print("=" * 60)
    print("TheAgentCompany V2 - Smart Parallel Scheduler")
    print("=" * 60)
    print(f"Total tasks: {total_tasks}, Groups: {len(groups)}")
    if args.num_instances > 1:
        print(f"Instances: {args.num_instances} (full-stack={full_stack_ids})")
    print()

    rounds = find_non_overlapping_groups(groups, args.max_groups)
    print("Execution Plan:")
    for ri, rg in enumerate(rounds, 1):
        parts = [f"{'+'.join(gk) if gk else 'no-deps'}({len(groups[gk])})" for gk in rg]
        print(f"  Round {ri}/{len(rounds)}: {' || '.join(parts)}")
    print()

    if args.list_groups or args.dry_run:
        if args.dry_run:
            print("Dry run - no tasks executed.")
        return

    outputs_path = os.path.abspath(args.outputs_path)
    os.makedirs(outputs_path, exist_ok=True)
    all_results = []
    total_start = time.time()
    completed = 0

    for ri, round_groups in enumerate(rounds, 1):
        print(f"\n{'='*60}\nRound {ri}/{len(rounds)}\n{'='*60}")
        sub_groups = _split_groups_across_instances(round_groups, groups, instance_manager_ref)
        unique_insts = len({iid for _, iid in sub_groups.keys()})
        max_workers = min(unique_insts, len(sub_groups), 16)

        with ProcessPoolExecutor(max_workers=max_workers) as executor:
            futures = {}
            for (gk, inst_id), (batch, info) in sub_groups.items():
                future = executor.submit(run_group_sequential, gk, batch,
                    args.agent_llm_config, args.env_llm_config, outputs_path,
                    args.server_hostname, str(SCRIPT_DIR), args.harness, args.base_image, info,
                    args.mock, _MOCK_CONFIG.duration_range)
                futures[future] = (gk, info)

            for future in as_completed(futures):
                gk, inst_info = futures[future]
                try:
                    results = future.result()
                    all_results.extend(results)
                    completed += len(results)
                    elapsed = time.time() - total_start
                    rate = completed / elapsed * 60 if elapsed > 0 else 0
                    eta = (total_tasks - completed) / rate if rate > 0 else 0
                    print(f"\n  Progress: {completed}/{total_tasks} "
                          f"({completed*100//total_tasks}%) | {rate:.1f} tasks/min | ETA: {eta:.0f} min")
                except Exception as e:
                    print(f"  Group {'+'.join(gk)} failed: {e}")
                finally:
                    _release_instance(instance_manager_ref, inst_info)

    total_duration = time.time() - total_start
    successes = sum(1 for r in all_results if r["success"])
    failures = sum(1 for r in all_results if not r["success"])
    print(f"\n{'='*60}")
    print(f"COMPLETE: {successes} passed, {failures} failed")
    print(f"Total: {total_duration/60:.1f} min ({total_duration:.0f}s)")
    print(f"Results: {outputs_path}")
    with open(os.path.join(outputs_path, "summary.json"), "w") as f:
        json.dump({"total": len(all_results), "passed": successes, "failed": failures,
                   "duration_seconds": round(total_duration, 1),
                   "num_instances": args.num_instances, "results": all_results}, f, indent=2)


if __name__ == "__main__":
    main()
