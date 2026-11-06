---
name: recall
description: Use when a question is about a document the user already has on this machine.
---

# Finding and citing local files

The `recall` plugin keeps a private index of the user's own documents. Use it whenever the answer
should come from a file rather than from general knowledge or the web.

## Sequence

1. **Search first.** `recall_search` with the words a human would put in the filename or the body
   ("acme invoice march", "lease parking clause"). Not a question, not a sentence — keywords.
2. **Read before quoting.** `recall_open` on the best hit. Quote from the returned text and name
   the path. A snippet is a preview, never the document.
3. **Page long documents.** `recall_open` takes `offset` and `max_chars`; keep paging instead of
   telling the user the document was truncated.
4. **Index when empty.** An empty index answers "no results" — call `recall_index` once (it is
   incremental afterwards) and retry. `recall_stats` shows coverage, roots and skip reasons.

## Rules

- Never invent a path, a date or a quote. If the search does not turn up the file, say what you
  searched for and ask the user which folder it lives in.
- A file can be in the index and still not be the right file. Check the modified date and the
  opening lines before answering from it.
- Credential stores are refused by design (`.env`, `*.pem`, `id_rsa`, `.ssh`, `.aws`, browser and
  agent auth files). Do not try to work around a refusal.
- The user can also point at a file directly with `@recall:<path-or-query>` in a message.
