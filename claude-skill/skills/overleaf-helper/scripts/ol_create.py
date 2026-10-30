"""create: make a new Overleaf project, blank, with Overleaf's example paper or from a local folder,
and clone it into a working copy, so that it can be edited, pushed and compiled like any other
project.

Like olcli's `project create` (github.com/aloth/olcli), the project is made with the calls of the
Overleaf editor's "New project" dialog; a local folder goes up as a zip, like "Upload project".
"""
import io
import re
import tempfile
import zipfile
from pathlib import Path

import ol_sync


def folder_name(name):
    """Folder for a new working copy when none is given: the project name, without characters that
    file systems refuse."""
    return re.sub(r'[<>:"/\\|?*]', "_", name).rstrip(". ")


def upload_files(source):
    """Files of the folder source that push would send from a working copy: without the LaTeX build
    files, the output PDF, .git and .overleaf, and without what the .gitignore files of source
    ignore, unless a git repository of source tracks it. git lists them, with a throwaway repository
    whose work tree is source. A folder that is a git repository of its own is listed as "folder/"
    and not sent."""
    source = Path(source)
    if not source.is_dir():
        raise ol_sync.SyncError(f"{source} is not a folder.")
    with tempfile.TemporaryDirectory() as temp:
        ol_sync.git(temp, "init", "--quiet")
        git_dir = Path(temp) / ".git"
        ol_sync.write_exclude(source, git_dir)
        others = ["--git-dir", str(git_dir), "--work-tree", ".", "ls-files", "-z", "--others"]
        listed = ol_sync.git(source, *others, "--exclude-standard")
        build = ol_sync.git(source, *others, "--ignored", f"--exclude-from={git_dir / 'info' / 'exclude'}")
    tracked = ol_sync.run(source, "ls-files", "-z").stdout  # empty when source is not in a git repository
    paths = set(listed.split("\0")) | (set(tracked.split("\0")) - set(build.split("\0")))
    return sorted(path for path in paths if path and (path.endswith("/") or Path(source, path).is_file()))


def pack(source, paths):
    """Zip of the files of source; folders listed as "folder/" are left out."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in paths:
            if not path.endswith("/"):
                archive.write(Path(source, path), path)
    return buffer.getvalue()


def left_out(folder, listed):
    """Files of the upload that are not in the working copy of the new project, and the folders that
    were not sent. Overleaf ignores some file types, such as .gz and .docx, and unpacks a zip whose
    files are all in one folder without that folder."""
    files = [path for path in listed if not path.endswith("/")]
    one_folder = len({path.split("/")[0] for path in files}) == 1 and all("/" in path for path in files)
    kept = set(ol_sync.git(folder, "ls-files", "-z").split("\0"))
    return [path for path in listed
            if path.endswith("/") or (path.split("/", 1)[1] if one_folder else path) not in kept]


def create(api, name, example=False, folder=None, source=None):
    """Create the project on Overleaf, blank, as the example paper or from the files of the folder
    source, and clone it into folder. Returns (project, state, sent, missing): the project as {id,
    name}, the state saved in .overleaf/project.json, the number of files uploaded from source, and
    the files of source that are not in the project."""
    name = name.strip()
    if not name:
        raise ol_sync.SyncError("The project needs a name.")
    if "/" in name or "\\" in name or len(name) > 150:
        raise ol_sync.SyncError("Overleaf refuses project names with / or \\ and names longer than 150 characters; "
                                "nothing was created on Overleaf.")
    folder = Path(folder or folder_name(name))
    if folder.exists() and any(folder.iterdir()):
        raise ol_sync.SyncError(f"{folder} exists and is not empty; nothing was created on Overleaf.")
    listed = upload_files(source) if source else []
    sent = sum(not path.endswith("/") for path in listed)
    if source and not sent:
        raise ol_sync.SyncError(f"{source} has no files to upload; nothing was created on Overleaf.")
    if source:
        project_id = api.upload_project(f"{folder_name(name) or 'project'}.zip", pack(source, listed))
        api.rename_project(project_id, name)  # Overleaf named it after the \title of the main document
    else:
        project_id = api.create_project(name, example)["project_id"]
    project = {"id": project_id, "name": name}
    state = ol_sync.clone(api, project, folder)
    state["text_since"] = state["version"]
    ol_sync.save_state(folder, state)
    return project, state, sent, left_out(folder, listed)
