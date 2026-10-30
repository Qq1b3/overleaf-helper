"""push: send the local edits of a working copy to Overleaf.

A changed doc gets the changed words as edits over the live connection, the same as typing in the
editor, so comments and suggestions on the unchanged text stay where they are. New files and
changed binary files are uploaded; renames, moves and deletions go through the file tree. Nothing is
sent when Overleaf changed since the last pull or when a check fails, and nothing without send.
"""
import difflib
import posixpath
import re

import ol_api
import ol_sync

TOKEN = re.compile(r"\w+|\s+|[^\w\s]")  # words, runs of white space, single other characters
CHUNK = 200  # edit operations per update
LARGE = 4_000_000  # word pairs above which a changed block is replaced whole instead of compared
MAX_DOC = 2_097_152  # characters, Overleaf's ol-maxDocLength
ORDER = {"folder": 0, "rename": 1, "move": 1, "edit": 2, "upload": 3, "delete": 4}


class PushError(Exception):
    """push stopped before sending anything."""


def text_ops(old, new):
    """Edits that turn old into new, compared line by line and then word by word inside the changed
    lines. Returns (ops, spans): ops [{p, d}] and [{p, i}] are applied in order, each position in the
    text as it is at that step, like Overleaf applies them; spans are the deleted (start, end) in old."""
    old_lines, new_lines = old.splitlines(keepends=True), new.splitlines(keepends=True)
    starts = [0]
    for line in old_lines:
        starts.append(starts[-1] + len(line))
    ops, spans, shift = [], [], 0
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        old_words, new_words = TOKEN.findall("".join(old_lines[i1:i2])), TOKEN.findall("".join(new_lines[j1:j2]))
        offsets = [0]
        for word in old_words:
            offsets.append(offsets[-1] + len(word))
        if len(old_words) * len(new_words) > LARGE:
            changes = [("replace", 0, len(old_words), 0, len(new_words))]
        else:
            changes = difflib.SequenceMatcher(None, old_words, new_words, autojunk=False).get_opcodes()
        for tag2, k1, k2, l1, l2 in changes:
            if tag2 == "equal":
                continue
            deleted, inserted = "".join(old_words[k1:k2]), "".join(new_words[l1:l2])
            at = starts[i1] + offsets[k1]
            if deleted:
                ops.append({"p": at + shift, "d": deleted})
                spans.append((at, at + len(deleted)))
            if inserted:
                ops.append({"p": at + shift, "i": inserted})
            shift += len(inserted) - len(deleted)
    return ops, spans


def apply_ops(text, ops):
    """text after the ops, applied in order like Overleaf does."""
    for op in ops:
        if "i" in op:
            text = text[:op["p"]] + op["i"] + text[op["p"]:]
        elif text[op["p"]:op["p"] + len(op["d"])] == op["d"]:
            text = text[:op["p"]] + text[op["p"] + len(op["d"]):]
        else:
            raise ValueError(f"deletion at {op['p']} does not match the text")
    return text


def shorten(text, length=50):
    text = " ".join(text.split())
    return text if len(text) <= length else text[:length] + "…"


def checked_text(path, text, warnings, errors):
    """The text as Overleaf stores it, noting what cannot be sent."""
    if "\r\n" in text:
        text = text.replace("\r\n", "\n")
        warnings.append(f"{path}: Windows line endings are sent as the LF that Overleaf uses")
    lines = [n for n, line in enumerate(text.split("\n"), 1) if any(ord(char) > 0xFFFF for char in line)]
    if lines:
        errors.append(f"{path}: line {', '.join(map(str, lines[:5]))} has characters Overleaf cannot store "
                      "(emoji and other characters outside the BMP); Overleaf would turn each into two �")
    if len(text) > MAX_DOC:
        errors.append(f"{path}: {len(text)} characters, more than the {MAX_DOC} Overleaf allows in one file")
    return text


def upload_step(root, local, path, warnings, errors):
    content = ol_sync.blob(root, local, path)
    try:
        if b"\0" not in content:
            content = checked_text(path, content.decode("utf-8"), warnings, errors).encode("utf-8")
    except UnicodeDecodeError:
        pass  # a binary file
    return {"action": "upload", "path": path, "content": content, "replace": False}


def warn_overlaps(path, text, spans, ranges, warnings):
    """Warn when the edit deletes text that a comment or a suggested insertion covers."""
    covered = [("comment", comment["op"]["p"], comment["op"]["c"]) for comment in ranges.get("comments", [])]
    covered += [("suggested insertion", change["op"]["p"], change["op"]["i"])
                for change in ranges.get("changes", []) if "i" in change["op"]]
    for kind, start, quote in covered:
        end = start + len(quote)
        if end > start and any(first < end and start < last for first, last in spans):
            warnings.append(f"{path} line {text.count(chr(10), 0, start) + 1}: the edit deletes text of the {kind} "
                            f"at “{shorten(quote)}”")


def edit_step(root, live, local, base_path, path, doc_id, warnings, errors):
    """The edits of a changed doc. The doc stays open, so that the edits can be sent against the
    version read here and Overleaf merges what others type meanwhile."""
    try:
        text = checked_text(path, ol_sync.blob(root, local, path).decode("utf-8"), warnings, errors)
    except UnicodeDecodeError:
        errors.append(f"{path}: not UTF-8 text, which an editable Overleaf file needs")
        return None
    base = ol_sync.blob(root, ol_sync.SNAPSHOTS, base_path).decode("utf-8")
    current, version, ranges = live.join_doc(doc_id)
    if current != base:
        errors.append(f"{path}: the text on Overleaf differs from the last pull; pull first")
        return None
    ops, spans = text_ops(base, text)
    if not ops:
        return None
    if apply_ops(base, ops) != text:
        errors.append(f"{path}: the computed edits do not give the local text, so nothing is sent (a bug)")
        return None
    warn_overlaps(path, base, spans, ranges, warnings)
    return {"action": "edit", "path": path, "id": doc_id, "version": version, "text": text, "ops": ops}


def folder_steps(entries, steps):
    """New folders that the uploads and moves need, parents first."""
    needed = set()
    for step in steps:
        target = step.get("to") or (step["path"] if step["action"] == "upload" else "")
        folder = posixpath.dirname(target)
        while folder and folder not in entries:
            needed.add(folder)
            folder = posixpath.dirname(folder)
    return [{"action": "folder", "path": folder} for folder in sorted(needed, key=lambda name: (name.count("/"), name))]


def plan(root, api, project_id, live, local):
    """Steps that bring Overleaf from the last pull to the local tree, with warnings and errors."""
    entries = ol_api.tree(live.project)
    ranges = {doc["id"]: doc["ranges"] for doc in api.ranges(project_id)}
    steps, warnings, errors = [], [], []
    for status, path, new_path in ol_sync.pending(root, local):
        if status == "A":
            steps.append(upload_step(root, local, path, warnings, errors))
            continue
        if path not in entries:
            errors.append(f"{path}: not on Overleaf any more; pull first")
            continue
        kind, entity_id = entries[path]
        if status == "D":
            steps.append({"action": "delete", "path": path, "kind": kind, "id": entity_id})
            count = len(ranges.get(entity_id, {}).get("comments", []))
            if count:
                warnings.append(f"{path}: deleting it also deletes its {count} comments")
            continue
        target = new_path or path
        if status == "R":
            moved = posixpath.dirname(path) != posixpath.dirname(new_path)
            steps.append({"action": "move" if moved else "rename", "path": path, "to": new_path, "kind": kind,
                          "id": entity_id})
            if ol_sync.blob(root, ol_sync.SNAPSHOTS, path) == ol_sync.blob(root, local, new_path):
                continue
        if kind == "doc":
            step = edit_step(root, live, local, path, target, entity_id, warnings, errors)
        else:
            step = {"action": "upload", "path": target, "content": ol_sync.blob(root, local, target), "replace": True}
        if step:
            steps.append(step)
    steps = folder_steps(entries, steps) + steps
    return sorted(steps, key=lambda step: ORDER[step["action"]]), warnings, errors


def describe(step):
    action, path = step["action"], step["path"]
    if action == "edit":
        added = sum(len(op["i"]) for op in step["ops"] if "i" in op)
        removed = sum(len(op["d"]) for op in step["ops"] if "d" in op)
        return f"edit    {path}  (+{added} -{removed} characters)"
    if action == "upload":
        return f"upload  {path}  ({'replaces the file on Overleaf' if step['replace'] else 'new'})"
    if action in ("rename", "move"):
        return f"{action:<7} {path} -> {step['to']}"
    if action == "folder":
        return f"folder  {path}  (new)"
    return f"delete  {path}"


def send(api, project_id, live, steps, track, warnings):
    entries = ol_api.tree(live.project)
    for step in steps:
        action = step["action"]
        if action == "folder":
            parent, name = posixpath.split(step["path"])
            entries[step["path"]] = ("folder", api.create_folder(project_id, entries[parent][1], name)["_id"])
        elif action in ("rename", "move"):
            (old_folder, old_name), (new_folder, new_name) = posixpath.split(step["path"]), posixpath.split(step["to"])
            if new_folder != old_folder:
                api.move(project_id, step["kind"], step["id"], entries[new_folder][1])
            if new_name != old_name:
                api.rename(project_id, step["kind"], step["id"], new_name)
        elif action == "edit":
            version = step["version"]
            for start in range(0, len(step["ops"]), CHUNK):
                version = live.apply_update(step["id"], step["ops"][start:start + CHUNK], version, track)
            live.leave_doc(step["id"])
            if live.read_doc(step["id"])[0] != step["text"]:
                warnings.append(f"{step['path']}: after the push the text on Overleaf differs from yours; someone "
                                "may have edited it at the same time. Pull to see it.")
        elif action == "upload":
            folder, name = posixpath.split(step["path"])
            api.upload(project_id, entries[folder][1], name, step["content"])
        else:
            api.delete(project_id, step["kind"], step["id"])


def push(api, root, send_it=False, track=False):
    """Check and describe the push of the working copy; with send_it also send it, then pull. Returns
    {steps, warnings, errors, diff}, after sending also {state, outcome, left}: left lists the
    files that still differ from Overleaf."""
    state = ol_sync.load_state(root)
    ol_sync.check_work_branch(root)
    ol_sync.ensure_line_endings(root)
    local = ol_sync.worktree_tree(root)
    result = {"steps": [], "warnings": [], "errors": [], "diff": ""}
    if local == ol_sync.tree_of(root, ol_sync.SNAPSHOTS):
        return result
    project_id = state["project_id"]
    if ol_sync.snapshot_tree(root, api.download_zip(project_id)) != ol_sync.tree_of(root, ol_sync.SNAPSHOTS):
        raise PushError("Overleaf has changed since the last pull. Pull first, check the result, then push.")
    with ol_api.Realtime(api, project_id) as live:
        steps, warnings, errors = plan(root, api, project_id, live, local)
        result.update(steps=[describe(step) for step in steps], warnings=warnings, errors=errors,
                      diff=ol_sync.git(root, "diff", "--no-color", "--no-ext-diff", "-M", ol_sync.SNAPSHOTS, local))
        if errors or not send_it or not steps:
            return result
        send(api, project_id, live, steps, track, warnings)
    state, outcome = ol_sync.pull(api, root, "Pushed to Overleaf")
    result.update(state=state, outcome=outcome,
                  left=[path for _, path, _ in ol_sync.pending(root, ol_sync.worktree_tree(root))])
    return result
