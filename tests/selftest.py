#!/usr/bin/env python3
"""Self-check for the recall plugin — standard library only, no Hermes install required.

    python3 tests/selftest.py

The plugin imports two Hermes modules (the @-reference base class and the plugin data directory);
both are stubbed here so the index, the extractor, the ranking, the secret denylist and the ambient
hook can be verified on a bare Python. Each test builds its own demo corpus, so nothing touches the
user's real files.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]


def _scratch(prefix: str) -> Path:
    """A temporary directory the plugin is allowed to read.

    `TMPDIR` often lives under `~/.hermes/cache/scratch` in a Hermes host, and this plugin refuses to
    read anything inside Hermes' home — correctly. So the harness builds its corpora in a system
    scratch directory instead of wherever `tempfile` happens to point.
    """
    for base in ("/tmp", "/var/tmp", tempfile.gettempdir()):
        try:
            path = Path(tempfile.mkdtemp(prefix=prefix, dir=base))
        except OSError:
            continue
        low = str(path)
        home = os.environ.get("HERMES_HOME", "").strip()
        if "/.hermes" in low or (home and low.startswith(home)):
            continue
        return path
    raise RuntimeError("recall selftest needs a scratch directory outside Hermes' home")


DATA_DIR = _scratch("recall-selftest-data-")


def _install_stubs() -> None:
    agent = types.ModuleType("agent")
    agent.__path__ = []
    refs = types.ModuleType("agent.context_references")

    class ContextReferenceProvider:
        prefix = ""
        description = ""

    class ContextCompletionItem:
        def __init__(self, text: str, display: str = "", meta: str = ""):
            self.text = text
            self.display = display or text
            self.meta = meta

    refs.ContextReferenceProvider = ContextReferenceProvider
    refs.ContextCompletionItem = ContextCompletionItem
    sys.modules["agent"] = agent
    sys.modules["agent.context_references"] = refs

    plugins = types.ModuleType("plugins")
    plugins.__path__ = []
    storage = types.ModuleType("plugins.plugin_storage")
    storage.plugin_data_dir = lambda name: DATA_DIR
    sys.modules["plugins"] = plugins
    sys.modules["plugins.plugin_storage"] = storage


def _load_plugin():
    _install_stubs()
    spec = importlib.util.spec_from_file_location(
        "recall_plugin", PLUGIN_DIR / "__init__.py", submodule_search_locations=[str(PLUGIN_DIR)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["recall_plugin"] = module
    spec.loader.exec_module(module)
    return module


def _load_corpus_builder():
    path = PLUGIN_DIR / "examples" / "make_demo_corpus.py"
    spec = importlib.util.spec_from_file_location("make_demo_corpus", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.build


recall = _load_plugin()
build_corpus = _load_corpus_builder()


class Runtime:
    """Minimal stand-in for the plugin Runtime: a connection plus settings."""

    def __init__(self, conn, **settings):
        self.conn = conn
        self._settings = settings

    def setting(self, key, default=None):
        return self._settings.get(key, default)


class RecallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = _scratch("recall-case-")
        self.corpus = self.tmp / "corpus"
        build_corpus(self.corpus)
        self.db = recall.store.connect(self.tmp / "index.db")
        self.indexed = recall.indexer.index(self.db, [self.corpus], max_files=500, budget_s=30)

    def tearDown(self):
        self.db.close()


    def test_index_finds_documents_and_answers_by_content(self):
        self.assertGreaterEqual(self.indexed["indexed"], 7, self.indexed)
        hits = recall.store.search(self.db, "acme invoice balance due", limit=3)
        self.assertTrue(hits, "no hit for the Acme invoice")
        self.assertTrue(hits[0]["path"].endswith("acme-robotics-INV-4471-2026-03.pdf"), hits[0])
        opened = recall.tools.recall_open(Runtime(self.db), {"path": hits[0]["path"]})
        self.assertIn("4 120.00 EUR", opened, opened[:400])

    def test_secrets_are_never_indexed(self):
        paths = [row["path"] for row in self.db.execute("SELECT path FROM files")]
        self.assertFalse([p for p in paths if p.endswith(".env")], paths)
        self.assertFalse([p for p in paths if "id_rsa" in p or ".ssh" in p])
        refusal = recall.tools.recall_open(Runtime(self.db), {"path": str(self.corpus / ".env")})
        self.assertIn("refused", refusal)

    def test_pdf_docx_and_xlsx_text_is_extracted(self):
        text, note = recall.extract.extract(
            self.corpus / "invoices" / "acme-robotics-INV-4471-2026-03.pdf"
        )
        self.assertIn("ACME ROBOTICS", text, note)
        docx_text, _ = recall.extract.extract(
            self.corpus / "contracts" / "supplier-agreement-acme-2026-01.docx"
        )
        self.assertIn("24 month warranty", docx_text)
        xlsx_text, _ = recall.extract.extract(self.corpus / "finance" / "expenses-2026-q1.xlsx")
        self.assertIn("Northwind Supply", xlsx_text)

    def test_unreadable_file_is_still_findable_by_name(self):
        hits = recall.store.search(self.db, "receipt scan hardware store", limit=3)
        self.assertTrue(hits and hits[0]["path"].endswith("receipt-scan-hardware-store.png"), hits)

    def test_second_run_skips_unchanged_files(self):
        again = recall.indexer.index(self.db, [self.corpus], max_files=500, budget_s=30)
        self.assertEqual(again["indexed"], 0, again)
        self.assertGreaterEqual(again["unchanged"], 7)

    def test_connection_is_usable_from_a_worker_thread(self):
        errors: list[BaseException] = []

        def work():
            try:
                recall.indexer.index(self.db, [self.corpus], max_files=500, budget_s=30)
                self.assertTrue(recall.store.search(self.db, "office lease parking", limit=3))
                self.db.commit()
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=work)
        thread.start()
        thread.join()
        self.assertFalse(errors, errors)

    def test_ambient_hook_offers_files_for_a_document_question(self):
        block = recall.hooks.build_context(
            Runtime(self.db), "please send the acme robotics invoice to legal", session_id="s1"
        )
        self.assertTrue(block.startswith("[recall]"), block)
        self.assertIn("acme-robotics-INV-4471-2026-03.pdf", block)
        self.assertLessEqual(len(block), recall.hooks.MAX_CONTEXT_CHARS)

    def test_ambient_hook_stays_quiet_for_chit_chat_and_explicit_references(self):
        runtime = Runtime(self.db)
        self.assertEqual(recall.hooks.build_context(runtime, "hey, good morning", session_id="s2"), "")
        self.assertEqual(
            recall.hooks.build_context(
                runtime, "look at @file:notes.md and tell me the invoice contract", session_id="s2"
            ),
            "",
        )

    def test_ambient_hook_ignores_machine_generated_turns(self):
        runtime = Runtime(self.db)
        for text in (
            "[[IMPORTANT]: 2 background process results arrived while this session was busy.",
            "[[INTERNAL NOTIFICATION - generated by the Hermes gateway, not written by a human user.",
            "[[OUT-OF-BAND USER MESSAGE - a direct message from the user] please read the invoice file",
        ):
            self.assertEqual(recall.hooks.build_context(runtime, text, session_id="s4"), "", text)

    def test_forget_drops_one_folder_and_then_everything(self):
        dropped = recall.store.forget_path(self.db, str(self.corpus / "invoices"))
        self.assertEqual(dropped, 2)
        paths = [row["path"] for row in self.db.execute("SELECT path FROM files")]
        self.assertFalse([p for p in paths if "/invoices/" in p], paths)
        self.assertTrue(recall.store.search(self.db, "office lease parking", limit=3))
        recall.store.wipe(self.db)
        self.assertEqual(recall.store.stats(self.db)["files"], 0)
        self.assertFalse(recall.store.search(self.db, "office lease parking", limit=3))

    def test_recall_forget_tool_reports_path_and_all_modes(self):
        import json as _json

        runtime = Runtime(self.db)
        one = _json.loads(recall.tools.recall_forget(runtime, {"path": str(self.corpus / "leases")}))
        self.assertEqual(one["files_removed"], 1)
        self.assertGreater(one["files_remaining"], 0)
        usage = _json.loads(recall.tools.recall_forget(runtime, {}))
        self.assertIn("error", usage)
        everything = _json.loads(recall.tools.recall_forget(runtime, {"all": True}))
        self.assertTrue(everything["cleared"])
        self.assertEqual(recall.store.stats(self.db)["files"], 0)

    def test_phrase_match_outranks_scattered_words(self):
        from pathlib import Path as _Path

        scatter = self.tmp / "scatter"
        scatter.mkdir()
        (scatter / "scattered.txt").write_text(
            "The price list is old.\n\nCoating options change.\n\nPowder handling notes.\n", encoding="utf-8"
        )
        tight = self.tmp / "tight"
        tight.mkdir()
        (tight / "together.txt").write_text(
            "Note: the powder coating price rose 12% versus January.\n", encoding="utf-8"
        )
        recall.indexer.index(self.db, [_Path(scatter), _Path(tight)], max_files=50, budget_s=20)
        hits = recall.store.search(self.db, "powder coating price", limit=2)
        self.assertTrue(hits)
        self.assertTrue(hits[0]["path"].endswith("together.txt"), hits)
    def test_secret_names_are_refused_even_when_innocent(self):
        """The habitual secret filenames people keep in Documents and Downloads never get indexed."""
        probe = self.tmp / "personal"
        (probe / "nested").mkdir(parents=True)
        for name in (
            "ngrok_recovery_codes.txt",
            "coinlist_authen_backupcode.txt",
            "key.txt",
            "tokens.json",
            "api.json",
            "my-api-key.md",
            "seed phrase.txt",
            "wallet.dat",
        ):
            target = probe / ("nested/" + name if " " in name else name)
            target.write_text("super secret material that must never be indexed\n", encoding="utf-8")
        (probe / "innocent.txt").write_text(
            "this one is a normal document and IS indexed\n", encoding="utf-8"
        )
        recall.indexer.index(self.db, [probe], max_files=100, budget_s=20)
        paths = [row["path"] for row in self.db.execute("SELECT path FROM files")]
        self.assertEqual(len([p for p in paths if p.endswith("innocent.txt")]), 1, paths)
        for banned in ("recovery_codes", "backupcode", "key.txt", "tokens.json", "api.json",
                       "my-api-key", "seed phrase", "wallet.dat"):
            self.assertFalse([p for p in paths if banned in p], f"{banned} was indexed: {paths}")

    def test_refresh_is_scoped_to_the_last_explicit_index(self):
        """A refresh must never widen the index to folders the user did not name."""
        runtime = Runtime(self.db)
        self.assertEqual(
            [str(r) for r in recall.refresh.refresh_roots(runtime)], [str(self.corpus.resolve())]
        )
        # A database with no recorded run refreshes nothing at all (not even the default roots).
        empty = recall.store.connect(self.tmp / "empty.db")
        empty_runtime = Runtime(empty)
        self.assertEqual(recall.refresh.refresh_roots(empty_runtime), [])
        self.assertFalse(recall.refresh.schedule(empty_runtime))
        empty.close()

    def test_ambient_hook_does_not_repeat_the_same_files(self):
        runtime = Runtime(self.db)
        message = "where is the acme robotics invoice document"
        self.assertTrue(recall.hooks.build_context(runtime, message, session_id="s3"))
        self.assertFalse(recall.hooks.build_context(runtime, message, session_id="s3"))


class HermesHomeTests(unittest.TestCase):
    """The reviewer's finding: Hermes' own state must be unreachable, on the resolved path.

    A fake home is installed for the duration of each test, so nothing here touches the real one.
    """

    def setUp(self):
        self.home = _scratch("recall-fake-home-") / ".hermes"
        (self.home / "vault").mkdir(parents=True)
        (self.home / "sessions").mkdir()
        (self.home / "mcp-tokens").mkdir()
        (self.home / "plugin-data" / "recall").mkdir(parents=True)
        (self.home / "vault" / "notes.md").write_text("VAULT-SECRET-MARKER\n", encoding="utf-8")
        (self.home / "sessions" / "2026-11-07.jsonl").write_text(
            "TRANSCRIPT-SECRET-MARKER\n", encoding="utf-8"
        )
        (self.home / "mcp-tokens" / "linear.json").write_text(
            '{"access_token": "MCP-TOKEN-MARKER"}\n', encoding="utf-8"
        )
        (self.home / "webhook_subscriptions.json").write_text(
            '{"secret": "WEBHOOK-SECRET-MARKER"}\n', encoding="utf-8"
        )
        (self.home / "state.db").write_text("SESSION-STATE-MARKER\n", encoding="utf-8")
        # A document the user legitimately wants, next to the home (the `~` case).
        self.user = self.home.parent
        (self.user / "Documents").mkdir(exist_ok=True)
        (self.user / "Documents" / "quarterly-plan.md").write_text(
            "quarterly plan: ship the local recall index\n", encoding="utf-8"
        )
        self._prev_home = os.environ.get("HERMES_HOME")
        os.environ["HERMES_HOME"] = str(self.home)
        recall.indexer.guard.forget_homes_cache()
        self.db = recall.store.connect(self.user / "index.db")
        self.runtime = Runtime(self.db)

    def tearDown(self):
        self.db.close()
        if self._prev_home is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = self._prev_home
        recall.indexer.guard.forget_homes_cache()

    def test_hermes_home_is_pruned_when_the_whole_parent_is_indexed(self):
        """`recall_index path=["~"]` is a legal request and must not walk into ~/.hermes."""
        summary = recall.indexer.index(self.db, [self.user], max_files=500, budget_s=30)
        paths = [row["path"] for row in self.db.execute("SELECT path FROM files")]
        self.assertTrue([p for p in paths if p.endswith("quarterly-plan.md")], paths)
        leaked = [p for p in paths if str(self.home) in p]
        self.assertFalse(leaked, f"Hermes home was indexed: {leaked}")
        self.assertTrue(summary["refused"], summary)
        for marker in ("VAULT-SECRET-MARKER", "MCP-TOKEN-MARKER", "WEBHOOK-SECRET-MARKER"):
            self.assertFalse(recall.store.search(self.db, marker, limit=3), marker)

    def test_open_refuses_every_kind_of_hermes_state(self):
        for target in (
            self.home / "vault" / "notes.md",
            self.home / "sessions" / "2026-11-07.jsonl",
            self.home / "mcp-tokens" / "linear.json",
            self.home / "webhook_subscriptions.json",
            self.home / "state.db",
            self.home / "plugin-data" / "recall" / "index.db",
        ):
            out = json.loads(recall.tools.recall_open(self.runtime, {"path": str(target)}))
            self.assertIn("error", out, target)
            self.assertIn("Hermes home", out["error"], target)

    def test_a_symlink_cannot_launder_a_secret_through_an_innocent_name(self):
        link = self.user / "Documents" / "notes.txt"
        link.symlink_to(self.home / "mcp-tokens" / "linear.json")
        ssh = self.user / "Documents" / "budget.txt"
        keyfile = self.user / ".ssh"
        keyfile.mkdir(exist_ok=True)
        (keyfile / "id_rsa").write_text("PRIVATE-KEY-MARKER\n", encoding="utf-8")
        ssh.symlink_to(keyfile / "id_rsa")

        for target in (link, ssh):
            out = json.loads(recall.tools.recall_open(self.runtime, {"path": str(target)}))
            self.assertIn("error", out, target)
            self.assertIn("refused", out["error"], target)

        recall.indexer.index(self.db, [self.user / "Documents"], max_files=100, budget_s=20)
        paths = [row["path"] for row in self.db.execute("SELECT path FROM files")]
        self.assertFalse([p for p in paths if p.endswith("notes.txt") or p.endswith("budget.txt")], paths)

    def test_reference_expansion_refuses_the_same_paths(self):
        ref = recall.refs.RecallReference(self.runtime)
        import asyncio

        for target in (self.home / "vault" / "notes.md", self.home / "webhook_subscriptions.json"):
            text = asyncio.run(ref.expand(str(target)))
            self.assertIn("refused", text, target)
            self.assertNotIn("VAULT-SECRET-MARKER", text)
            self.assertNotIn("WEBHOOK-SECRET-MARKER", text)
        ok = asyncio.run(ref.expand(str(self.user / "Documents" / "quarterly-plan.md")))
        self.assertIn("quarterly plan", ok)

    def test_a_root_inside_the_hermes_home_is_refused_with_a_reason(self):
        out = json.loads(recall.tools.recall_index(self.runtime, {"path": [str(self.home)]}))
        self.assertIn("error", out, out)
        self.assertIn("Hermes home", json.dumps(out["refused"]), out)

    def test_prefix_siblings_are_not_treated_as_inside_the_root(self):
        guard = recall.indexer.guard
        self.assertFalse(guard.is_relative_to(Path("/home/u/Documents-private"), Path("/home/u/Documents")))
        self.assertTrue(guard.is_relative_to(Path("/home/u/Documents/a.txt"), Path("/home/u/Documents")))

    @unittest.skipUnless(Path("/proc/self/environ").exists(), "no /proc on this platform")
    def test_virtual_filesystems_are_refused(self):
        out = json.loads(recall.tools.recall_open(self.runtime, {"path": "/proc/self/environ"}))
        self.assertIn("error", out, out)
        self.assertIn("virtual filesystem", out["error"])

    def test_default_roots_text_matches_the_code(self):
        """Reviewer finding 3: the manifest and the tool schema must not promise extra folders.

        The manifest text used to list ~/Desktop, ~/Downloads and ~/Projects as defaults while the
        code indexed ~/Documents only. This test pins the sentence that a reader takes as the default.
        """
        import re as _re

        yaml_text = (PLUGIN_DIR / "plugin.yaml").read_text(encoding="utf-8")
        block = yaml_text.split("  roots:", 1)[1].split("  excludes:", 1)[0]
        claimed_defaults = _re.search(r"Defaults? to ([^.;]+)", block)
        self.assertIsNotNone(claimed_defaults, block)
        named = set(_re.findall(r"~/[A-Za-z][A-Za-z0-9_-]*", claimed_defaults.group(1)))
        self.assertEqual(named, set(recall.indexer.DEFAULT_ROOTS), claimed_defaults.group(0))

        schema = json.dumps(recall.tools.INDEX_SCHEMA)
        self.assertIn("~/Documents", schema)
        self.assertIn("indexed only when named", schema)
        # The schema must not present the opt-in folders as defaults.
        self.assertNotRegex(schema, r"Defaults to the configured roots \(~/Documents, ")


class RootsAndRefreshTests(unittest.TestCase):
    def setUp(self):
        self.tmp = _scratch("recall-roots-")

    def test_a_folder_name_with_a_comma_survives_a_refresh(self):
        """Reviewer-adjacent: the refresh scope was a comma-joined string, so such a folder vanished."""
        folder = self.tmp / "Acme, Inc" / "contracts"
        folder.mkdir(parents=True)
        (folder / "lease.md").write_text("office lease: parking clause\n", encoding="utf-8")
        db = recall.store.connect(self.tmp / "index.db")
        recall.indexer.index(db, [self.tmp / "Acme, Inc"], max_files=50, budget_s=20)
        runtime = Runtime(db)
        self.assertEqual(
            [str(r) for r in recall.refresh.refresh_roots(runtime)], [str((self.tmp / "Acme, Inc").resolve())]
        )
        db.close()

    def test_nested_roots_are_deduplicated(self):
        outer = self.tmp / "docs"
        inner = outer / "reports"
        inner.mkdir(parents=True)
        roots = recall.indexer.resolve_roots([str(outer), str(inner), str(inner)])
        self.assertEqual([str(r) for r in roots], [str(outer.resolve())])

    def test_a_single_string_root_is_one_root_not_a_list_of_characters(self):
        docs = self.tmp / "single"
        docs.mkdir()
        self.assertEqual(
            [str(r) for r in recall.indexer.resolve_roots(str(docs))], [str(docs.resolve())]
        )


class ExtractionCapTests(unittest.TestCase):
    """A small file on disk must not turn into an unbounded allocation in the indexer."""

    def setUp(self):
        self.tmp = _scratch("recall-cap-")

    def test_an_office_member_is_read_only_up_to_the_cap(self):
        import zipfile

        target = self.tmp / "big.docx"
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("word/document.xml", "<w:t>" + "A" * 2_000 + "</w:t>")
            zf.writestr("word/bomb.xml", "<w:t>" + "A" * 5_000_000 + "</w:t>")
        with zipfile.ZipFile(target) as zf:
            # A member inside the tolerance is truncated at the cap …
            self.assertEqual(len(recall.extract._member(zf, "word/document.xml", cap=1000)), 1000)
            # … and a member far past it is refused outright instead of inflated.
            self.assertEqual(recall.extract._member(zf, "word/bomb.xml", cap=1000), "")

    def test_pdf_stream_inflation_is_capped(self):
        import zlib

        payload = zlib.compress(b"(" + b"B" * 600_000 + b")")
        target = self.tmp / "bomb.pdf"
        target.write_bytes(b"%PDF-1.4\nstream\n" + payload + b"\nendstream\n%%EOF\n")
        saved = recall.extract.MAX_INFLATE_BYTES
        recall.extract.MAX_INFLATE_BYTES = 5_000
        try:
            text, _ = recall.extract.extract(target)
        finally:
            recall.extract.MAX_INFLATE_BYTES = saved
        self.assertLessEqual(len(text), 6_000, len(text))


if __name__ == "__main__":
    unittest.main(verbosity=2)
