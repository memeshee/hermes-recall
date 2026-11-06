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
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = Path(tempfile.mkdtemp(prefix="recall-selftest-"))


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
        self.tmp = Path(tempfile.mkdtemp(prefix="recall-case-"))
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
        self.assertLess(len(block), 1000)

    def test_ambient_hook_stays_quiet_for_chit_chat_and_explicit_references(self):
        runtime = Runtime(self.db)
        self.assertEqual(recall.hooks.build_context(runtime, "hey, good morning", session_id="s2"), "")
        self.assertEqual(
            recall.hooks.build_context(
                runtime, "look at @file:notes.md and tell me the invoice contract", session_id="s2"
            ),
            "",
        )

    def test_ambient_hook_does_not_repeat_the_same_files(self):
        runtime = Runtime(self.db)
        message = "where is the acme robotics invoice document"
        self.assertTrue(recall.hooks.build_context(runtime, message, session_id="s3"))
        self.assertFalse(recall.hooks.build_context(runtime, message, session_id="s3"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
