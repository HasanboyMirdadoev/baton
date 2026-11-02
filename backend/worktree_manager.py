from __future__ import annotations

import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import tempfile
import time
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple, Union

from backend.config import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    FORBIDDEN_PORTS,
    AppConfig,
    load_config,
    validate_working_dir,
)
from backend.models import ConflictError, UnprocessableError, ValidationError

PORT_RANGE_START: int = 18100
PORT_RANGE_END: int = 18999
MAX_DIFF_BYTES: int = 512 * 1024  # 512 KiB

NOT_GIT_REPO_MESSAGE: str = (
    "Target folder is not a git repository — run `git init` and commit first"
)


class WorkspaceResult(Dict[str, Any]):
    """Dictionary result that also supports attribute access (`res.worktree_path`)."""

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError:
            raise AttributeError(
                f"'{type(self).__name__}' object has no attribute '{name}'"
            )


def _run_git(
    args: List[str],
    cwd: Optional[Union[str, Path]] = None,
    timeout: float = 30.0,
    env: Optional[Dict[str, str]] = None,
) -> subprocess.CompletedProcess:  # type: ignore[type-arg]
    cmd = ["git"]
    if cwd is not None:
        cmd.extend(["-C", str(cwd)])
    cmd.extend(args)
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=env,
    )


def allocate_safe_port(
    exclude_ports: Optional[Union[Iterable[int], AppConfig, Any]] = None,
    *,
    start_port: int = PORT_RANGE_START,
    end_port: int = PORT_RANGE_END,
    host: str = DEFAULT_HOST,
    app_port: Optional[int] = None,
) -> int:
    """
    Scans `18100..18999` (by default), binds a temporary TCP socket on `127.0.0.1`
    to confirm the port is free, and excludes `FORBIDDEN_PORTS`, Baton's server port,
    and any caller-supplied `exclude_ports`.
    """
    excluded: Set[int] = set(FORBIDDEN_PORTS)
    excluded.add(DEFAULT_PORT)

    env_port_raw = os.environ.get("AGENT_RELAY_PORT")
    if env_port_raw:
        try:
            excluded.add(int(env_port_raw))
        except ValueError:
            pass

    if app_port is not None:
        excluded.add(int(app_port))

    if exclude_ports is not None:
        if isinstance(exclude_ports, AppConfig):
            excluded.add(int(exclude_ports.port))
        elif isinstance(exclude_ports, int):
            excluded.add(int(exclude_ports))
        elif hasattr(exclude_ports, "__iter__"):
            for p in exclude_ports:
                if p is not None:
                    try:
                        excluded.add(int(p))
                    except (TypeError, ValueError):
                        pass

    for candidate in range(start_port, end_port + 1):
        if candidate in excluded:
            continue
        if not _port_is_free(candidate, host):
            continue
        # A bind probe misses listeners on other address families (e.g. Node on `::`),
        # so also ask lsof whether anything is listening on the port.
        if _list_listening_pids(candidate, include_self=True):
            continue
        return candidate

    raise RuntimeError(
        f"No free safe port available in range {start_port}..{end_port}"
    )


def _port_is_free(port: int, host: str = DEFAULT_HOST) -> bool:
    """
    True when nothing is bound to `port`. Binds the wildcard address WITHOUT SO_REUSEADDR:
    on macOS, SO_REUSEADDR lets a 127.0.0.1 bind succeed even while another process
    listens on 0.0.0.0, which would hand out a port that is already in use.
    """
    hosts = ["0.0.0.0"]
    if host and host not in hosts:
        hosts.append(host)
    for h in hosts:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind((h, port))
            except OSError:
                return False
    return True


def prepare_workspace(
    run_id: Union[str, AppConfig],
    target_repo: Optional[Union[str, Path]] = None,
    data_dir: Optional[Union[str, Path, AppConfig]] = None,
    *,
    config: Optional[AppConfig] = None,
) -> WorkspaceResult:
    """
    Validates `target_repo` is an existing directory and an initialized Git repository
    with at least one commit (raises `UnprocessableError` -> HTTP 422 otherwise).
    Creates an isolated git worktree at `<data_dir>/worktrees/<run_id>` on branch
    `baton/<run_id>`, creates `.baton/screenshots/`, and adds `.baton/` to the
    repository's git `info/exclude`.
    """
    if isinstance(run_id, AppConfig):
        # Handle (config, run_id, target_repo) call order if used
        cfg_arg = run_id
        if target_repo is None or data_dir is None:
            raise ValueError("run_id and target_repo are required")
        actual_run_id = str(target_repo)
        actual_target_repo = str(data_dir)
        resolved_data_dir = cfg_arg.data_dir
    else:
        actual_run_id = str(run_id).strip()
        if not actual_run_id:
            raise ValueError("run_id cannot be empty")
        if target_repo is None:
            raise ValueError("target_repo is required")
        actual_target_repo = str(target_repo)
        if isinstance(data_dir, AppConfig):
            resolved_data_dir = data_dir.data_dir
        elif data_dir is not None:
            resolved_data_dir = Path(os.path.expanduser(str(data_dir))).resolve()
        elif config is not None:
            resolved_data_dir = config.data_dir
        else:
            resolved_data_dir = load_config().data_dir

    validated_repo = validate_working_dir(actual_target_repo)

    # Check if inside a git work tree
    inside_res = _run_git(["rev-parse", "--is-inside-work-tree"], cwd=validated_repo)
    if inside_res.returncode != 0 or inside_res.stdout.strip().lower() != "true":
        raise UnprocessableError(NOT_GIT_REPO_MESSAGE)

    # Check if the repository has at least one commit (HEAD exists)
    head_res = _run_git(["rev-parse", "--verify", "HEAD"], cwd=validated_repo)
    if head_res.returncode != 0 or not head_res.stdout.strip():
        raise UnprocessableError(NOT_GIT_REPO_MESSAGE)

    base_commit = head_res.stdout.strip()

    # Subfolder of the repository the user picked (e.g. "services/api/"); agents work
    # in the same subfolder of the worktree, not at the repository root.
    prefix_res = _run_git(["rev-parse", "--show-prefix"], cwd=validated_repo)
    work_subdir = (
        prefix_res.stdout.strip().strip("/") if prefix_res.returncode == 0 else ""
    )

    # Determine base_branch (fallback to "HEAD" if detached)
    branch_res = _run_git(
        ["symbolic-ref", "--short", "-q", "HEAD"], cwd=validated_repo
    )
    if branch_res.returncode == 0 and branch_res.stdout.strip():
        base_branch = branch_res.stdout.strip()
    else:
        base_branch = "HEAD"

    branch_name = f"baton/{actual_run_id}"
    worktrees_root = resolved_data_dir / "worktrees"
    worktrees_root.mkdir(parents=True, exist_ok=True)
    worktree_path = worktrees_root / actual_run_id

    # Clean up any stale worktree or branch with the same run_id
    if worktree_path.exists():
        _run_git(
            ["worktree", "remove", "--force", str(worktree_path)],
            cwd=validated_repo,
        )
        if worktree_path.exists():
            shutil.rmtree(worktree_path, ignore_errors=True)
    _run_git(["worktree", "prune"], cwd=validated_repo)

    existing_branch = _run_git(
        ["show-ref", "--verify", "--quiet", f"refs/heads/{branch_name}"],
        cwd=validated_repo,
    )
    if existing_branch.returncode == 0:
        _run_git(["branch", "-D", branch_name], cwd=validated_repo)

    # Create isolated worktree on branch baton/<run_id> at base_commit
    wt_res = _run_git(
        ["worktree", "add", "-b", branch_name, str(worktree_path), base_commit],
        cwd=validated_repo,
    )
    if wt_res.returncode != 0:
        err_detail = wt_res.stderr.strip() or wt_res.stdout.strip()
        raise UnprocessableError(
            f"Failed to create git worktree for branch '{branch_name}': {err_detail}"
        )

    work_dir = worktree_path / work_subdir if work_subdir else worktree_path
    work_dir.mkdir(parents=True, exist_ok=True)

    # Create .baton/ and .baton/screenshots/ inside the agents' working folder
    baton_dir = work_dir / ".baton"
    screenshots_dir = baton_dir / "screenshots"
    screenshots_dir.mkdir(parents=True, exist_ok=True)

    # Exclude .baton/ (at any depth) from git status/diff/commit via info/exclude
    _ensure_baton_excluded(worktree_path)

    return WorkspaceResult(
        {
            "target_repo": validated_repo,
            "is_git_repo": True,
            "base_branch": base_branch,
            "base_commit": base_commit,
            "worktree_path": str(worktree_path),
            "work_subdir": work_subdir,
            "work_dir": str(work_dir),
            "branch_name": branch_name,
        }
    )


def _ensure_baton_excluded(worktree_path: Path) -> None:
    excl_res = _run_git(
        ["rev-parse", "--git-path", "info/exclude"], cwd=worktree_path
    )
    if excl_res.returncode != 0 or not excl_res.stdout.strip():
        return

    raw_excl = excl_res.stdout.strip()
    excl_path = (
        Path(raw_excl)
        if os.path.isabs(raw_excl)
        else (worktree_path / raw_excl).resolve()
    )
    try:
        excl_path.parent.mkdir(parents=True, exist_ok=True)
        existing = (
            excl_path.read_text(encoding="utf-8", errors="replace")
            if excl_path.exists()
            else ""
        )
        lines = {ln.strip() for ln in existing.splitlines()}
        if ".baton/" not in lines and ".baton" not in lines:
            prefix = "" if (not existing or existing.endswith("\n")) else "\n"
            with open(excl_path, "a", encoding="utf-8") as f:
                f.write(f"{prefix}.baton/\n")
    except OSError:
        pass


def _is_baton_artifact_path(path_str: str) -> bool:
    cleaned = path_str.strip().lstrip("./")
    return cleaned == ".baton" or cleaned.startswith(".baton/")


def _collect_diff(
    wt_path: Path, effective_base: str, env: Optional[Dict[str, str]]
) -> Tuple[List[Dict[str, Any]], str, str]:
    # Make sure .baton is never intent-to-added or staged
    _run_git(["reset", "-q", "--", ".baton"], cwd=wt_path, env=env)

    # 1. Intent-to-add untracked project files (excluding .baton)
    _run_git(["add", "-N", "--", ".", ":!.baton"], cwd=wt_path, env=env)

    # 2. Full unified diff against base_commit (capped at 512 KiB)
    diff_res = _run_git(
        ["diff", effective_base, "--", ".", ":!.baton"], cwd=wt_path, env=env
    )
    diff_text = diff_res.stdout if diff_res.returncode == 0 else ""
    diff_bytes = diff_text.encode("utf-8", errors="replace")
    if len(diff_bytes) > MAX_DIFF_BYTES:
        diff_text = (
            diff_bytes[:MAX_DIFF_BYTES].decode("utf-8", errors="ignore")
            + "\n... [diff truncated at 512 KiB]\n"
        )

    # 3. Diff --stat summary text
    stat_res = _run_git(
        ["diff", "--stat", effective_base, "--", ".", ":!.baton"], cwd=wt_path, env=env
    )
    stat_text = stat_res.stdout.strip() if stat_res.returncode == 0 else ""

    # 4. File status codes (A, M, D, R) from diff --name-status and status --porcelain
    status_by_path: Dict[str, str] = {}
    name_status_res = _run_git(
        ["diff", "--name-status", effective_base, "--", ".", ":!.baton"],
        cwd=wt_path,
        env=env,
    )
    if name_status_res.returncode == 0:
        for raw_line in name_status_res.stdout.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            parts = line.split("\t")
            if len(parts) >= 2:
                raw_status = parts[0].strip().upper()
                file_path = parts[-1].strip()
                if not _is_baton_artifact_path(file_path):
                    status_by_path[file_path] = raw_status[0] if raw_status else "M"

    porcelain_res = _run_git(
        ["status", "--porcelain", "--", ".", ":!.baton"], cwd=wt_path, env=env
    )
    if porcelain_res.returncode == 0:
        for raw_line in porcelain_res.stdout.splitlines():
            if len(raw_line) < 4:
                continue
            xy = raw_line[:2]
            file_path = raw_line[3:].strip()
            if " -> " in file_path:
                file_path = file_path.split(" -> ", 1)[1].strip()
            if file_path.startswith('"') and file_path.endswith('"'):
                file_path = file_path[1:-1]
            if _is_baton_artifact_path(file_path):
                continue
            if file_path not in status_by_path:
                if "?" in xy or "A" in xy:
                    status_by_path[file_path] = "A"
                elif "D" in xy:
                    status_by_path[file_path] = "D"
                elif "R" in xy:
                    status_by_path[file_path] = "R"
                else:
                    status_by_path[file_path] = "M"

    # 5. Structured additions/deletions per file via diff --numstat
    numstat_by_path: Dict[str, Tuple[int, int]] = {}
    ordered_paths: List[str] = []
    numstat_res = _run_git(
        ["diff", "--numstat", effective_base, "--", ".", ":!.baton"], cwd=wt_path, env=env
    )
    if numstat_res.returncode == 0:
        for raw_line in numstat_res.stdout.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            add_str, del_str, file_path = parts[0], parts[1], parts[2].strip()
            if _is_baton_artifact_path(file_path):
                continue
            additions = int(add_str) if add_str.isdigit() else 0
            deletions = int(del_str) if del_str.isdigit() else 0
            numstat_by_path[file_path] = (additions, deletions)
            if file_path not in ordered_paths:
                ordered_paths.append(file_path)

    for file_path in status_by_path:
        if file_path not in ordered_paths:
            ordered_paths.append(file_path)

    files_list: List[Dict[str, Any]] = []
    for file_path in ordered_paths:
        if _is_baton_artifact_path(file_path):
            continue
        additions, deletions = numstat_by_path.get(file_path, (0, 0))
        st_code = status_by_path.get(file_path, "M")
        files_list.append(
            {
                "path": file_path,
                "status": st_code,
                "additions": additions,
                "deletions": deletions,
            }
        )

    return files_list, diff_text, stat_text


def get_workspace_diff(
    worktree_path: Union[str, Path, Dict[str, Any]],
    base_commit: Optional[str] = None,
    *,
    run_id: Optional[str] = None,
    base_branch: Optional[str] = None,
    branch_name: Optional[str] = None,
) -> WorkspaceResult:
    """
    Computes the current workspace diff in `worktree_path` against `base_commit`,
    excluding `.baton/`.
    Marks untracked project files with `git add -N` so new files appear in `git diff`
    without staging their contents.
    """
    if isinstance(worktree_path, dict):
        run_dict = worktree_path
        wt_str = str(run_dict.get("worktree_path") or "")
        base_commit = base_commit or str(run_dict.get("base_commit") or "HEAD")
        run_id = run_id if run_id is not None else run_dict.get("id")
        base_branch = (
            base_branch if base_branch is not None else run_dict.get("base_branch")
        )
        branch_name = (
            branch_name if branch_name is not None else run_dict.get("branch_name")
        )
    else:
        wt_str = str(worktree_path)

    effective_base = (base_commit or "").strip() or "HEAD"
    wt_path = Path(wt_str)

    result_data: Dict[str, Any] = {
        "files": [],
        "diff_text": "",
        "stat_text": "",
    }
    if run_id is not None:
        result_data["run_id"] = run_id
    if base_branch is not None:
        result_data["base_branch"] = base_branch
    if base_commit is not None:
        result_data["base_commit"] = base_commit
    if branch_name is not None:
        result_data["branch_name"] = branch_name

    if not wt_str or not wt_path.exists() or not wt_path.is_dir():
        return WorkspaceResult(result_data)

    _ensure_baton_excluded(wt_path)

    # Work on a private copy of the index: `git add -N` must not touch the worktree's
    # real index, which an agent may be using concurrently (index.lock contention).
    tmp_index_dir = tempfile.mkdtemp(prefix="baton-diff-index-")
    try:
        git_env = dict(os.environ)
        idx_res = _run_git(["rev-parse", "--git-path", "index"], cwd=wt_path)
        tmp_index = Path(tmp_index_dir) / "index"
        if idx_res.returncode == 0 and idx_res.stdout.strip():
            raw_idx = idx_res.stdout.strip()
            real_index = (
                Path(raw_idx) if os.path.isabs(raw_idx) else (wt_path / raw_idx)
            )
            if real_index.is_file():
                shutil.copyfile(real_index, tmp_index)
        git_env["GIT_INDEX_FILE"] = str(tmp_index)
        files_list, diff_text, stat_text = _collect_diff(wt_path, effective_base, git_env)
    finally:
        shutil.rmtree(tmp_index_dir, ignore_errors=True)

    result_data["files"] = files_list
    result_data["diff_text"] = diff_text
    result_data["stat_text"] = stat_text
    return WorkspaceResult(result_data)


def finalize_workspace(
    run: Optional[Dict[str, Any]] = None,
    action: Optional[str] = None,
    commit_message: Optional[str] = None,
    *,
    run_id: Optional[str] = None,
    target_repo: Optional[Union[str, Path]] = None,
    worktree_path: Optional[Union[str, Path]] = None,
    branch_name: Optional[str] = None,
    base_branch: Optional[str] = None,
) -> WorkspaceResult:
    """
    Finalizes the team run workspace according to user sign-off `action`:
      - `"merge"`: verifies `target_repo` is clean and on `base_branch`, commits
        project changes in `worktree_path`, removes worktree, merges `baton/<run_id>`
        with `--no-ff` into `target_repo`, and deletes `baton/<run_id>`.
      - `"keep_branch"`: commits project changes in `worktree_path` onto `baton/<run_id>`
        and removes the worktree directory while leaving `baton/<run_id>` in `target_repo`.
      - `"discard"`: force-removes `worktree_path` and deletes `baton/<run_id>`.
    """
    run_dict: Dict[str, Any] = run if isinstance(run, dict) else {}

    eff_action = (action or run_dict.get("action") or "").strip().lower()
    if eff_action not in ("merge", "keep_branch", "discard"):
        raise UnprocessableError(
            f"Invalid sign-off action: {action!r}. Must be 'merge', 'keep_branch', or 'discard'."
        )

    eff_run_id = str(run_id or run_dict.get("id") or run_dict.get("run_id") or "")
    eff_target_repo = str(target_repo or run_dict.get("target_repo") or "")
    eff_worktree_path = str(worktree_path or run_dict.get("worktree_path") or "")
    eff_branch_name = str(
        branch_name
        or run_dict.get("branch_name")
        or (f"baton/{eff_run_id}" if eff_run_id else "")
    )
    eff_base_branch = str(base_branch or run_dict.get("base_branch") or "HEAD")
    eff_work_subdir = str(run_dict.get("work_subdir") or "").strip().strip("/")

    if not eff_target_repo:
        raise ValidationError("target_repo is required to finalize workspace")

    task_prompt = str(run_dict.get("task_prompt") or "").strip()
    if commit_message and commit_message.strip():
        eff_commit_msg = commit_message.strip()
    elif task_prompt:
        first_line = task_prompt.splitlines()[0][:60].strip()
        eff_commit_msg = f"Baton team run: {first_line}"
    else:
        eff_commit_msg = f"Baton team run {eff_run_id or eff_branch_name}"

    wt_path = Path(eff_worktree_path) if eff_worktree_path else None
    work_dir = (wt_path / eff_work_subdir) if (wt_path and eff_work_subdir) else wt_path

    if eff_action == "merge":
        # 0. A run started from a detached HEAD has no branch to merge into; merging into
        #    whatever is checked out now (and deleting baton/<run_id>) could strand the work.
        if not eff_base_branch or eff_base_branch == "HEAD":
            raise ConflictError(
                "This run was started from a detached HEAD, so there is no base branch to "
                f"merge into. Choose 'Keep Branch' and merge '{eff_branch_name}' yourself."
            )

        # 1. Check if target_repo has uncommitted changes to tracked files
        dirty_res = _run_git(
            ["status", "--porcelain", "--untracked-files=no"],
            cwd=eff_target_repo,
        )
        if dirty_res.returncode != 0:
            raise ConflictError(
                f"Unable to inspect target repository status: {dirty_res.stderr.strip()}"
            )
        if dirty_res.stdout.strip():
            raise ConflictError(
                "Target repository has uncommitted changes in the working tree. "
                "Please commit or stash them before merging, or choose 'Keep Branch'."
            )

        # 2. Check that target_repo is currently on base_branch
        curr_branch_res = _run_git(
            ["symbolic-ref", "--short", "-q", "HEAD"],
            cwd=eff_target_repo,
        )
        curr_branch = (
            curr_branch_res.stdout.strip() if curr_branch_res.returncode == 0 else "HEAD"
        )
        if curr_branch != eff_base_branch:
            raise ConflictError(
                f"Target repository is currently on branch '{curr_branch}', "
                f"expected '{eff_base_branch}'. Please checkout '{eff_base_branch}' "
                "before merging, or choose 'Keep Branch'."
            )

        # 3. In worktree_path, remove .baton, stage all project changes, and commit onto baton/<run_id>
        if wt_path is not None and wt_path.exists():
            _commit_worktree_changes(wt_path, eff_commit_msg, work_dir)
            _remove_worktree(eff_target_repo, wt_path)

        # 4. Merge baton/<run_id> into target_repo and delete temporary branch
        merge_res = _run_git(
            [
                "-c",
                "user.name=Baton",
                "-c",
                "user.email=baton@localhost",
                "merge",
                "--no-ff",
                eff_branch_name,
                "-m",
                f"Merge {eff_branch_name}",
            ],
            cwd=eff_target_repo,
        )
        if merge_res.returncode != 0:
            _run_git(["merge", "--abort"], cwd=eff_target_repo)
            err_detail = merge_res.stderr.strip() or merge_res.stdout.strip()
            raise ConflictError(
                f"Failed to merge branch '{eff_branch_name}' into '{eff_base_branch}': {err_detail}"
            )

        del_res = _run_git(["branch", "-d", eff_branch_name], cwd=eff_target_repo)
        if del_res.returncode != 0:
            _run_git(["branch", "-D", eff_branch_name], cwd=eff_target_repo)

        resolution = "merged"

    elif eff_action == "keep_branch":
        if wt_path is not None and wt_path.exists():
            _commit_worktree_changes(wt_path, eff_commit_msg, work_dir)
            _remove_worktree(eff_target_repo, wt_path)

        resolution = "kept_branch"

    else:  # eff_action == "discard"
        if wt_path is not None:
            _remove_worktree(eff_target_repo, wt_path)
        _run_git(["worktree", "prune"], cwd=eff_target_repo)
        if eff_branch_name:
            _run_git(["branch", "-D", eff_branch_name], cwd=eff_target_repo)

        resolution = "discarded"

    return WorkspaceResult(
        {
            "action": eff_action,
            "resolution": resolution,
            "branch_name": eff_branch_name,
            "target_repo": eff_target_repo,
            "worktree_path": eff_worktree_path,
        }
    )


def _commit_worktree_changes(
    wt_path: Path, commit_message: str, work_dir: Optional[Path] = None
) -> None:
    """
    Commits every project change in the worktree onto its `baton/<run_id>` branch.
    Raises ConflictError when anything fails, so callers never remove a worktree whose
    changes were not committed (that would silently discard the team's work).
    """
    _ensure_baton_excluded(wt_path)
    _run_git(["reset", "-q", "--", ".baton"], cwd=wt_path)
    shutil.rmtree(wt_path / ".baton", ignore_errors=True)
    if work_dir is not None and Path(work_dir) != wt_path:
        shutil.rmtree(Path(work_dir) / ".baton", ignore_errors=True)

    add_res = _run_git(["add", "-A", "--", ".", ":!.baton"], cwd=wt_path)
    if add_res.returncode != 0:
        raise ConflictError(
            "Could not stage the team's changes for commit: "
            f"{add_res.stderr.strip() or add_res.stdout.strip()}"
        )
    # Baton's own commit on its temporary branch: skip the repo's commit hooks and
    # signing, which would otherwise fail non-interactively (pre-commit, gpg agent).
    commit_res = _run_git(
        [
            "-c",
            "user.name=Baton",
            "-c",
            "user.email=baton@localhost",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "--no-verify",
            "--allow-empty",
            "-m",
            commit_message,
        ],
        cwd=wt_path,
    )
    if commit_res.returncode != 0:
        raise ConflictError(
            "Could not commit the team's changes: "
            f"{commit_res.stderr.strip() or commit_res.stdout.strip()}"
        )
    status_res = _run_git(["status", "--porcelain", "--", ".", ":!.baton"], cwd=wt_path)
    if status_res.returncode != 0 or status_res.stdout.strip():
        raise ConflictError(
            "The worktree still has uncommitted changes after committing; "
            "it was left in place so no work is lost."
        )


def _remove_worktree(target_repo: str, wt_path: Path) -> None:
    if wt_path.exists():
        _run_git(
            ["worktree", "remove", "--force", str(wt_path)],
            cwd=target_repo,
        )
        if wt_path.exists():
            shutil.rmtree(wt_path, ignore_errors=True)
    _run_git(["worktree", "prune"], cwd=target_repo)


def _lsof_bin() -> str:
    if os.path.exists("/usr/sbin/lsof"):
        return "/usr/sbin/lsof"
    return shutil.which("lsof") or "/usr/sbin/lsof"


def _list_listening_pids(port: int, include_self: bool = False) -> List[int]:
    """PIDs with a TCP socket in LISTEN state on `port` (clients connected to it are ignored)."""
    try:
        res = subprocess.run(
            [_lsof_bin(), "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5.0,
        )
    except (OSError, subprocess.SubprocessError):
        return []

    if res.returncode != 0 or not res.stdout.strip():
        return []

    my_pid = os.getpid()
    pids: List[int] = []
    for line in res.stdout.splitlines():
        s = line.strip()
        if s.isdigit():
            pid = int(s)
            if (include_self or pid != my_pid) and pid > 1 and pid not in pids:
                pids.append(pid)
    return pids


def _process_cwd(pid: int) -> Optional[str]:
    """Best-effort current working directory of `pid` (Linux /proc, else lsof)."""
    proc_link = f"/proc/{pid}/cwd"
    if os.path.exists(proc_link):
        try:
            return os.path.realpath(proc_link)
        except OSError:
            return None
    try:
        res = subprocess.run(
            [_lsof_bin(), "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5.0,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    for line in res.stdout.splitlines():
        if line.startswith("n") and len(line) > 1:
            return os.path.realpath(line[1:])
    return None


def _is_inside(path: Optional[str], root: str) -> bool:
    if not path:
        return False
    root_real = os.path.realpath(root)
    return path == root_real or path.startswith(root_real.rstrip(os.sep) + os.sep)


def _signal_pid_or_pgid(pid: int, sig: signal.Signals) -> None:
    my_pid = os.getpid()
    if pid == my_pid or pid <= 1:
        return
    try:
        my_pgid = os.getpgid(my_pid)
    except OSError:
        my_pgid = -1

    try:
        pgid = os.getpgid(pid)
        if pgid > 1 and pgid != my_pgid:
            os.killpg(pgid, sig)
            return
    except (ProcessLookupError, PermissionError, OSError):
        pass

    try:
        os.kill(pid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def cleanup_port_processes(
    port_or_worktree: Union[int, str, Path, None] = None,
    allocated_port: Optional[int] = None,
    *,
    port: Optional[int] = None,
    worktree_path: Optional[Union[str, Path]] = None,
    wait_seconds: float = 2.0,
) -> List[int]:
    """
    Stops processes LISTENING on `allocated_port` that were started from inside the run's
    worktree (their working directory is inside `worktree_path`). Sends SIGTERM to their
    process groups, waits up to `wait_seconds`, then SIGKILLs survivors.
    Clients connected to the port (e.g. the user's browser) and listeners started
    elsewhere (an unrelated server of the user's) are never signalled.
    Accepts `cleanup_port_processes(worktree_path, port)`; with no worktree nothing is killed.
    """
    target_port: Optional[int] = None
    if allocated_port is not None:
        target_port = int(allocated_port)
    elif port is not None:
        target_port = int(port)
    elif isinstance(port_or_worktree, int):
        target_port = int(port_or_worktree)
    elif isinstance(port_or_worktree, str) and port_or_worktree.strip().isdigit():
        target_port = int(port_or_worktree.strip())

    eff_worktree: Optional[str] = None
    if worktree_path:
        eff_worktree = str(worktree_path)
    elif isinstance(port_or_worktree, (str, Path)) and not (
        isinstance(port_or_worktree, str) and port_or_worktree.strip().isdigit()
    ):
        eff_worktree = str(port_or_worktree) if str(port_or_worktree).strip() else None

    if target_port is None or target_port <= 0 or not eff_worktree:
        return []

    # Never touch forbidden ports or Baton's own server port
    if target_port in FORBIDDEN_PORTS or target_port == DEFAULT_PORT:
        return []
    env_port_raw = os.environ.get("AGENT_RELAY_PORT")
    if env_port_raw and env_port_raw.strip().isdigit():
        if target_port == int(env_port_raw.strip()):
            return []

    def _owned_listeners() -> List[int]:
        return [
            pid
            for pid in _list_listening_pids(target_port)
            if _is_inside(_process_cwd(pid), eff_worktree)  # type: ignore[arg-type]
        ]

    pids = _owned_listeners()
    if not pids:
        return []

    for pid in pids:
        _signal_pid_or_pgid(pid, signal.SIGTERM)

    deadline = time.monotonic() + max(0.0, float(wait_seconds))
    remaining = [p for p in _list_listening_pids(target_port) if p in pids]
    while remaining and time.monotonic() < deadline:
        time.sleep(0.05)
        remaining = [p for p in _list_listening_pids(target_port) if p in pids]

    if remaining:
        for pid in remaining:
            _signal_pid_or_pgid(pid, signal.SIGKILL)

    return pids


_cleanup_checker_processes = cleanup_port_processes


class WorktreeManager:
    """
    Object-oriented wrapper around the module-level worktree, diff, and port helpers.
    """

    def __init__(self, config: Optional[AppConfig] = None) -> None:
        self.config = config

    def allocate_safe_port(
        self,
        exclude_ports: Optional[Iterable[int]] = None,
        *,
        start_port: int = PORT_RANGE_START,
        end_port: int = PORT_RANGE_END,
        host: str = DEFAULT_HOST,
    ) -> int:
        app_port = self.config.port if self.config is not None else None
        return allocate_safe_port(
            exclude_ports,
            start_port=start_port,
            end_port=end_port,
            host=host,
            app_port=app_port,
        )

    def prepare_workspace(
        self,
        run_id: str,
        target_repo: Union[str, Path],
        data_dir: Optional[Union[str, Path]] = None,
    ) -> WorkspaceResult:
        return prepare_workspace(
            run_id=run_id,
            target_repo=target_repo,
            data_dir=data_dir,
            config=self.config,
        )

    def get_workspace_diff(
        self,
        worktree_path: Union[str, Path, Dict[str, Any]],
        base_commit: Optional[str] = None,
        *,
        run_id: Optional[str] = None,
        base_branch: Optional[str] = None,
        branch_name: Optional[str] = None,
    ) -> WorkspaceResult:
        return get_workspace_diff(
            worktree_path=worktree_path,
            base_commit=base_commit,
            run_id=run_id,
            base_branch=base_branch,
            branch_name=branch_name,
        )

    def finalize_workspace(
        self,
        run: Optional[Dict[str, Any]] = None,
        action: Optional[str] = None,
        commit_message: Optional[str] = None,
        *,
        run_id: Optional[str] = None,
        target_repo: Optional[Union[str, Path]] = None,
        worktree_path: Optional[Union[str, Path]] = None,
        branch_name: Optional[str] = None,
        base_branch: Optional[str] = None,
    ) -> WorkspaceResult:
        return finalize_workspace(
            run=run,
            action=action,
            commit_message=commit_message,
            run_id=run_id,
            target_repo=target_repo,
            worktree_path=worktree_path,
            branch_name=branch_name,
            base_branch=base_branch,
        )

    def cleanup_port_processes(
        self,
        port_or_worktree: Union[int, str, Path, None] = None,
        allocated_port: Optional[int] = None,
        *,
        port: Optional[int] = None,
        worktree_path: Optional[Union[str, Path]] = None,
        wait_seconds: float = 2.0,
    ) -> List[int]:
        return cleanup_port_processes(
            port_or_worktree,
            allocated_port,
            port=port,
            worktree_path=worktree_path,
            wait_seconds=wait_seconds,
        )
