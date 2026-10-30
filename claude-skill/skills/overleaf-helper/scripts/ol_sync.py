"""Local working copy of an Overleaf project: a git repo whose branch overleaf holds exact snapshots
of the project, one commit per pull, and whose branch main holds the local edits.

    <folder>/.overleaf/project.json   project ID and name, history version of the last pull

Snapshots are built in a temporary index, so they never touch the files in the working copy.
"""
import datetime
import io
import json
import os
import subprocess
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

import ol_api

STATE = Path(".overleaf") / "project.json"
SNAPSHOTS = "overleaf"
WORK = "main"
SNAPSHOT_IDENTITY = {"GIT_AUTHOR_NAME": "Overleaf", "GIT_AUTHOR_EMAIL": "overleaf@localhost",
                     "GIT_COMMITTER_NAME": "overleaf-helper",
                     "GIT_COMMITTER_EMAIL": "overleaf-helper@localhost"}
TOOL_IDENTITY = {"GIT_AUTHOR_NAME": "overleaf-helper", "GIT_AUTHOR_EMAIL": "overleaf-helper@localhost",
                 "GIT_COMMITTER_NAME": "overleaf-helper",
                 "GIT_COMMITTER_EMAIL": "overleaf-helper@localhost"}
EXCLUDE = ["/.overleaf/", "*.aux", "*.bbl", "*.bcf", "*.blg", "*.fdb_latexmk", "*.fls", "*.lof", "*.log",
           "*.lot", "*.nav", "*.out", "*.run.xml", "*.snm", "*.synctex.gz", "*.synctex(busy)", "*.toc", "*.vrb"]
ATTRIBUTES = "* text=auto eol=lf"  # text files as LF like on Overleaf, whatever an editor writes


class SyncError(Exception):
    """The working copy is not in a state for this command, or git failed."""


def run(folder, *args, env=None):
    return subprocess.run(["git", *args], cwd=folder, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=None if env is None else os.environ | env)


def git(folder, *args, env=None):
    result = run(folder, *args, env=env)
    if result.returncode != 0:
        raise SyncError(f"git {args[0]}: {(result.stderr or result.stdout).strip()}")
    return result.stdout


def rev(folder, name):
    return run(folder, "rev-parse", "--verify", "--quiet", name).stdout.strip() or None


def local_identity(folder):
    """None when git knows the user, else the tool's identity for local commits and merges."""
    known = all(run(folder, "config", key).stdout.strip() for key in ("user.name", "user.email"))
    return None if known else TOOL_IDENTITY


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def when(milliseconds):
    return datetime.datetime.fromtimestamp(milliseconds / 1000).strftime("%Y-%m-%d %H:%M")


def unpack(zip_bytes, target):
    """Files of a project zip into target. Entries that would land outside target are skipped."""
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
        for info in archive.infolist():
            name = info.filename
            if not info.flag_bits & 0x800:  # name not marked as UTF-8, so zipfile read it as cp437
                try:
                    name = name.encode("cp437").decode("utf-8")
                except (UnicodeEncodeError, UnicodeDecodeError):
                    pass
            path = PurePosixPath(name)
            if info.is_dir() or path.is_absolute() or ".." in path.parts:
                continue
            destination = target.joinpath(*path.parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(archive.read(info))


def snapshot_tree(folder, zip_bytes):
    """Git tree of a project zip, written into the repo without touching the working copy."""
    with tempfile.TemporaryDirectory() as temp:
        files = Path(temp) / "files"
        files.mkdir()
        unpack(zip_bytes, files)
        repo = ["--git-dir", str(Path(folder).resolve() / ".git"), "--work-tree", str(files)]
        env = {"GIT_INDEX_FILE": str(Path(temp) / "index")}
        git(files, *repo, "add", "--all", "--force", env=env)
        return git(files, *repo, "write-tree", env=env).strip()


def changed_files(entries):
    """Paths named in history entries, by edits or by file operations."""
    paths = {path for entry in entries for path in entry["pathnames"]}
    for entry in entries:
        for operation in entry["project_ops"]:
            for kind in ("add", "remove", "rename"):
                if kind in operation:
                    paths.add(operation[kind]["pathname"])
                    paths.update([operation[kind]["newPathname"]] if kind == "rename" else [])
    return paths


def snapshot_message(entries, since, version):
    """Commit message of a snapshot: the version, and who changed which files when since the last one.
    The first snapshot (since None) sums up the whole history instead."""
    title = f"Overleaf version {version}" + (f" (since {since})" if since is not None else "")
    new = [entry for entry in entries if since is None or entry["toV"] > since]
    if not new:
        return title
    users = sorted({ol_api.user_name(user) for entry in new for user in entry["meta"]["users"]})
    start = min(entry["meta"]["start_ts"] for entry in new)
    end = max(entry["meta"]["end_ts"] for entry in new)
    if since is None:
        return "\n".join([title, "", f"History: {len(new)} entries by {', '.join(users)}, {when(start)} to {when(end)}"])
    files = sorted(changed_files(new))
    shown = ", ".join(files[:20]) + (f" and {len(files) - 20} more" if len(files) > 20 else "")
    return "\n".join([title, "", f"Changed by {', '.join(users)}, {when(start)} to {when(end)}",
                      f"Files: {shown or '-'}"])


def worktree_tree(root):
    """Git tree of the working copy as push sends it: tracked and new files, without ignored ones.
    The files of HEAD count as tracked, so files from Overleaf that an ignore rule matches stay."""
    with tempfile.TemporaryDirectory() as temp:
        env = {"GIT_INDEX_FILE": str(Path(temp) / "index")}
        git(root, "read-tree", "HEAD", env=env)
        git(root, "add", "--all", env=env)
        return git(root, "write-tree", env=env).strip()


def tree_of(root, name):
    return git(root, "rev-parse", f"{name}^{{tree}}").strip()


def blob(root, tree, path):
    """Bytes of a file in a commit or tree."""
    result = subprocess.run(["git", "cat-file", "blob", f"{tree}:{path}"], cwd=root, capture_output=True)
    if result.returncode != 0:
        raise SyncError(f"git cat-file: {result.stderr.decode('utf-8', 'replace').strip()}")
    return result.stdout


def pending(root, tree):
    """Changes from the last pull to tree: [(status, path, new path)], status A, M, D or R; the new
    path is None except for renames."""
    found = []
    for line in git(root, "diff", "--name-status", "-M", SNAPSHOTS, tree).splitlines():
        status, *paths = line.split("\t")
        found.append(("M" if status[0] == "T" else status[0], paths[0], paths[1] if len(paths) > 1 else None))
    return found


def commit_snapshot(folder, tree, message):
    parent = rev(folder, f"refs/heads/{SNAPSHOTS}")
    commit = git(folder, "commit-tree", tree, *(["-p", parent] if parent else []), "-m", message,
                 env=SNAPSHOT_IDENTITY).strip()
    git(folder, "update-ref", f"refs/heads/{SNAPSHOTS}", commit)
    return commit


def write_exclude(folder, git_dir=None):
    """Ignore LaTeX build files, the output PDF and the tool's state, in .git/info/exclude so that
    the rules stay local and are never pushed. git_dir is the repository when it is not folder/.git."""
    outputs = [f"/{tex.stem}.pdf" for tex in sorted(folder.glob("*.tex"))
               if "\\documentclass" in tex.read_text(encoding="utf-8", errors="replace")]
    path = Path(git_dir or folder / ".git") / "info" / "exclude"
    path.parent.mkdir(exist_ok=True)
    path.write_text("# overleaf-helper: local files that are never pushed to Overleaf\n"
                    + "\n".join(EXCLUDE + outputs) + "\n", encoding="utf-8")


def ensure_line_endings(root):
    """Let git keep text files with LF, as Overleaf does, also when an editor on Windows writes CRLF.
    The rule is in .git/info/attributes, so that it stays local."""
    path = Path(root) / ".git" / "info" / "attributes"
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    if ATTRIBUTES not in existing.splitlines():
        path.parent.mkdir(exist_ok=True)
        path.write_text(existing + f"# overleaf-helper\n{ATTRIBUTES}\n", encoding="utf-8")


def find_root(start="."):
    """Top folder of the working copy that contains start."""
    folder = Path(start).resolve()
    for candidate in (folder, *folder.parents):
        if (candidate / STATE).is_file():
            return candidate
    raise SyncError(f"{folder} is not in a working copy made by clone (no {STATE.as_posix()}).")


def load_state(root):
    return json.loads((root / STATE).read_text(encoding="utf-8"))


def save_state(root, state):
    (root / STATE).parent.mkdir(exist_ok=True)
    (root / STATE).write_text(json.dumps(state, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


def clone(api, project, folder):
    """New working copy of an Overleaf project. Returns the state saved in .overleaf/project.json."""
    folder = Path(folder)
    if folder.exists() and any(folder.iterdir()):
        raise SyncError(f"{folder} exists and is not empty.")
    entries = api.updates(project["id"])
    version = entries[0]["toV"] if entries else 0
    zip_bytes = api.download_zip(project["id"])
    folder.mkdir(parents=True, exist_ok=True)
    git(folder, "init", "--quiet", f"--initial-branch={WORK}")
    git(folder, "config", "core.autocrlf", "false")
    git(folder, "config", "core.quotepath", "false")
    ensure_line_endings(folder)
    commit_snapshot(folder, snapshot_tree(folder, zip_bytes), snapshot_message(entries, None, version))
    git(folder, "reset", "--hard", "--quiet", SNAPSHOTS)
    write_exclude(folder)
    state = {"project_id": project["id"], "name": project["name"], "server": ol_api.BASE_URL,
             "version": version, "pulled_at": now()}
    save_state(folder, state)
    return state


def check_work_branch(root):
    if (root / ".git" / "MERGE_HEAD").exists():
        raise SyncError("A merge is not finished: resolve the conflict markers, then git add the files and git commit.")
    branch = run(root, "symbolic-ref", "--short", "HEAD").stdout.strip()
    if branch != WORK:
        raise SyncError(f"The working copy is on {branch or 'a detached HEAD'}; check out {WORK} first.")


def commit_local_edits(root, message):
    """Commit uncommitted local edits on main, so that a merge sees them. Returns True if any."""
    if not git(root, "status", "--porcelain").strip():
        return False
    git(root, "add", "--all")
    git(root, "commit", "--quiet", "-m", message, env=local_identity(root))
    return True


def pull(api, root, message="Local edits before pull"):
    """Bring the Overleaf changes since the last pull into main; uncommitted local edits are committed
    first with message. Returns (state, outcome): outcome is "unchanged", "merged", or the list of
    files with merge conflicts."""
    state = load_state(root)
    check_work_branch(root)
    ensure_line_endings(root)
    entries = api.updates(state["project_id"])
    version = entries[0]["toV"] if entries else state["version"]
    tree = snapshot_tree(root, api.download_zip(state["project_id"]))
    commit_local_edits(root, message)
    if tree == tree_of(root, SNAPSHOTS):
        outcome = "unchanged"
    else:
        commit_snapshot(root, tree, snapshot_message(entries, state["version"], version))
        result = run(root, "merge", "--no-edit", "-m", f"Merge Overleaf version {version}", SNAPSHOTS,
                     env=local_identity(root))
        conflicts = git(root, "diff", "--name-only", "--diff-filter=U").splitlines()
        if result.returncode != 0 and not conflicts:
            raise SyncError(f"git merge: {(result.stderr or result.stdout).strip()}")
        outcome = conflicts or "merged"
    state.update(version=version, pulled_at=now())
    save_state(root, state)
    return state, outcome


def snapshot_text(root, path):
    """Text of a file in the last pulled snapshot, or None if it is not there."""
    result = run(root, "show", f"{SNAPSHOTS}:{path}")
    return result.stdout if result.returncode == 0 else None


def changes(root, base, other=None):
    """Changed paths between base and other (commit or tree), or the working copy when other is None,
    as [(status, path)]: status A, M, D or R, a renamed path as "old -> new"."""
    found = []
    for line in git(root, "diff", "--name-status", "-M", base, *([other] if other else [])).splitlines():
        status, *paths = line.split("\t")
        found.append((status[0], " -> ".join(paths)))
    if other is None:
        found += [("A", path) for path in git(root, "ls-files", "--others", "--exclude-standard").splitlines()]
    return found


def status(api, root):
    """(state, local changes, changes on Overleaf since the last pull)."""
    state = load_state(root)
    remote = snapshot_tree(root, api.download_zip(state["project_id"]))
    return state, changes(root, SNAPSHOTS), changes(root, SNAPSHOTS, remote)


def diff(root, api=None, incoming=False, versions=None, word=False):
    """Unified diff: local edits not yet on Overleaf, the changes on Overleaf since the last pull
    (incoming), or between two Overleaf history versions."""
    options = ["diff", "--no-color", "--no-ext-diff"] + (["--word-diff=plain"] if word else [])
    if versions:
        project_id = load_state(root)["project_id"]
        old, new = (snapshot_tree(root, api.version_zip(project_id, version)) for version in versions)
        return git(root, *options, "-M", old, new)
    if incoming:
        remote = snapshot_tree(root, api.download_zip(load_state(root)["project_id"]))
        return git(root, *options, "-M", SNAPSHOTS, remote)
    text = git(root, *options, "-M", SNAPSHOTS)
    for path in git(root, "ls-files", "--others", "--exclude-standard").splitlines():
        text += run(root, *options, "--no-index", "--", "/dev/null", path).stdout
    return text
