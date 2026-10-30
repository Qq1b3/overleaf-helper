"""compile: build the project on Overleaf with its TeX Live, fetch the PDF and the log, and sum up
the log: errors and warnings with file and line, overfull and underfull boxes, pages.

Overleaf compiles what is on Overleaf, so local edits count only after a push.
"""
import re

import ol_api
import ol_sync

OUTPUT = ol_sync.STATE.parent / "output"  # ignored like all of .overleaf/, so it is never pushed
WARNING = re.compile(r"^(LaTeX|LaTeX Font|Package \S+|Class \S+) Warning: ")


def log_summary(log):
    """{errors, warnings: [{file, line, message}], boxes, pages} of a LaTeX log. The file is the
    project file TeX was reading, found by following the parentheses with which TeX opens and closes
    files in the log; None when it is not a file of the project."""
    errors, warnings, boxes, pages, files = [], [], 0, None, []
    lines = log.splitlines()
    for number, line in enumerate(lines):
        for match in re.finditer(r"\(([^\s()]*)|\)", line):
            if match.group() == ")":
                if files:
                    files.pop()
            else:
                name = match.group(1)
                files.append(name[2:] if name.startswith("./") else None)
        current = next((name for name in reversed(files) if name), None)
        if line.startswith("! "):
            at = next((int(found.group(1)) for found in (re.match(r"l\.(\d+)\b", following)
                                                       for following in lines[number + 1:number + 30]) if found), None)
            errors.append({"file": current, "line": at, "message": line[2:].strip()})
        elif WARNING.match(line):
            message = [line.strip()]
            for following in lines[number + 1:number + 6]:
                if not following.strip():
                    break
                message.append(following.strip())
            text = " ".join(" ".join(message).split())
            at = re.search(r"on input line (\d+)", text)
            warnings.append({"file": current, "line": int(at.group(1)) if at else None, "message": text})
        elif re.match(r"(Over|Under)full \\[hv]box", line):
            boxes += 1
        elif found := re.search(r"Output written on .*\((\d+) pages?", line):
            pages = int(found.group(1))
    return {"errors": errors, "warnings": warnings, "boxes": boxes, "pages": pages}


def compile_project(api, root, out=None, draft=False):
    """Compile on Overleaf and save output.pdf and output.log into out (default .overleaf/output).
    Returns (status, saved files, log summary or None, local changes not pushed yet)."""
    state = ol_sync.load_state(root)
    with ol_api.Realtime(api, state["project_id"]) as live:
        root_doc = live.project.get("rootDoc_id")
    compiled = api.compile(state["project_id"], root_doc, draft)
    out = out or root / OUTPUT
    out.mkdir(parents=True, exist_ok=True)
    saved = {}
    for name in ("output.pdf", "output.log"):
        content = api.output_file(compiled, name)
        if content is not None:
            (out / name).write_bytes(content)
            saved[name] = out / name
    summary = log_summary(saved["output.log"].read_text(encoding="utf-8", errors="replace")) if "output.log" in saved else None
    unpushed = [path for _, path, _ in ol_sync.pending(root, ol_sync.worktree_tree(root))]
    return compiled.get("status"), saved, summary, unpushed
