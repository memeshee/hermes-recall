# recall — local file recall for Hermes Agent

Your agent can search the web in one call and your chat history in another, but it cannot find the
Acme invoice that is sitting in `~/Downloads`. `recall` fixes that: a private index of the documents
already on this machine, in the prompt where it matters.

Three surfaces, one index:

- **`recall_search` / `recall_open` tools** — keyword search over file *content* (BM25) with a
  highlighted snippet, then read the file as text (PDF, Word, Excel, PowerPoint, Markdown, CSV, code).
- **`@recall:` context reference** — type `@recall:acme invoice march` (or a path) in any message and
  the matching passages are expanded inline into that turn.
- **Ambient hints (`pre_llm_call` hook)** — when a message reads like it is about a document
  ("send the Acme invoice to legal"), the hook appends up to three candidate paths with one
  highlighted line each. The model still has to `recall_open` the file, so a hint can speed up a real
  lookup and can never invent one.

Nothing leaves the machine: no network calls, no embedding service, no third-party packages. The
index is a single SQLite database (FTS5) under `~/.hermes/plugin-data/recall/`.

## Install

```bash
hermes plugins install memeshee/hermes-recall      # from the catalog
hermes plugins enable recall
```

Then, once, in a session:

```
/recall index ~/Documents
```

or just ask: *"index my Documents folder so you can find files for me"* (the agent calls
`recall_index`). Repeat runs are incremental — unchanged files are skipped by mtime and size.

## Use

```text
you> where is the acme invoice from march, and what's the balance?
bot> [recall] Local files in the user's index that may be the document being discussed:
     - /home/me/Documents/invoices/acme-robotics-INV-4471-2026-03.pdf (modified 2026-03-14) — "BALANCE DUE 4 120.00 EUR"
     ...
     BALANCE DUE is 4 120.00 EUR, due 2026-04-13 (net 30).
     Source: /home/me/Documents/invoices/acme-robotics-INV-4471-2026-03.pdf
```

Other entry points:

| Call | What it does |
| --- | --- |
| `/recall <query>` | top matches in chat, with score and snippet |
| `/recall index [path] [--rebuild]` | index or refresh, prints counts |
| `/recall stats` | files, chunks, last run, roots, ambient state |
| `@recall:<query-or-path>` | inline expansion inside a message |
| tools | `recall_index`, `recall_search`, `recall_open`, `recall_stats` |

## What it reads, and what it refuses

Indexed: text and code files, PDFs, `.docx`, `.xlsx`, `.pptx`, OpenDocument, HTML, CSV/TSV, email
(`.eml`), subtitles. PDF text layers are read through `pypdf` when it happens to be installed and
otherwise through a standard-library stream extractor (FlateDecode streams included), so no dependency
is required for the common case.

Never read — matched by name before any file is opened, and not overridable by settings:

```
.env  *.env  .env.*  *.pem  *.key  *.p12  *.pfx  *.keystore
id_rsa*  id_ed25519*  *.kdbx  .netrc  .pgpass  .git-credentials
auth.json  credentials.json  state.db*  .npmrc  .pypirc
.ssh/  .gnupg/  .aws/  .azure/  .kube/  .docker/  .password-store/
node_modules/  .git/  .venv/  __pycache__/  dist/  build/  target/  .cache/  Library/  AppData/
```

A file whose text cannot be extracted (a scan, an image, an encrypted PDF) is still findable by the
words in its filename, and `recall_stats` reports the skip reasons so the gap is visible rather than
silent.

## Settings

Under `plugins.entries.recall.settings` (Desktop → Plugins → recall, or `hermes config set`):

| Key | Default | Meaning |
| --- | --- | --- |
| `roots` | `~/Documents, ~/Desktop, ~/Downloads, ~/Projects` | folders indexed when no path is given |
| `excludes` | `[]` | extra glob/substring patterns, on top of the built-in denylist |
| `ambient` | `true` | allow the `pre_llm_call` hint |
| `max_hints` | `3` | candidate files per turn |
| `max_file_mb` | `8` | larger files are skipped and reported |
| `chunk_chars` | `1200` | characters per indexed chunk |
| `max_files` | `20000` | files touched by one run |
| `budget_s` | `90` | wall-clock budget per run; partial runs resume next call |

## Behaviour you can rely on

- **Local only.** No network, no API key, no account, no telemetry.
- **Incremental.** Unchanged files are skipped by `(mtime, size)`; a deleted file is dropped from the
  index on the next run inside a scanned root.
- **Bounded.** A run stops at `budget_s` / `max_files` and reports `partial: true` instead of hanging
  a turn.
- **Read before quote.** Snippets are previews. The bundled skill (and the tool descriptions) tell the
  model to `recall_open` a file before quoting it, and to say what it searched for when nothing matches.
- **Quiet by default.** The ambient hook needs a document-ish cue, two content tokens, a score
  threshold, and it never shows the same path twice in a session.

## Demo corpus

```bash
python3 examples/make_demo_corpus.py /tmp/demo-corpus
```

Writes a realistic corpus — two invoices (real Flate-compressed PDF), a supplier agreement (`.docx`),
an expenses sheet (`.xlsx`), an office lease, cashflow notes, a fake scan, and a `.env` that must stay
invisible. Useful for a demo recording or to reproduce a bug without touching real documents.

## Tests

```bash
python3 tests/selftest.py     # standard library only, no Hermes install needed
```

Nine behavioural checks: content ranking, PDF/Word/Excel extraction, the credential denylist (including
`recall_open` refusing a secret path), name-only recall for an unreadable file, incremental re-index,
thread safety of the shared connection, and the ambient hook's three rules (fires for a document
question, stays quiet otherwise, does not repeat itself).

## Disclosure

`recall` reads files under the folders you point it at, stores extracted text in a local SQLite index,
and injects short excerpts of that text into the model prompt when a message looks document-related.
Everything it reads is sent to your configured model provider in the normal course of a turn — that is
the only place text leaves the machine, exactly as any other file you show the agent. It does not
modify, move or delete your files. It does not persist anything outside
`~/.hermes/plugin-data/recall/`.

MIT licensed.
