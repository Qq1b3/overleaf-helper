"""comment, reply, resolve and suggest: write to the review panel of a project, as the logged-in user.

They work on the text as it is on Overleaf now, not on the working copy. A comment, an answer or a
resolve leaves the text as it is. A suggestion is a tracked change that coauthors accept or reject;
Overleaf's text then contains it, so the working copy needs a pull before the next push.
"""
import difflib
import posixpath
import re

import ol_api
import ol_push
import ol_review


class AnnotateError(Exception):
    """Nothing was written: the file, the text or the comment is not on Overleaf, or not just once."""


def clean(path):
    return posixpath.normpath(path.replace("\\", "/"))


def doc_id(live, path):
    """ID of the doc at path, the file Overleaf edits as text."""
    entry = ol_api.tree(live.project).get(path)
    if entry is None:
        raise AnnotateError(f"No file {path} on Overleaf.")
    if entry[0] != "doc":
        raise AnnotateError(f"{path} is not a text file on Overleaf.")
    return entry[1]


def line_of(text, offset):
    return text.count("\n", 0, offset) + 1


def find(text, quote, line=None, path="the file"):
    """(start, end) of quote in text. Any white space in quote matches any white space, line breaks
    too; with line, only a place that starts on that line counts. Raises AnnotateError unless exactly
    one place fits."""
    words = quote.split()
    if not words:
        raise AnnotateError("The text to find is empty.")
    pattern = re.compile(r"(?=(" + r"\s+".join(map(re.escape, words)) + "))")  # overlapping places too
    places = [match.span(1) for match in pattern.finditer(text)]
    if line is not None:
        places = [place for place in places if line_of(text, place[0]) == line]
    if len(places) == 1:
        return places[0]
    where = f" on line {line}" if line is not None else ""
    if not places:
        raise AnnotateError(f"{path}: “{ol_push.shorten(quote)}” is not in the text on Overleaf{where}. Give the "
                            "text as it is there; pull first if Overleaf changed.")
    lines = ", ".join(str(line_of(text, start)) for start, _ in places)
    more = "give more of the text" if line is not None else "give --line or more of the text"
    raise AnnotateError(f"{path}: “{ol_push.shorten(quote)}” is in the text {len(places)} times, on lines {lines}; "
                        f"{more}.")


def numbered(text, first, last):
    lines, width = text.split("\n"), len(str(last))
    return "\n".join(f"  {n:>{width}} | {lines[n - 1]}" for n in range(first, last + 1))


def comment(api, project_id, path, quote, message, line=None, send=False):
    """Comment message on quote in the file at path. Returns {file, line, excerpt}: the lines with the
    text marked ⟦…⟧; with send, writes the comment and adds its thread ID."""
    if not message.strip():
        raise AnnotateError("The comment has no text.")
    path = clean(path)
    with ol_api.Realtime(api, project_id) as live:
        doc = doc_id(live, path)
        text, version, _ = live.join_doc(doc)
        start, end = find(text, quote, line, path)
        found = {"file": path, "line": line_of(text, start),
                 "excerpt": numbered(text[:start] + "⟦" + text[start:end] + "⟧" + text[end:],
                                     line_of(text, start), line_of(text, end))}
        if send:
            found["thread"] = ol_api.new_id()
            api.add_thread_message(project_id, found["thread"], message)
            try:
                live.add_comment(doc, found["thread"], start, text[start:end], version)
            except ol_api.OverleafError:
                api.delete_thread(project_id, doc, found["thread"])
                raise
    return found


def find_comments(api, project_id, thread_ids):
    """The comments with these thread IDs, as comments lists them, each with the ID of its doc as
    doc_id: None for a thread without a place in the text."""
    threads, docs, names = ol_review.read_live(api, project_id)
    unknown = [thread_id for thread_id in thread_ids if thread_id not in threads]
    if unknown:
        raise AnnotateError("No comment on Overleaf has these IDs: " + ", ".join(unknown) + ". comments lists the IDs.")
    doc_ids = {mark["id"]: doc["id"] for doc in api.ranges(project_id) for mark in doc["ranges"].get("comments", [])}
    found = {entry["thread"]: entry for entry in ol_review.comments(threads, docs, names, {})}
    return [{**found[thread_id], "doc_id": doc_ids.get(thread_id)} for thread_id in thread_ids]


def reply(api, project_id, thread_id, message, send=False):
    """Answer the comment thread_id with message. Returns the comment; with send, writes the answer."""
    if not message.strip():
        raise AnnotateError("The answer has no text.")
    found = find_comments(api, project_id, [thread_id])[0]
    if send:
        api.add_thread_message(project_id, thread_id, message)
    return found


def resolve(api, project_id, thread_ids, send=False):
    """Resolve these comments. Returns them; with send, resolves those that are still open."""
    found = find_comments(api, project_id, thread_ids)
    lost = [entry["thread"] for entry in found if entry["doc_id"] is None]
    if lost:
        raise AnnotateError("These comments have no place in the text, so they cannot be resolved: " + ", ".join(lost))
    for entry in found if send else []:
        if entry["status"] == "open":
            api.resolve_thread(project_id, entry["doc_id"], entry["thread"])
    return found


def suggestion_ops(text, start, end, quote, new):
    """Edits that turn text[start:end], where find found quote, into new, word by word. Where quote and
    new agree, the doc keeps its own white space, so a line break that quote gives as a space stays."""
    old = text[start:end]
    old_words, quote_words, new_words = (ol_push.TOKEN.findall(part) for part in (old, quote.strip(), new))
    target = []  # quote_words and old_words differ only in white space, word for word
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, quote_words, new_words, autojunk=False).get_opcodes():
        target += old_words[i1:i2] if tag == "equal" else new_words[j1:j2]
    ops, _ = ol_push.text_ops(old, "".join(target))
    return [{**op, "p": op["p"] + start} for op in ops]


def suggest(api, project_id, path, quote, new, line=None, send=False):
    """Suggest new instead of quote in the file at path, as a tracked change. Returns {file, line,
    excerpt}: the lines after the change with {+inserted+} and [-deleted-]; with send, writes it."""
    if any(ord(char) > 0xFFFF for char in new):
        raise AnnotateError("The new text has characters Overleaf cannot store (emoji and other characters "
                            "outside the BMP); Overleaf would turn each into two �.")
    path = clean(path)
    with ol_api.Realtime(api, project_id) as live:
        doc = doc_id(live, path)
        text, version, _ = live.join_doc(doc)
        start, end = find(text, quote, line, path)
        ops = suggestion_ops(text, start, end, quote, new)
        if not ops:
            raise AnnotateError(f"{path}: the new text is the same as the text on Overleaf.")
        after = ol_push.apply_ops(text, ops)
        found = {"file": path, "line": line_of(after, start),
                 "excerpt": numbered(ol_review.marked(after, changes=ops), line_of(after, start),
                                     line_of(after, end + len(after) - len(text)))}
        if send:
            live.apply_update(doc, ops, version, track=True)
    return found
