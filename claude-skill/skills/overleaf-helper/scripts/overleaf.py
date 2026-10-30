"""Overleaf helper: pull and push Overleaf projects, show changes, history and comments.

    python overleaf.py login [--clipboard]    store the overleaf_session2 cookie copied from Chrome
    python overleaf.py whoami                 check that the stored cookie still works
    python overleaf.py logout                 delete the stored cookie
    python overleaf.py projects [--all]       projects of the account, last changed first
    python overleaf.py create NAME [DIR] [--example | --from FOLDER]
                                              new project on Overleaf and its working copy
    python overleaf.py clone PROJECT [DIR] [--history]
                                              new working copy; PROJECT is a name, an ID or a URL
    python overleaf.py pull                   bring the changes on Overleaf into main
    python overleaf.py status                 files changed locally, on Overleaf, or both
    python overleaf.py diff [--incoming | --from V --to V] [--word]
    python overleaf.py history [FILE] [--since DATE] [--user NAME] [--diff] [--json]
    python overleaf.py timeline [--since DATE] [--user NAME] [--file FILE] [--md | --json]
    python overleaf.py comments [--open] [--context N] [--md | --json | --inline [DIR]]
    python overleaf.py changes [--json]       tracked changes waiting to be accepted or rejected
    python overleaf.py push [--yes] [--track] send the local edits; a dry run without --yes
    python overleaf.py compile [--draft] [--out DIR]
                                              compile on Overleaf, fetch the PDF and sum up the log
    python overleaf.py replay                 the history as git commits with their authors, for
                                              git blame and git log on the branch history
Commands on a working copy run inside it or take -C DIR. clone and pull keep a record of the
history, comments, tracked changes and chat in .overleaf/, which history and timeline read;
comments and changes read Overleaf live.
Needs git, requests and keyring (pip install requests keyring).
"""
import argparse
import datetime
import sys
from pathlib import Path

import requests

import ol_api
import ol_auth
import ol_compile
import ol_create
import ol_push
import ol_review
import ol_sync


INLINE = (ol_review.RECORD / "inline").as_posix()


def when(iso):
    return datetime.datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d %H:%M")


def since(text):
    """Local day or time from the command line, such as 2026-10-29 or "2026-10-29 14:00"."""
    return datetime.datetime.fromisoformat(text).astimezone() if text else None


def record_summary(counts):
    skipped = (f"\nOverleaf did not give the text of {counts['skipped']} history entries; the next pull tries "
               "again.") if counts["skipped"] else ""
    return (f"Saved {counts['entries']} history entries, {counts['threads']} comment threads and "
            f"{counts['chat']} chat messages in {ol_review.RECORD.as_posix()}/{skipped}")


def record_root(args):
    root = ol_sync.find_root(args.C)
    if not (root / ol_review.RECORD / "history.json").is_file():
        sys.exit("No record of the history yet: run pull first.")
    return root


def print_table(header, rows):
    widths = [max(len(str(row[i])) for row in [header, *rows]) for i in range(len(header))]
    for row in [header, *rows]:
        print("  ".join(str(cell).ljust(width) for cell, width in zip(row, widths)).rstrip())


def print_changes(title, found):
    print(f"\n{title}:")
    for status, path in found or [("", "none")]:
        print(f"  {status} {path}".rstrip() if status else f"  {path}")


def cmd_login(args):
    print(f"Logged in as {ol_auth.login(args.clipboard)}. Cookie stored in the system credential store.")


def cmd_whoami(args):
    print(f"Logged in as {ol_auth.whoami()}.")


def cmd_logout(args):
    ol_auth.logout()
    print("Stored cookie deleted.")


def cmd_projects(args):
    projects = [project for project in ol_api.Overleaf().projects()
                if args.all or not (project.get("archived") or project.get("trashed"))]
    print_table(("Name", "Last change", "By", "Owner", "Access", "ID"),
                [(project["name"], when(project["lastUpdated"]), ol_api.user_name(project.get("lastUpdatedBy")),
                  ol_api.user_name(project.get("owner")), project["accessLevel"], project["id"])
                 for project in projects])


def cmd_clone(args):
    api = ol_api.Overleaf()
    project = ol_api.find_project(api.projects(), args.project)
    folder = Path(args.dir or ol_create.folder_name(project["name"]))
    state = ol_sync.clone(api, project, folder)
    state["text_since"] = 0 if args.history else state["version"]  # history entries that get their text
    ol_sync.save_state(folder, state)
    counts = ol_review.update_record(api, folder, project["id"], state["text_since"])
    print(f"Cloned {project['name']} (version {state['version']}) into {folder}")
    print(record_summary(counts))


def cmd_create(args):
    api = ol_api.Overleaf()
    project, state, sent, missing = ol_create.create(api, args.name, args.example, args.dir, args.source)
    folder = Path(args.dir or ol_create.folder_name(project["name"]))
    origin = f" from {sent} files of {args.source}" if args.source else ""
    print(f"Created {project['name']} on Overleaf ({ol_api.BASE_URL}/project/{project['id']}){origin} "
          f"and cloned it into {folder}")
    if missing:
        print(f"Not in the project ({len(missing)}): " + ", ".join(missing))
    print(record_summary(ol_review.update_record(api, folder, project["id"], state["text_since"])))


def cmd_pull(args):
    api = ol_api.Overleaf()
    root = ol_sync.find_root(args.C)
    state, outcome = ol_sync.pull(api, root)
    counts = ol_review.update_record(api, root, state["project_id"], state.get("text_since", 0))
    if outcome == "unchanged":
        print(f"No changes on Overleaf since the last pull (version {state['version']}).")
    elif outcome == "merged":
        print(f"Pulled version {state['version']} and merged it into {ol_sync.WORK}.")
    print(record_summary(counts))
    if outcome not in ("unchanged", "merged"):
        files = "\n".join(f"  {path}" for path in outcome)
        sys.exit(f"Pulled version {state['version']}, but these files have merge conflicts:\n{files}\n"
                 "Resolve the conflict markers, then git add the files and git commit.")


def cmd_status(args):
    state, local, incoming = ol_sync.status(ol_api.Overleaf(), ol_sync.find_root(args.C))
    print(f"{state['name']}: version {state['version']}, pulled {when(state['pulled_at'])}")
    print_changes("Changed locally, not on Overleaf yet", local)
    print_changes("Changed on Overleaf since the last pull", incoming)
    both = sorted({path for _, path in local} & {path for _, path in incoming})
    if both:
        print_changes("Changed in both, pull merges them (conflicts possible)", [("", path) for path in both])


def cmd_diff(args):
    if (args.from_version is None) != (args.to_version is None):
        sys.exit("Give both --from and --to.")
    versions = (args.from_version, args.to_version) if args.from_version is not None else None
    api = ol_api.Overleaf() if args.incoming or versions else None
    text = ol_sync.diff(ol_sync.find_root(args.C), api, incoming=args.incoming, versions=versions, word=args.word)
    print(text, end="") if text else print("No differences.")


def with_text(root, choose):
    """The events choose() selects, after fetching the changed text that their edits still lack.
    Without a connection they come without that text, and a note says so."""
    found = choose()
    missing = ol_review.missing_text(root, found)
    if missing:
        try:
            skipped = ol_review.fetch_diffs(ol_api.Overleaf(), root, ol_sync.load_state(root)["project_id"], missing)
        except (ol_auth.AuthError, ol_api.OverleafError, requests.RequestException) as error:
            print(f"The changed text of {len(missing)} edits is missing: {error}", file=sys.stderr)
            return found
        if skipped:
            print(f"Overleaf did not give the changed text of {skipped} edits.", file=sys.stderr)
        found = choose()
    return found


def cmd_history(args):
    root = record_root(args)

    def choose():
        return ol_review.select(ol_review.events(root), since(args.since), args.user, args.file, {"edit", "label"})

    found = (with_text(root, choose) if args.diff else choose())[::-1]
    if args.json:
        print(ol_review.as_json(found))
    else:
        print(ol_review.as_text(found, changes=args.diff) or "No history entries match.")


def cmd_timeline(args):
    root = record_root(args)

    def choose():
        return ol_review.select(ol_review.events(root), since(args.since), args.user, args.file)

    found = with_text(root, choose)
    state = ol_sync.load_state(root)
    if args.json:
        print(ol_review.as_json(found))
    elif args.md:
        print(ol_review.as_markdown(found, f"Timeline of {state['name']} as of the pull at {when(state['pulled_at'])}"),
              end="")
    else:
        print(ol_review.as_text(found) or "No events match.")


def cmd_comments(args):
    root = ol_sync.find_root(args.C)
    state = ol_sync.load_state(root)
    threads, docs, names = ol_review.read_live(ol_api.Overleaf(), state["project_id"])
    last_pull = {path: ol_sync.snapshot_text(root, path) for path in docs}
    found = ol_review.comments(threads, docs, names, last_pull, args.context)
    if args.open:
        found = [comment for comment in found if comment["status"] == "open"]
    changed = sorted({comment["file"] for comment in found if comment["file"] and not comment["same_as_last_pull"]})
    if changed:
        print(f"Changed on Overleaf since the last pull: {', '.join(changed)}. Line numbers refer to the "
              "text on Overleaf; pull to bring the local files up to date.", file=sys.stderr)
    if args.inline:
        target = root / args.inline if args.inline == INLINE else Path(args.inline)
        for file in ol_review.write_inline(target, docs, found, ol_review.suggestions(docs, names)):
            print(file)
    elif args.json:
        print(ol_review.as_json(found))
    elif args.md:
        print(ol_review.comments_as_markdown(found, f"Comments in {state['name']}, read from Overleaf at "
                                                    f"{datetime.datetime.now():%Y-%m-%d %H:%M}"), end="")
    else:
        print(ol_review.comments_as_text(found) or "No comments.")


def cmd_push(args):
    api = ol_api.Overleaf()
    root = ol_sync.find_root(args.C)
    result = ol_push.push(api, root, send_it=args.yes, track=args.track)
    if not result["steps"]:
        print("Nothing to push: the working copy has no changes against the last pull.")
        return
    print("Push to Overleaf" + (", as tracked changes" if args.track else "") + ":")
    print("\n".join(f"  {step}" for step in result["steps"]))
    for title, lines in (("Warnings", result["warnings"]), ("Errors", result["errors"])):
        if lines:
            print(f"\n{title}:\n" + "\n".join(f"  {line}" for line in lines))
    if result["errors"]:
        sys.exit("\nNothing was sent. Fix the errors, then push again.")
    if "state" not in result:
        print(f"\n{result['diff']}", end="")
        print(f"\nDry run: nothing was sent. To send it: push --yes{' --track' if args.track else ''}")
        return
    state = result["state"]
    counts = ol_review.update_record(api, root, state["project_id"], state.get("text_since", 0))
    print(f"\nPushed. Overleaf is at version {state['version']}.")
    if result["outcome"] not in ("unchanged", "merged"):
        print("The pull after the push has merge conflicts in: " + ", ".join(result["outcome"]))
    if result["left"]:
        print("These files still differ from Overleaf: " + ", ".join(result["left"]))
    print(record_summary(counts))


def cmd_compile(args):
    root = ol_sync.find_root(args.C)
    status, saved, summary, unpushed = ol_compile.compile_project(ol_api.Overleaf(), root,
                                                                  Path(args.out) if args.out else None, args.draft)
    pages = f", {summary['pages']} pages" if summary and summary["pages"] else ""
    print(f"Compiled on Overleaf: {status}{pages}.")
    print("\n".join(f"  {path}" for path in saved.values()))
    if summary:
        print(f"Log: {len(summary['errors'])} errors, {len(summary['warnings'])} warnings, "
              f"{summary['boxes']} overfull or underfull boxes")
        for kind in ("errors", "warnings"):
            for item in summary[kind][:10]:
                where = (item["file"] or "?") + (f":{item['line']}" if item["line"] else "")
                print(f"  {kind[:-1]:<7}  {where}  {item['message']}")
            if len(summary[kind]) > 10:
                print(f"  … {len(summary[kind]) - 10} more {kind} in the log")
    if unpushed:
        print("Not in this PDF, since not pushed yet: " + ", ".join(unpushed))
    if status != "success":
        sys.exit(f"Overleaf's compile ended with: {status}")


def cmd_replay(args):
    root = record_root(args)
    commits = ol_review.replay(ol_api.Overleaf(), root, ol_sync.load_state(root)["project_id"])
    if not commits:
        print("The record has no history with text to replay.")
        return
    print(f"Replayed the history as {commits} commits on the branch {ol_review.REPLAY}.")
    differ = ol_sync.git(root, "diff", "--name-only", "--diff-filter=MD", ol_review.REPLAY, ol_sync.SNAPSHOTS).splitlines()
    print("Its text equals the last pull." if not differ else
          f"Its text differs from the last pull in {', '.join(differ)}; Overleaf's history can lag a few minutes.")
    counts = ol_review.line_authors(root)
    print("Lines of the text by author: " + ", ".join(f"{name} {count}" for name, count in counts.most_common()))
    print(f"Who wrote which line: git blame {ol_review.REPLAY} -- FILE; every change: git log -p {ol_review.REPLAY}")


def cmd_changes(args):
    state = ol_sync.load_state(ol_sync.find_root(args.C))
    _, docs, names = ol_review.read_live(ol_api.Overleaf(), state["project_id"])
    found = ol_review.suggestions(docs, names)
    print(ol_review.as_json(found) if args.json else ol_review.suggestions_as_text(found) or "No tracked changes.")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Pull and push Overleaf projects, show history and comments.")
    commands = parser.add_subparsers(dest="command", required=True)
    login = commands.add_parser("login", help="store the overleaf_session2 cookie copied from Chrome")
    login.add_argument("--clipboard", action="store_true",
                       help="take the cookie from the clipboard instead of a hidden prompt, then clear it")
    commands.add_parser("whoami", help="check that the stored cookie still works")
    commands.add_parser("logout", help="delete the stored cookie (the Overleaf session stays valid)")
    projects = commands.add_parser("projects", help="projects of the account, last changed first")
    projects.add_argument("--all", action="store_true", help="include archived and trashed projects")
    create = commands.add_parser("create", help="new project on Overleaf, blank, the example paper or a local folder, and its working copy")
    create.add_argument("name", help="name of the new project")
    create.add_argument("dir", nargs="?", help="folder for the working copy (default: the project name)")
    start = create.add_mutually_exclusive_group()
    start.add_argument("--example", action="store_true", help="start from Overleaf's example paper instead of blank")
    start.add_argument("--from", dest="source", metavar="FOLDER",
                       help="upload the files of a local folder, without build files and what its .gitignore ignores "
                            "unless git tracks it")
    clone = commands.add_parser("clone", help="new working copy of a project")
    clone.add_argument("project", help="name, unique start of the name, ID or URL of the project")
    clone.add_argument("dir", nargs="?", help="folder for the working copy (default: the project name)")
    clone.add_argument("--history", action="store_true",
                       help="also fetch the text of every change in the history (one request per file and entry)")
    working_copy = argparse.ArgumentParser(add_help=False)
    working_copy.add_argument("-C", default=".", metavar="DIR", help="the working copy (default: here)")
    commands.add_parser("pull", parents=[working_copy], help="bring the changes on Overleaf into main")
    commands.add_parser("status", parents=[working_copy], help="files changed locally, on Overleaf, or both")
    diff = commands.add_parser("diff", parents=[working_copy],
                               help="local edits not yet on Overleaf, or the changes on Overleaf")
    diff.add_argument("--incoming", action="store_true", help="changes on Overleaf since the last pull")
    diff.add_argument("--from", dest="from_version", type=int, metavar="V", help="Overleaf history version")
    diff.add_argument("--to", dest="to_version", type=int, metavar="V", help="Overleaf history version")
    diff.add_argument("--word", action="store_true", help="show changed words instead of changed lines")
    filters = argparse.ArgumentParser(add_help=False)
    filters.add_argument("--since", metavar="DATE", help='from this day or time on: 2026-10-29 or "2026-10-29 14:00"')
    filters.add_argument("--user", metavar="NAME", help="only people whose name contains NAME")
    history = commands.add_parser("history", parents=[working_copy, filters],
                                  help="who changed which files when, as of the last pull")
    history.add_argument("file", nargs="?", help="only the changes of this file")
    history.add_argument("--diff", action="store_true", help="show the changed text, fetching what is missing")
    history.add_argument("--json", action="store_true", help="the entries as JSON")
    timeline = commands.add_parser("timeline", parents=[working_copy, filters],
                                   help="edits with their text, comments, suggestions, chat and named versions in "
                                        "time order; fetches the text the record still lacks")
    timeline.add_argument("--file", help="only events about this file")
    output = timeline.add_mutually_exclusive_group()
    output.add_argument("--md", action="store_true", help="as Markdown, grouped by day")
    output.add_argument("--json", action="store_true", help="the events as JSON")
    comments = commands.add_parser("comments", parents=[working_copy],
                                   help="comments with the text they refer to, read live from Overleaf")
    comments.add_argument("--open", action="store_true", help="only open comments")
    comments.add_argument("--context", type=int, default=1, metavar="N",
                          help="lines of text around each comment (default 1)")
    output = comments.add_mutually_exclusive_group()
    output.add_argument("--md", action="store_true", help="as Markdown, one section per comment")
    output.add_argument("--json", action="store_true", help="as JSON")
    output.add_argument("--inline", nargs="?", const=INLINE, metavar="DIR",
                        help=f"write copies of the files with comments and suggestions marked in the text "
                             f"(default {INLINE})")
    changes = commands.add_parser("changes", parents=[working_copy],
                                  help="tracked changes waiting to be accepted or rejected, read live")
    changes.add_argument("--json", action="store_true", help="as JSON")
    push = commands.add_parser("push", parents=[working_copy],
                               help="send the local edits to Overleaf; without --yes only a dry run")
    push.add_argument("--yes", action="store_true", help="really send it")
    push.add_argument("--track", action="store_true", help="send the text edits as tracked changes for review")
    compile_parser = commands.add_parser("compile", parents=[working_copy],
                                         help="compile on Overleaf, fetch the PDF and sum up the log")
    compile_parser.add_argument("--out", metavar="DIR", help=f"folder for output.pdf and output.log "
                                                             f"(default {ol_compile.OUTPUT.as_posix()})")
    compile_parser.add_argument("--draft", action="store_true", help="draft mode: faster, images as boxes")
    commands.add_parser("replay", parents=[working_copy],
                        help=f"the history as git commits with their authors on the branch {ol_review.REPLAY}")
    args = parser.parse_args()

    try:
        globals()[f"cmd_{args.command}"](args)
    except (ol_auth.AuthError, ol_api.OverleafError, ol_sync.SyncError, ol_push.PushError) as error:
        sys.exit(str(error))
    except requests.RequestException as error:
        sys.exit(f"Could not reach Overleaf: {error}")


if __name__ == "__main__":
    main()
