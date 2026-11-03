"""History, comments, tracked changes, chat and labels of an Overleaf project, and the timeline that
puts them in order.

clone and pull keep a local record in the working copy, so that the whole interaction of all
coauthors can be read offline, and older text survives Overleaf's 24-hour history on free accounts:

    .overleaf/history.json            history entries, newest first, kept across pulls
    .overleaf/diffs/<from>-<to>.json  text of the changes of one entry: {path: diff parts}
    .overleaf/review.json             comment threads (deleted ones kept), their positions, tracked
                                      changes, labels, file tree and people, as of the last pull
    .overleaf/chat.json               chat messages, kept across pulls

Comments and tracked changes with their exact place in the text are read live (read_live), since
their positions only fit the text of the same moment.

replay turns the record into the git branch history, one commit per history entry with its authors,
so that git log and git blame show who wrote which line.
"""
import collections
import datetime
import json
import subprocess
from pathlib import Path

import ol_api
import ol_sync

RECORD = Path(".overleaf")
CONTEXT = 60  # characters of unchanged text shown on each side of a change
REPLAY = "history"  # branch with one commit per Overleaf history entry


def read_json(path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else default


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


def key(entry):
    return f"{entry['fromV']}-{entry['toV']}"


def parse_time(value):
    """Aware datetime of a timestamp in milliseconds or an ISO text."""
    if isinstance(value, (int, float)):
        return datetime.datetime.fromtimestamp(value / 1000, datetime.timezone.utc)
    return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))


def iso(value):
    return parse_time(value).astimezone(datetime.timezone.utc).isoformat(timespec="seconds")


def local(value):
    return f"{parse_time(value).astimezone():%Y-%m-%d %H:%M}"


def merge_history(stored, fetched):
    """The fetched entries plus the stored ones older than all of them. Overleaf regroups recent
    versions into new entries, so stored entries are only kept where the fetched ones no longer reach."""
    if not fetched:
        return stored
    oldest = fetched[-1]["fromV"]
    return fetched + [entry for entry in stored if entry["toV"] <= oldest]


def fetch_diffs(api, root, project_id, entries):
    """Fetch the text of the changes of these entries into .overleaf/diffs/, unless already there.
    An entry whose text Overleaf does not give is skipped, so that a later run tries again.
    Returns the number of skipped entries."""
    skipped = 0
    for entry in entries:
        path = root / RECORD / "diffs" / f"{key(entry)}.json"
        cached = read_json(path, {})
        missing = [name for name in entry["pathnames"] if name not in cached]
        if not missing:
            continue
        try:
            cached.update({name: api.diff(project_id, name, entry["fromV"], entry["toV"]) for name in missing})
        except ol_api.OverleafError:
            skipped += 1
            continue
        write_json(path, cached)
    return skipped


def missing_text(root, found):
    """History entries of the edit events in found whose changed text is not in the record yet."""
    versions = {event["versions"] for event in found if event["kind"] == "edit"}
    missing = []
    for entry in read_json(root / RECORD / "history.json", []):
        if key(entry) in versions:
            cached = read_json(root / RECORD / "diffs" / f"{key(entry)}.json", {})
            if any(name not in cached for name in entry["pathnames"]):
                missing.append(entry)
    return missing


def fetch_added(api, root, project_id, entries):
    """Add the full text of the docs each entry added to its cached diffs; files that are not text
    get None. replay needs it, since a doc's first text is not among the edits."""
    for entry in entries:
        path = root / RECORD / "diffs" / f"{key(entry)}.json"
        cached = read_json(path, {})
        added = [operation["add"]["pathname"] for operation in entry["project_ops"]
                 if "add" in operation and operation["add"]["pathname"] not in cached]
        if added:
            for name in added:
                parts = api.diff(project_id, name, entry["fromV"], entry["toV"])
                cached[name] = parts if isinstance(parts, list) else None  # {"binary": true} for other files
            write_json(path, cached)


def update_record(api, root, project_id, text_since=0):
    """Refresh the local record after a clone or pull. The text of the changes is fetched for every
    history entry after the version text_since that has none yet, so an interrupted run is made up
    for by the next one. Returns counts."""
    record = root / RECORD
    history = merge_history(read_json(record / "history.json", []), api.updates(project_id))
    skipped = fetch_diffs(api, root, project_id, [entry for entry in history if entry["toV"] > text_since])
    write_json(record / "history.json", history)
    keys = {key(entry) for entry in history}
    for cached in (record / "diffs").glob("*.json"):
        if cached.stem not in keys:
            cached.unlink()

    with ol_api.Realtime(api, project_id) as live:
        project = live.project
    threads = api.threads(project_id)
    for thread_id, thread in read_json(record / "review.json", {}).get("threads", {}).items():
        if thread_id not in threads:
            threads[thread_id] = thread | {"deleted": True}
    write_json(record / "review.json", {
        "fetched_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "tree": ol_api.tree(project), "members": [project["owner"], *project.get("members", [])],
        "threads": threads, "ranges": api.ranges(project_id), "labels": api.labels(project_id),
        "change_users": api.changes_users(project_id)})
    chat = {message["id"]: message for message in read_json(record / "chat.json", [])}
    chat.update((message["id"], message) for message in api.messages(project_id))
    write_json(record / "chat.json", sorted(chat.values(), key=lambda message: message["timestamp"], reverse=True))
    return {"entries": len(history), "threads": len(threads), "chat": len(chat), "skipped": skipped}


def people(history, review, chat):
    """Display names by user ID from every part of the record; the longest name wins."""
    users = [user for entry in history for user in entry["meta"]["users"]]
    users += review.get("members", []) + review.get("change_users", []) + [message.get("user") for message in chat]
    for thread in review.get("threads", {}).values():
        users += [message.get("user") for message in thread["messages"]] + [thread.get("resolved_by_user")]
    names = {}
    for user in filter(None, users):
        user_id, name = user.get("id") or user.get("_id"), ol_api.user_name(user)
        if user_id and len(name) > len(names.get(user_id, "")):
            names[user_id] = name
    return names


def display(names, user):
    """Display name of a user object, with the fullest name known for its ID."""
    user = user or {}
    return names.get(user.get("id") or user.get("_id")) or ol_api.user_name(user or None)


def mark(part):
    """A deletion as [-text-], an insertion as {+text+}, like git's word diff."""
    return "{+" + part["i"] + "+}" if "i" in part else "[-" + part["d"] + "-]"


def change_places(parts):
    """Where one file's diff changes the text: [{"line": line in the new text, "text": the line with
    the change marked}], with the unchanged text on both sides shortened."""
    if not isinstance(parts, list):
        return []
    places, line, i = [], 1, 0
    while i < len(parts):
        if "u" in parts[i]:
            line += parts[i]["u"].count("\n")
            i += 1
            continue
        before = parts[i - 1]["u"].rsplit("\n", 1)[-1] if i else ""
        start, changed = line, ""
        while i < len(parts) and "u" not in parts[i]:
            changed += mark(parts[i])
            line += parts[i].get("i", "").count("\n")
            i += 1
        after = parts[i]["u"].split("\n", 1)[0] if i < len(parts) else ""
        before = before if len(before) <= CONTEXT else "…" + before[-CONTEXT:]
        after = after if len(after) <= CONTEXT else after[:CONTEXT] + "…"
        places.append({"line": start, "text": before + changed + after})
    return places


def operation_text(operation):
    if "rename" in operation:
        return f"renamed {operation['rename']['pathname']} to {operation['rename']['newPathname']}"
    if "add" in operation:
        return f"added {operation['add']['pathname']}"
    if "remove" in operation:
        return f"removed {operation['remove']['pathname']}"
    return "changed the file tree"


def events(root):
    """Every event of the local record, oldest first. Each has time (ISO, UTC), kind (edit, comment,
    reply, resolve, suggestion, chat, label), user, file (None if none) and text; an edit also has
    versions, files, operations and changes, a comment quote and thread, a label version."""
    record = root / RECORD
    history = read_json(record / "history.json", [])
    review = read_json(record / "review.json", {})
    chat = read_json(record / "chat.json", [])
    names = people(history, review, chat)
    paths = {entry_id: path for path, (kind, entry_id) in review.get("tree", {}).items() if kind == "doc"}
    found = []

    def add(time, kind, user, file=None, text="", **extra):
        found.append({"time": iso(time), "kind": kind, "user": user, "file": file, "text": text, **extra})

    for entry in history:
        diffs = read_json(record / "diffs" / f"{key(entry)}.json", {})
        add(entry["meta"]["end_ts"], "edit", ", ".join(display(names, user) for user in entry["meta"]["users"]),
            versions=key(entry), files=sorted(entry["pathnames"]),
            operations=[operation_text(operation) for operation in entry["project_ops"]],
            changes=[{"file": path, **place} for path in sorted(entry["pathnames"])
                     for place in change_places(diffs.get(path))])

    anchors = {}
    for doc in review.get("ranges", []):
        for comment in doc["ranges"].get("comments", []):
            anchors[comment["op"]["t"]] = (paths.get(doc["id"]), comment["op"]["c"])
        for change in doc["ranges"].get("changes", []):
            add(change["metadata"]["ts"], "suggestion", display(names, {"id": change["metadata"]["user_id"]}),
                paths.get(doc["id"]), mark(change["op"]))
    for thread_id, thread in review.get("threads", {}).items():
        file, quote = anchors.get(thread_id, (None, None))
        extra = {"quote": quote, "thread": thread_id} | ({"deleted": True} if thread.get("deleted") else {})
        for n, message in enumerate(sorted(thread["messages"], key=lambda message: message["timestamp"])):
            add(message["timestamp"], "reply" if n else "comment",
                display(names, message.get("user") or {"id": message.get("user_id")}), file, message["content"],
                **extra)
        if thread.get("resolved"):
            add(thread["resolved_at"], "resolve",
                display(names, thread.get("resolved_by_user") or {"id": thread.get("resolved_by_user_id")}), file,
                **extra)
    for message in chat:
        add(message["timestamp"], "chat", display(names, message.get("user") or {"id": message.get("user_id")}),
            text=message["content"])
    for label in review.get("labels", []):
        add(label["created_at"], "label", names.get(label.get("user_id")) or label.get("user_display_name")
            or "unknown user", text=label["comment"], version=label["version"])
    return sorted(found, key=lambda event: event["time"])


def text_at_end(parts):
    """A file's text at the end of a history entry, from the parts of its diff."""
    return "".join(part.get("u", "") + part.get("i", "") for part in parts)


def person(user):
    """Name <email> of a history user, for git."""
    user = user or {}
    name = ol_api.user_name(user or None).replace("<", "").replace(">", "")
    return f"{name} <{user.get('email') or str(user.get('id', 'unknown')) + '@overleaf'}>"


def quoted(path):
    return '"' + path.replace("\\", "\\\\").replace('"', '\\"') + '"'


def replay_message(entry, users):
    done = ([f"edited {', '.join(entry['pathnames'])}"] if entry["pathnames"] else [])
    summary = "; ".join(done + [operation_text(operation) for operation in entry["project_ops"]]) or "changed the project"
    lines = [f"Overleaf versions {key(entry)}: " + (summary if len(summary) <= 200 else summary[:200] + "…")]
    labels = [label["comment"] for label in entry.get("labels", [])]
    if labels:
        lines += ["", f"Named version: {', '.join(labels)}"]
    if users[1:]:
        lines += ["", *[f"Co-authored-by: {person(user)}" for user in users[1:]]]
    return "\n".join(lines) + "\n"


def replay_stream(root, history):
    """Input of git fast-import for the branch history: for each history entry that changed text,
    oldest first, a commit with the text of its docs at the end of the entry, its first user as
    author, the others as coauthors, and its time. Files that are not text are left out.
    Returns (input, number of commits)."""
    out, files, commits = [f"reset refs/heads/{REPLAY}\n".encode()], set(), 0
    for entry in reversed(history):
        diffs = read_json(root / RECORD / "diffs" / f"{key(entry)}.json", {})
        commands, renamed, removed = [], {}, set()

        def modify(path, parts):
            data = text_at_end(parts).encode("utf-8")
            files.add(path)
            commands.append(f"M 100644 inline {quoted(path)}\ndata {len(data)}\n".encode("utf-8") + data + b"\n")

        for operation in sorted(entry["project_ops"], key=lambda operation: operation.get("atV", 0)):
            if "add" in operation and isinstance(diffs.get(operation["add"]["pathname"]), list):
                modify(operation["add"]["pathname"], diffs[operation["add"]["pathname"]])
            elif "rename" in operation and operation["rename"]["pathname"] in files:
                old, new = operation["rename"]["pathname"], operation["rename"]["newPathname"]
                commands.append(f"R {quoted(old)} {quoted(new)}\n".encode("utf-8"))
                files.discard(old)
                files.add(new)
                renamed[old] = new
            elif "remove" in operation and operation["remove"]["pathname"] in files:
                commands.append(f"D {quoted(operation['remove']['pathname'])}\n".encode("utf-8"))
                files.discard(operation["remove"]["pathname"])
                removed.add(operation["remove"]["pathname"])
        for path in entry["pathnames"]:
            if isinstance(diffs.get(path), list) and path not in removed:
                modify(renamed.get(path, path), diffs[path])
        if not commands:
            continue
        users = entry["meta"]["users"] or [None]
        author = f"{person(users[0])} {entry['meta']['end_ts'] // 1000} +0000"
        message = replay_message(entry, users).encode("utf-8")
        out += [f"commit refs/heads/{REPLAY}\nauthor {author}\ncommitter {author}\ndata {len(message)}\n".encode("utf-8"),
                message, b"\n", *commands, b"\n"]
        commits += 1
    return b"".join(out), commits


def replay(api, root, project_id):
    """Build the branch history from the local record, after fetching the text it still lacks. The
    same record always gives the same commits, so running it again only adds what is new. Returns
    the number of commits."""
    history = read_json(root / RECORD / "history.json", [])
    skipped = fetch_diffs(api, root, project_id, history)
    if skipped:
        raise ol_api.OverleafError(f"Overleaf did not give the text of {skipped} history entries; try again later.")
    fetch_added(api, root, project_id, history)
    stream, commits = replay_stream(root, history)
    if commits:
        result = subprocess.run(["git", "fast-import", "--force", "--quiet"], cwd=root, input=stream, capture_output=True)
        if result.returncode != 0:
            raise ol_sync.SyncError(f"git fast-import: {result.stderr.decode('utf-8', 'replace').strip()}")
    return commits


def line_authors(root):
    """Lines of the text at the end of the branch history, counted by author."""
    counts = collections.Counter()
    for path in ol_sync.git(root, "ls-tree", "-r", "--name-only", REPLAY).splitlines():
        for line in ol_sync.git(root, "blame", "--line-porcelain", REPLAY, "--", path).splitlines():
            if line.startswith("author "):
                counts[line[len("author "):]] += 1
    return counts


def select(found, since=None, user=None, file=None, kinds=None):
    """Events from since on, by a user whose name contains user, about file, of these kinds."""
    return [event for event in found
            if (since is None or parse_time(event["time"]) >= since)
            and (user is None or user.casefold() in event["user"].casefold())
            and (file is None or event["file"] == file or file in event.get("files", []))
            and (kinds is None or event["kind"] in kinds)]


def read_live(api, project_id):
    """Comment threads, names, and {path: (text, version, ranges)} of every doc with comments or
    tracked changes, read live; the text and ranges of a doc come from the same moment."""
    threads = api.threads(project_id)
    with ol_api.Realtime(api, project_id) as live:
        paths = {entry_id: path for path, (kind, entry_id) in ol_api.tree(live.project).items() if kind == "doc"}
        members = [live.project["owner"], *live.project.get("members", [])]
        docs = {paths[doc["id"]]: live.read_doc(doc["id"]) for doc in api.ranges(project_id)
                if doc["ranges"] and doc["id"] in paths}
    names = people([], {"members": members, "change_users": api.changes_users(project_id), "threads": threads}, [])
    return threads, docs, names


def thread_entry(names, thread_id, thread, place):
    entry = {"thread": thread_id, **place, "status": "resolved" if thread.get("resolved") else "open",
             "messages": [{"user": display(names, message.get("user") or {"id": message.get("user_id")}),
                           "time": iso(message["timestamp"]), "text": message["content"]}
                          for message in sorted(thread["messages"], key=lambda message: message["timestamp"])]}
    if thread.get("resolved"):
        entry["resolved_by"] = display(names, thread.get("resolved_by_user") or {"id": thread.get("resolved_by_user_id")})
        entry["resolved_at"] = iso(thread["resolved_at"])
    return entry


def comments(threads, docs, names, last_pull, context=1):
    """Every comment with the text it refers to, by file and position; threads without a position
    come last. last_pull maps paths to their text at the last pull. Fields: reference/export-format.md."""
    found, placed = [], set()
    for path, (text, version, ranges) in sorted(docs.items()):
        lines = text.split("\n")
        same = (last_pull.get(path) or "").rstrip("\n") == text.rstrip("\n")
        for comment in sorted(ranges.get("comments", []), key=lambda comment: comment["op"]["p"]):
            op = comment["op"]
            first = text.count("\n", 0, op["p"]) + 1
            last = first + op["c"].count("\n")
            placed.add(op["t"])
            found.append(thread_entry(names, op["t"], threads.get(op["t"], {"messages": []}), {
                "file": path, "line": first, "last_line": last, "offset": op["p"], "quote": op["c"],
                "context": [{"line": n, "text": lines[n - 1]}
                            for n in range(max(1, first - context), min(len(lines), last + context) + 1)],
                "doc_version": version, "same_as_last_pull": same}))
    for thread_id, thread in threads.items():
        if thread_id not in placed:
            found.append(thread_entry(names, thread_id, thread, {
                "file": None, "line": None, "last_line": None, "offset": None, "quote": None, "context": [],
                "doc_version": None, "same_as_last_pull": None}))
    return found


def marked(text, comments=(), changes=()):
    """text with comments marked ⟦C1⟧…⟦/C1⟧ and tracked changes as {+inserted+} or [-deleted-].
    comments are (number, offset, length), changes are ops {p, i} or {p, d}. Newlines of deleted
    text become ↵, so that the lines of the result are the lines of text."""
    inserts = []  # (offset, order, mark): at one offset, ends come before deletions before starts
    for number, offset, length in comments:
        if length:
            inserts += [(offset, 2, f"⟦C{number}⟧"), (offset + length, 0, f"⟦/C{number}⟧")]
        else:  # its text was deleted
            inserts.append((offset, 2, f"⟦C{number}⟧⟦/C{number}⟧"))
    for op in changes:
        if "i" in op:
            inserts += [(op["p"], 2, "{+"), (op["p"] + len(op["i"]), 0, "+}")]
        else:
            inserts.append((op["p"], 1, "[-" + op["d"].replace("\n", "↵") + "-]"))
    pieces, last = [], 0
    for position, _, text_mark in sorted(inserts, key=lambda insert: insert[:2]):
        pieces += [text[last:position], text_mark]
        last = position
    return "".join(pieces) + text[last:]


def suggestions(docs, names):
    """Every tracked change that waits to be accepted or rejected, by file and position, with its
    line marked. Fields: reference/export-format.md."""
    found = []
    for path, (text, _, ranges) in sorted(docs.items()):
        changes = sorted(ranges.get("changes", []), key=lambda change: change["op"]["p"])
        lines = marked(text, changes=[change["op"] for change in changes]).split("\n")
        for change in changes:
            op = change["op"]
            line = text.count("\n", 0, op["p"]) + 1
            found.append({"id": change["id"], "file": path, "line": line, "offset": op["p"],
                          "kind": "insert" if "i" in op else "delete", "text": op.get("i", op.get("d")),
                          "user": display(names, {"id": change["metadata"]["user_id"]}),
                          "time": iso(change["metadata"]["ts"]), "marked_line": lines[line - 1]})
    return found


def replacements(found):
    """{ID: ID of the other part} of each replaced text among the suggestions: a deletion right after an
    insertion by the same person, which the review panel shows and accepts as one."""
    deletions = {(change["file"], change["offset"], change["user"]): change["id"]
                 for change in found if change["kind"] == "delete"}
    pairs = {}
    for change in found:
        other = deletions.get((change["file"], change["offset"] + len(change["text"]), change["user"]))
        if change["kind"] == "insert" and other:
            pairs[change["id"]], pairs[other] = other, change["id"]
    return pairs


def select_suggestions(found, ids=(), user=None, file=None):
    """The suggestions with these IDs, by people whose name contains user, in file. The ID of one part
    of a replaced text also selects the other part."""
    pairs = replacements(found)
    wanted = set(ids) | {pairs[change_id] for change_id in ids if change_id in pairs}
    return [change for change in found
            if (not ids or change["id"] in wanted)
            and (user is None or user.casefold() in change["user"].casefold())
            and (file is None or change["file"] == file)]


def accept(api, project_id, change_ids):
    """Accept these tracked changes on Overleaf, doc by doc. Returns those that Overleaf still lists."""
    wanted = set(change_ids)
    for doc in api.ranges(project_id):
        ids = [change["id"] for change in doc["ranges"].get("changes", []) if change["id"] in wanted]
        if ids:
            api.accept_changes(project_id, doc["id"], ids)
    return sorted(wanted & {change["id"] for doc in api.ranges(project_id)
                            for change in doc["ranges"].get("changes", [])})


def write_inline(target, docs, found, changes):
    """Copies of the docs with comments and suggestions marked in the text, and the threads and
    suggestions as % lines after the line where each ends. Returns the written files."""
    numbers = {comment["thread"]: n for n, comment in enumerate([c for c in found if c["file"]], 1)}
    written = []
    for path, (text, _, _) in sorted(docs.items()):
        mine = [comment for comment in found if comment["file"] == path]
        theirs = [change for change in changes if change["file"] == path]
        if not mine and not theirs:
            continue
        lines = marked(text, [(numbers[c["thread"]], c["offset"], len(c["quote"])) for c in mine],
                       [{"p": c["offset"], "i" if c["kind"] == "insert" else "d": c["text"]} for c in theirs]).split("\n")
        notes = {}
        for comment in mine:
            state = comment["status"] + (f" by {comment['resolved_by']}" if comment.get("resolved_by") else "")
            notes.setdefault(comment["last_line"], []).append(f"% ⟦C{numbers[comment['thread']]}⟧ {state}")
            notes[comment["last_line"]] += [f"%   {message['user']} ({local(message['time'])}): {one_line(message['text'])}"
                                            for message in comment["messages"]]
        for change in theirs:
            kind = {"i" if change["kind"] == "insert" else "d": change["text"]}
            notes.setdefault(change["line"], []).append(
                f"% suggestion by {change['user']} ({local(change['time'])}): {one_line(mark(kind))}")
        file = target / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("\n".join(line for n, text_line in enumerate(lines, 1)
                                  for line in [text_line, *notes.get(n, [])]), encoding="utf-8")
        written.append(file)
    return written


def shorten(text, length=CONTEXT):
    text = " ".join(text.split())
    return text if len(text) <= length else text[:length] + "…"


def one_line(text):
    return text.replace("\n", "↵")


def sentence(event):
    """What happened, in one line."""
    where = f" in {event['file']}" if event.get("file") else ""
    quote = f" at “{shorten(event['quote'])}”" if event.get("quote") else ""
    kind = event["kind"]
    if kind == "edit":
        done = ([f"edited {', '.join(event['files'])}"] if event["files"] else []) + event["operations"]
        return "; ".join(done) or "changed the project"
    if kind == "suggestion":
        return f"suggested{where}: {event['text']}"
    if kind == "resolve":
        return f"resolved the comment{where}{quote}"
    if kind in ("comment", "reply"):
        deleted = " (deleted on Overleaf)" if event.get("deleted") else ""
        return f"{'commented' if kind == 'comment' else 'replied'}{where}{quote}{deleted}: {event['text']}"
    if kind == "chat":
        return f"wrote in the chat: {event['text']}"
    return f"named version {event['version']} “{event['text']}”"


def as_text(found, changes=True):
    lines = []
    for event in found:
        lines.append(f"{local(event['time'])}  {event['user']}: " + sentence(event).replace("\n", "\n" + " " * 18))
        for change in event.get("changes", []) if changes else []:
            lines.append(f"    {change['file']}:{change['line']}  {one_line(change['text'])}")
    return "\n".join(lines)


def code(text):
    text = one_line(text)
    return f"`` {text} ``" if "`" in text else f"`{text}`"


def as_markdown(found, title):
    lines, day = [f"# {title}"], None
    for event in found:
        moment = parse_time(event["time"]).astimezone()
        if moment.date() != day:
            day = moment.date()
            lines += ["", f"## {day:%Y-%m-%d}", ""]
        lines.append(f"- {moment:%H:%M} **{event['user']}** {sentence(event)}".replace("\n", "\n  "))
        lines += [f"  - `{change['file']}` line {change['line']}: {code(change['text'])}"
                  for change in event.get("changes", [])]
    return "\n".join(lines) + "\n"


def as_json(found):
    return json.dumps(found, indent=1, ensure_ascii=False)


def place(comment):
    if not comment["file"]:
        return "(no position in the text)"
    lines = f"{comment['line']}" + (f"-{comment['last_line']}" if comment["last_line"] != comment["line"] else "")
    return f"{comment['file']}:{lines}"


def state(comment):
    if comment["status"] == "open":
        return "open"
    return f"resolved by {comment['resolved_by']}, {local(comment['resolved_at'])}"


def comments_as_text(found):
    lines = []
    for comment in found:
        quote = f"  “{shorten(comment['quote'])}”" if comment.get("quote") else ""
        lines.append(f"{place(comment)}  {state(comment)}{quote}")
        lines += [f"    {message['user']}, {local(message['time'])}: {message['text']}".replace("\n", "\n      ")
                  for message in comment["messages"]]
    return "\n".join(lines)


def comments_as_markdown(found, title):
    lines = [f"# {title}"]
    for comment in found:
        lines += ["", f"## {place(comment)} ({state(comment)})", ""]
        if comment.get("quote"):
            lines += [f"> {line}" for line in comment["quote"].split("\n")] + [""]
        elif comment["file"]:
            lines += ["The highlighted text was deleted.", ""]
        if comment["context"]:
            width = len(str(comment["context"][-1]["line"]))
            lines += ["```latex", *[f"{line['line']:>{width}} | {line['text']}" for line in comment["context"]], "```", ""]
        lines += [f"- **{message['user']}** ({local(message['time'])}): {message['text']}".replace("\n", "\n  ")
                  for message in comment["messages"]]
    return "\n".join(lines) + "\n"


def suggestions_as_text(found):
    lines, at = [], None
    for change in found:
        if (change["file"], change["line"]) != at:
            at = (change["file"], change["line"])
            lines.append(f"{change['file']}:{change['line']}  {change['marked_line']}")
        verb = "inserted" if change["kind"] == "insert" else "deleted"
        lines.append(f"    {change['user']}, {local(change['time'])}: {verb} “{one_line(change['text'])}”  "
                     f"(ID {change['id']})")
    return "\n".join(lines)
