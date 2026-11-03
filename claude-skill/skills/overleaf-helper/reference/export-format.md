# Export format

What the tool prints for a later LLM step. The fields below stay stable; new fields may be added.

- `timeline --json` and `history --json` read the local record that `clone` and `pull` keep in
  `.overleaf/`, so they show the project as of the last pull.
- `comments --json`, `changes --json` and `comments --inline` read Overleaf live, so their positions
  fit the text on Overleaf at that moment.

## Events

`timeline --json` prints a list of events, oldest first. `history --json` prints only the `edit` and
`label` events, newest first.

| Field | Kinds | Meaning |
|---|---|---|
| `time` | all | ISO 8601 time in UTC, for example `2026-10-29T10:35:12+00:00` |
| `kind` | all | `edit`, `comment`, `reply`, `resolve`, `suggestion`, `chat` or `label` |
| `user` | all | display name; several names joined by `, ` when an edit has several authors |
| `file` | all | path of the file a comment, reply, resolve or suggestion is about; `null` for the other kinds |
| `text` | all | the message of a comment, reply or chat message; the change of a suggestion; the name of a label; empty for `edit` and `resolve` |
| `versions` | edit | Overleaf history versions the edit spans, `"from-to"` |
| `files` | edit | paths of the edited files |
| `operations` | edit | file operations in words: `added main.tex`, `removed a.tex`, `renamed a.tex to b.tex` |
| `changes` | edit | the changed text, `[{file, line, text}]`; empty when the text was not fetched |
| `quote` | comment, reply, resolve | the highlighted text; `""` when it was deleted, `null` when the comment has no position |
| `thread` | comment, reply, resolve | ID of the comment thread |
| `deleted` | comment, reply, resolve | `true` when the thread was deleted on Overleaf after an earlier pull; missing otherwise |
| `version` | label | the history version the label names |

## Changed text

`changes[].text` and the `text` of a suggestion mark deletions as `[-text-]` and insertions as
`{+text+}`, like `git diff --word-diff`. A change keeps up to 60 characters of unchanged text of its
line on each side, cut with `…`. `line` is the line in the file after the edit.

`pull` fetches the text of the history entries it has not seen before. The text of older entries
comes with `clone --history`, or with `history --diff` for the entries it shows.

## Comments

`comments --json` prints a list of comments by file and position; threads that have no position in
the text come last, with `file` and the other position fields `null`. `--open` leaves out the
resolved ones.

| Field | Meaning |
|---|---|
| `thread` | ID of the comment thread |
| `file` | path of the file |
| `line`, `last_line` | first and last line of the highlighted text, counted from 1 |
| `offset` | position of the highlighted text in the file, in characters from the start |
| `quote` | the highlighted text; `""` when it was deleted, then `offset` is where it was |
| `context` | the lines around it: `[{line, text}]`, `--context N` lines on each side (default 1) |
| `doc_version` | Overleaf's version of the file the positions belong to |
| `same_as_last_pull` | `true` when Overleaf's text equals the last pull, so the line numbers also fit the local files |
| `status` | `open` or `resolved` |
| `messages` | the thread, oldest first: `[{user, time, text}]` |
| `resolved_by`, `resolved_at` | who resolved the thread and when; only for resolved threads |

## Suggestions

`changes --json` prints the tracked changes that wait to be accepted or rejected, by file and
position. Overleaf's text already contains a suggested insertion and no longer contains a suggested
deletion, so the text of the files reads as if every suggestion were accepted.

| Field | Meaning |
|---|---|
| `id` | ID of the tracked change; `accept ID` accepts it |
| `file`, `line`, `offset` | where it is; a deletion is at the place the deleted text was |
| `kind` | `insert` or `delete` |
| `text` | the inserted or deleted text |
| `user`, `time` | who suggested it and when |
| `marked_line` | the line with all its suggestions marked `{+inserted+}` and `[-deleted-]` |

## Inline copy

`comments --inline [DIR]` writes a copy of each file with comments or suggestions, by default into
`.overleaf/inline/`. The copy is meant for reading, not for compiling.

- A comment's highlighted text is wrapped in `⟦C1⟧…⟦/C1⟧`, numbered by file and position. A
  comment whose text was deleted is `⟦C1⟧⟦/C1⟧` at the place of the deletion. Overlapping comments
  nest as they fall: `⟦C4⟧one long ⟦C5⟧source line⟦/C4⟧, as LaTeX⟦/C5⟧`.
- Suggestions are marked `{+inserted+}` and `[-deleted-]`; a line break in deleted text becomes `↵`.
- After the line where a comment ends, `%` lines give its number, status and messages:

```latex
Open comment: ⟦C1⟧this sentence gets a comment that stays open.⟦/C1⟧
% ⟦C1⟧ open
%   Test User1 (2026-10-29 10:50): Open comment
```

- After the line of a suggestion: `% suggestion by Test User1 (2026-10-29 10:55): {+fast+}`.

The lines of the copy are the lines of the file plus the `%` lines.

## Example of the timeline

```json
[
 {
  "time": "2026-10-29T10:35:40+00:00",
  "kind": "reply",
  "user": "Test User2",
  "file": "main.tex",
  "text": "Totally agree",
  "quote": "this passage is spread over three lines\nof the source file, ...",
  "thread": "000000000000000000000017"
 },
 {
  "time": "2026-10-29T10:35:55+00:00",
  "kind": "edit",
  "user": "Test User2",
  "file": null,
  "text": "",
  "versions": "28-30",
  "files": ["sections/intro.tex"],
  "operations": [],
  "changes": [
   {"file": "sections/intro.tex", "line": 3,
    "text": "Colleague: {+this is a great sentence.+}[-please change this sentence in any way you like.-]"}
  ]
 }
]
```
