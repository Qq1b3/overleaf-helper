"""Overleaf's internal API: the calls of the web editor, as checked in docs/overleaf-api.md.

Every Overleaf URL and message format of the tool lives in this module. Overleaf does not document
these calls and can change them; a failing call raises OverleafError naming the call.
"""
import html
import json
import re
import secrets
import time

import requests

import ol_auth

BASE_URL = ol_auth.BASE_URL
PAUSE = 0.2  # seconds between two requests, to stay at a human pace
RETRY = (429, 502, 503, 504)
FRAME = re.compile(r"^(\d):([^:]*):([^:]*):?(.*)$", re.S)
OBJECT_ID = re.compile(r"\b[0-9a-f]{24}\b")


class OverleafError(Exception):
    """An Overleaf call failed or answered something unexpected."""


def meta(page, name):
    """Value of <meta name="..."> in an Overleaf page, read by its data-type; None when missing.
    A boolean is true when the tag has a content attribute, even an empty one."""
    tag = re.search(rf'<meta\b[^>]*\bname="{re.escape(name)}"[^>]*>', page)
    if not tag:
        return None
    tag = tag.group()
    kind = re.search(r'\bdata-type="([^"]*)"', tag)
    kind = kind.group(1) if kind else "string"
    if kind == "boolean":
        return re.search(r"\scontent[\s>=]", tag) is not None
    content = re.search(r'\bcontent="([^"]*)"', tag)
    if content is None:
        return None
    value = html.unescape(content.group(1))
    return json.loads(value) if kind in ("json", "number") else value


def decode(text):
    """Text from the live connection, which sends UTF-8 bytes packed into a latin1 string."""
    try:
        return text.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text


def split_frames(body):
    """socket.io 0.9 frames of one poll answer: several are sent as �<length>�<frame>..."""
    if not body.startswith("�"):
        return [body] if body else []
    frames, i = [], 0
    while i < len(body):
        j = body.index("�", i + 1)
        length = int(body[i + 1:j])
        frames.append(body[j + 1:j + 1 + length])
        i = j + 1 + length
    return frames


def tree(project):
    """Entities of joinProjectResponse's project by path: {path: (kind, id)} with kind doc, file or
    folder. The root folder has the path ""."""
    root = project["rootFolder"][0]
    entries = {"": ("folder", root["_id"])}

    def walk(folder, prefix):
        for doc in folder.get("docs", []):
            entries[prefix + doc["name"]] = ("doc", doc["_id"])
        for file in folder.get("fileRefs", []):
            entries[prefix + file["name"]] = ("file", file["_id"])
        for sub in folder.get("folders", []):
            entries[prefix + sub["name"]] = ("folder", sub["_id"])
            walk(sub, prefix + sub["name"] + "/")

    walk(root, "")
    return entries


def find_project(projects, query):
    """The project a URL, an ID or a name means: the exact name, else a unique prefix or part of it.
    Projects in the trash are left out."""
    projects = [project for project in projects if not project.get("trashed")]
    match = OBJECT_ID.search(query)
    if match:
        for project in projects:
            if project["id"] == match.group():
                return project
        raise OverleafError(f"No project with the ID {match.group()} in this account.")
    wanted = query.casefold()
    for fits in (lambda name: name == wanted, lambda name: name.startswith(wanted), lambda name: wanted in name):
        found = [project for project in projects if fits(project["name"].casefold())]
        if len(found) == 1:
            return found[0]
        if found:
            names = "\n".join(f"  {project['name']}  ({project['id']})" for project in found)
            raise OverleafError(f"Several projects match {query!r}, give the ID:\n{names}")
    raise OverleafError(f"No project matches {query!r}.")


def user_name(user):
    """Display name of a user object of the history, comments or chat."""
    if not user:
        return "unknown user"
    first = user.get("first_name") or user.get("firstName") or ""
    last = user.get("last_name") or user.get("lastName") or ""
    return f"{first} {last}".strip() or user.get("email") or "unknown user"


def _now():
    return int(time.time() * 1000)


class Overleaf:
    """HTTP calls with the stored session cookie, at a polite pace, retried on server errors."""

    def __init__(self, cookie=None):
        self.http = ol_auth.session(cookie or ol_auth.stored_cookie())
        self._csrf = None
        self._last = 0.0

    def request(self, method, path, retry=True, timeout=60, **kwargs):
        """One call; retried on server errors unless retry is False, as for writes."""
        for attempt in range(3 if retry else 1):
            pause = self._last + PAUSE - time.monotonic()
            if pause > 0:
                time.sleep(pause)
            try:
                response = self.http.request(method, BASE_URL + path, allow_redirects=False, timeout=timeout, **kwargs)
            except requests.ConnectionError as error:
                response = error
            self._last = time.monotonic()
            if isinstance(response, requests.Response) and response.status_code not in RETRY:
                break
            if attempt < 2:
                time.sleep(2 ** attempt)
        if isinstance(response, Exception):
            raise OverleafError(f"{method} {path}: {response}")
        if response.is_redirect and "/login" in response.headers.get("location", ""):
            raise ol_auth.AuthError(f"The Overleaf session has expired. {ol_auth.HOW_TO_COPY}")
        if response.status_code >= 300:
            raise OverleafError(f"{method} {path}: {response.status_code} {response.reason}")
        return response

    def json(self, path, **params):
        return self.request("GET", path, params=params).json()

    def csrf(self):
        if self._csrf is None:
            self._csrf = meta(self.request("GET", "/project").text, "ol-csrfToken")
        return self._csrf

    def projects(self):
        """All projects of the account, last changed first: id, name, owner, lastUpdated,
        lastUpdatedBy, accessLevel, archived, trashed."""
        answer = self.request("POST", "/api/project", headers={"X-Csrf-Token": self.csrf(), "Accept": "application/json"},
                              json={"filters": {}, "page": {"size": 1000},
                                    "sort": {"by": "lastUpdated", "order": "desc"}}).json()
        return answer["projects"]

    def create_project(self, name, example=False):
        """New blank project, or with the example paper of Overleaf when example is True. Returns the
        answer of the editor: project_id, owner_ref and the rest."""
        body = {"projectName": name, "template": "example"} if example else {"projectName": name}
        answer = self.write("POST", "/project/new", json=body).json()
        if not answer.get("project_id"):
            raise OverleafError("Overleaf did not return the ID of the new project.")
        return answer

    def upload_project(self, filename, zip_bytes):
        """New project from a zip, like Upload project in the editor's New project menu. Overleaf picks
        the main document and names the project after its \\title, else after the zip. Returns the
        project's ID."""
        answer = self.write("POST", "/project/new/upload", data={"name": filename}, timeout=300,
                            files={"qqfile": (filename, zip_bytes, "application/zip")}).json()
        if not answer.get("project_id"):
            raise OverleafError("Overleaf did not return the ID of the uploaded project.")
        return answer["project_id"]

    def rename_project(self, project_id, name):
        self.write("POST", f"/project/{project_id}/rename", json={"newProjectName": name})

    def download_zip(self, project_id):
        return self.request("GET", f"/project/{project_id}/download/zip").content

    def version_zip(self, project_id, version):
        return self.request("GET", f"/project/{project_id}/version/{version}/zip").content

    def updates(self, project_id):
        """The whole history, newest entry first."""
        entries, before = [], None
        while True:
            answer = self.json(f"/project/{project_id}/updates", min_count=10, **({"before": before} if before else {}))
            entries += answer["updates"]
            before = answer.get("nextBeforeTimestamp")
            if not before:
                return entries

    def diff(self, project_id, pathname, from_version, to_version):
        """Parts of one file's text between two versions: {u}, {i, meta} or {d, meta}."""
        return self.json(f"/project/{project_id}/diff", pathname=pathname,
                         **{"from": from_version, "to": to_version})["diff"]

    def labels(self, project_id):
        return self.json(f"/project/{project_id}/labels")

    def threads(self, project_id):
        return self.json(f"/project/{project_id}/threads")

    def ranges(self, project_id):
        return self.json(f"/project/{project_id}/ranges")

    def changes_users(self, project_id):
        return self.json(f"/project/{project_id}/changes/users")

    def messages(self, project_id):
        """The whole project chat, newest first."""
        found, before = [], None
        while True:
            page = self.json(f"/project/{project_id}/messages", limit=50, **({"before": before} if before else {}))
            found += page
            if len(page) < 50:
                return found
            before = min(message["timestamp"] for message in page)

    def write(self, method, path, **kwargs):
        return self.request(method, path, retry=False, headers={"X-Csrf-Token": self.csrf(), "Accept": "application/json"},
                            **kwargs)

    def upload(self, project_id, folder_id, name, content):
        """Upload a file into a folder, replacing a file of the same name there. Overleaf makes text
        files editable docs. Returns {success, entity_id, entity_type}."""
        return self.write("POST", f"/project/{project_id}/upload", params={"folder_id": folder_id}, data={"name": name},
                          files={"qqfile": (name, content, "application/octet-stream")}).json()

    def create_folder(self, project_id, parent_id, name):
        """New folder; returns it as {_id, name, docs, fileRefs, folders}."""
        return self.write("POST", f"/project/{project_id}/folder", json={"name": name, "parent_folder_id": parent_id}).json()

    def rename(self, project_id, kind, entity_id, name):
        self.write("POST", f"/project/{project_id}/{kind}/{entity_id}/rename", json={"name": name})

    def move(self, project_id, kind, entity_id, folder_id):
        self.write("POST", f"/project/{project_id}/{kind}/{entity_id}/move", json={"folder_id": folder_id})

    def delete(self, project_id, kind, entity_id):
        self.write("DELETE", f"/project/{project_id}/{kind}/{entity_id}")

    def compile(self, project_id, root_doc_id=None, draft=False):
        """Compile the project on Overleaf, like Recompile in the editor. Returns {status, outputFiles:
        [{path, url, type, build}], pdfDownloadDomain, clsiServerId, compileGroup, ...}; status is
        success when a PDF came out."""
        options = {"draft": draft, "check": "silent", "incrementalCompilesEnabled": True, "stopOnFirstError": False}
        if root_doc_id:
            options["rootDoc_id"] = root_doc_id
        return self.write("POST", f"/project/{project_id}/compile", params={"auto_compile": "false"}, timeout=300,
                          json=options).json()

    def output_file(self, compiled, path):
        """Bytes of an output file of a compile answer, such as output.pdf or output.log; None if missing.
        Output files come from the download host the answer names, not from www.overleaf.com, and only
        with the build server as clsiserverid (in lower case; clsiServerId gives 404)."""
        found = next((file for file in compiled.get("outputFiles", []) if file["path"] == path), None)
        if found is None:
            return None
        params = {"compileGroup": compiled.get("compileGroup"), "clsiserverid": compiled.get("clsiServerId")}
        response = self.http.get((compiled.get("pdfDownloadDomain") or BASE_URL) + found["url"], params=params,
                                 timeout=120)
        if response.status_code != 200:
            raise OverleafError(f"download of {path}: {response.status_code} {response.reason}")
        return response.content


class Realtime:
    """The editor's live connection: socket.io 0.9 over xhr-polling, since overleaf.com refuses the
    websocket upgrade from a script. Gives the file tree with IDs and the docs with their version."""

    def __init__(self, overleaf, project_id):
        self.http = overleaf.http
        self.project_id = project_id
        answer = self.http.get(f"{BASE_URL}/socket.io/1/", params={"projectId": project_id, "t": _now()}, timeout=60)
        if answer.status_code != 200 or answer.text.count(":") < 3:
            raise OverleafError(f"socket.io handshake: {answer.status_code} {answer.text[:100]}")
        self.url = f"{BASE_URL}/socket.io/1/xhr-polling/{answer.text.split(':')[0]}"
        self.queue, self.events, self.next_id = [], [], 1
        event = self.wait_event("joinProjectResponse", "connectionRejected")
        if event["name"] == "connectionRejected":
            raise OverleafError(f"Overleaf refused the live connection: {event['args']}")
        self.project = event["args"][0]["project"]
        self.permissions = event["args"][0].get("permissionsLevel")

    def __enter__(self):
        return self

    def __exit__(self, *exception):
        self.close()

    def send(self, frame):
        response = self.http.post(self.url, params={"projectId": self.project_id, "t": _now()}, timeout=30,
                                  data=frame.encode("utf-8"), headers={"Content-Type": "text/plain;charset=UTF-8"})
        if response.status_code != 200:
            raise OverleafError(f"socket.io send: {response.status_code} {response.text[:100]}")

    def receive(self):
        """The next frame other than connect, heartbeat or noop, as (type, data)."""
        while not self.queue:
            response = self.http.get(self.url, params={"projectId": self.project_id, "t": _now()}, timeout=60)
            if response.status_code != 200:
                raise OverleafError(f"socket.io poll: {response.status_code} {response.text[:100]}")
            for frame in split_frames(response.text):
                kind, _, _, data = FRAME.match(frame).groups()
                if kind == "2":
                    self.send("2::")
                elif kind == "0":
                    raise OverleafError("Overleaf closed the live connection.")
                elif kind == "7":
                    raise OverleafError(f"socket.io error: {data}")
                elif kind not in ("1", "8"):
                    self.queue.append((kind, data))
        return self.queue.pop(0)

    def wait_event(self, *names):
        """The next event with one of these names, also if it arrived while waiting for an answer."""
        while True:
            for i, event in enumerate(self.events):
                if event["name"] in names:
                    return self.events.pop(i)
            kind, data = self.receive()
            if kind == "5":
                self.events.append(json.loads(data))

    def call(self, name, *args):
        """Send an event and return the arguments of the server's answer."""
        msg_id, self.next_id = self.next_id, self.next_id + 1
        self.send(f"5:{msg_id}+::" + json.dumps({"name": name, "args": list(args)}))
        while True:
            kind, data = self.receive()
            if kind == "6":
                answer_id, payload = re.match(r"^(\d+)\+?(.*)$", data, re.S).groups()
                if int(answer_id) == msg_id:
                    return json.loads(payload) if payload else []
            elif kind == "5":
                self.events.append(json.loads(data))

    def join_doc(self, doc_id):
        """Open a doc: its text, version and ranges (comments, tracked changes), read at the same moment."""
        error, lines, version, _, ranges, *_ = self.call("joinDoc", doc_id, {"encodeRanges": True})
        if error:
            raise OverleafError(f"joinDoc {doc_id}: {error}")
        for comment in ranges.get("comments", []):
            comment["op"]["c"] = decode(comment["op"]["c"])
        for change in ranges.get("changes", []):
            for key in ("i", "d"):
                if key in change["op"]:
                    change["op"][key] = decode(change["op"][key])
        return "\n".join(decode(line) for line in lines), version, ranges

    def leave_doc(self, doc_id):
        self.call("leaveDoc", doc_id)

    def read_doc(self, doc_id):
        state = self.join_doc(doc_id)
        self.leave_doc(doc_id)
        return state

    def apply_update(self, doc_id, ops, version, track=False):
        """Apply edit operations [{p, i} or {p, d}] to an open doc, positions as at version; Overleaf
        merges edits that others made since then. With track they become tracked changes. Returns
        the doc's version after the update."""
        update = {"doc": doc_id, "op": ops, "v": version}
        if track:
            update["meta"] = {"tc": secrets.token_hex(9)}  # seed of the tracked change IDs
        answer = self.call("applyOtUpdate", doc_id, update)
        if answer and answer[0]:
            raise OverleafError(f"Overleaf refused the edit of doc {doc_id}: {answer[0]}")
        while True:
            event = self.wait_event("otUpdateApplied", "otUpdateError")
            if event["name"] == "otUpdateError":
                raise OverleafError(f"Overleaf refused the edit of doc {doc_id}: {event['args']}")
            applied = event["args"][0]
            if applied.get("doc") == doc_id and "op" not in applied:  # ours; others' updates carry their op
                return applied["v"] + 1

    def close(self):
        try:
            self.send("0::")
        except (requests.RequestException, OverleafError):
            pass
