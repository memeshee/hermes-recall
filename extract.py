"""Text extraction for the recall index — standard library only for text, Office and PDF.

No OCR, no network. A PDF falls back to a plain-text scan of its content streams when pypdf is
not installed: good enough to make a scanned-free invoice or contract searchable, and it never
raises. Anything that cannot be decoded is recorded as skipped with a reason the stats tool
reports, so the index never silently loses a whole file type.
"""

from __future__ import annotations

import html
import itertools
import re
import zipfile
from pathlib import Path

TEXT_EXT = {
    ".txt", ".md", ".markdown", ".rst", ".log", ".csv", ".tsv", ".json", ".jsonl", ".yaml",
    ".yml", ".toml", ".ini", ".cfg", ".conf", ".xml", ".html", ".htm", ".py", ".js", ".ts",
    ".tsx", ".jsx", ".sh", ".bash", ".zsh", ".rb", ".go", ".rs", ".java", ".kt", ".c", ".h",
    ".cpp", ".hpp", ".cs", ".php", ".sql", ".tex", ".org", ".srt", ".vtt", ".eml", ".ics",
}
OFFICE_EXT = {".docx", ".xlsx", ".pptx", ".odt", ".ods", ".odp"}
PDF_EXT = {".pdf"}

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t\u00a0]+")
_BLANKS = re.compile(r"\n{3,}")


def _clean(text: str) -> str:
    text = _TAG.sub(" ", text)
    text = html.unescape(text)
    text = _WS.sub(" ", text)
    text = _BLANKS.sub("\n\n", text)
    return text.strip()


_SI_BLOCK = re.compile(r"<si\b[^>]*>(.*?)</si>", re.S)
_T_NODE = re.compile(r"<t\b[^>]*>(.*?)</t>", re.S)
_WT_NODE = re.compile(r"<w:t\b[^>]*>(.*?)</w:t>", re.S)
_A_T_NODE = re.compile(r"<a:t\b[^>]*>(.*?)</a:t>", re.S)
_TEXT_NODE = re.compile(r"<text:[pt]\b[^>]*>(.*?)</text:[pt]>", re.S)
_NUM_NODE = re.compile(r"<v\b[^>]*>(-?\d+(?:\.\d+)?)</v>", re.S)

# A document that is 200 KB on disk can inflate to gigabytes: an OOXML member or a PDF stream is
# attacker-shaped input the moment it comes from a download folder. Both readers stop at a cap
# instead of trusting the archive's own idea of its size.
MAX_MEMBER_BYTES = 8 * 1024 * 1024
MAX_INFLATE_BYTES = 4 * 1024 * 1024
MAX_PDF_STREAMS = 4000


def _member(zf: zipfile.ZipFile, name: str, cap: int = MAX_MEMBER_BYTES) -> str:
    """Read one archive member, refusing to decompress past `cap` bytes."""
    info = zf.getinfo(name)
    if info.file_size > cap * 4:
        return ""
    with zf.open(name) as fh:
        return fh.read(cap).decode("utf-8", "ignore")


def _xml_text(raw: str) -> str:
    return html.unescape(_TAG.sub(" ", raw)).strip()


def _office_text(path: Path) -> str:
    """Pull readable text out of an OOXML/ODF container without third-party packages.

    Each format is parsed by its own text nodes rather than by blanket tag stripping: a spreadsheet
    becomes one cell per line (`sharedStrings` entries, which are the values a human typed) and a
    document becomes one paragraph per line, so a search snippet reads like the source instead of a
    wall of words.
    """
    suffix = path.suffix.lower()
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        if suffix in {".xlsx", ".ods"}:
            cells: list[str] = []
            for name in names:
                if "sharedStrings" in name:
                    blob = _member(zf, name)
                    for si in _SI_BLOCK.findall(blob):
                        value = " ".join(_xml_text(t) for t in _T_NODE.findall(si)).strip()
                        if value:
                            cells.append(value)
                elif re.search(r"sheet\d*\.xml$|content\.xml$", name):
                    blob = _member(zf, name)
                    for node in _TEXT_NODE.findall(blob):
                        value = _xml_text(node)
                        if value:
                            cells.append(value)
                    for node in _NUM_NODE.findall(blob):
                        cells.append(node)
            return " | ".join(cells[:4000])

        wanted: list[str] = []
        if suffix in {".pptx", ".odp"}:
            wanted = [n for n in names if re.search(r"slides?/.*\.xml$|content\.xml$", n)]
        else:
            wanted = [n for n in names if re.search(r"word/document.*\.xml$|content\.xml$", n)]
        if not wanted:
            wanted = [n for n in names if n.endswith(".xml")][:8]

        parts: list[str] = []
        for name in wanted[:60]:
            try:
                blob = _member(zf, name)
            except (KeyError, RuntimeError, OSError):
                continue
            paragraphs = (
                _WT_NODE.findall(blob)
                or _A_T_NODE.findall(blob)
                or _TEXT_NODE.findall(blob)
            )
            if paragraphs:
                lines = []
                for node in paragraphs:
                    value = _xml_text(node)
                    if value:
                        lines.append(value)
                if lines:
                    parts.append("\n".join(lines))
                    continue
            cleaned = _clean(blob)
            if cleaned:
                parts.append(cleaned)
    return "\n".join(p for p in parts if p)



def _pdf_text(path: Path) -> tuple[str, str]:
    try:
        import pypdf  # type: ignore

        reader = pypdf.PdfReader(str(path))
        pages = []
        for page in reader.pages[:200]:
            try:
                pages.append(page.extract_text() or "")
            except Exception:
                continue
        text = _clean("\n".join(pages))
        if text:
            return text, ""
    except Exception:
        pass
    # Fallback: pull literal strings out of the content streams, inflating FlateDecode streams with
    # the standard library. Covers most digitally-created PDFs without a third-party dependency.
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return "", f"unreadable: {exc.__class__.__name__}"

    import zlib

    def _inflate(blob: bytes) -> str | None:
        """Decompress a stream with a hard output ceiling (a PDF bomb must not eat the index run)."""
        try:
            obj = zlib.decompressobj()
            out = obj.decompress(blob, MAX_INFLATE_BYTES)
            return out.decode("latin-1", "ignore")
        except zlib.error:
            return None

    pieces: list[str] = []
    for match in itertools.islice(re.finditer(rb"stream\r?\n(.{16,}?)endstream", raw, re.S), MAX_PDF_STREAMS):
        blob = match.group(1)
        for candidate in (blob, blob.rstrip(b"\r\n")):
            inflated = _inflate(candidate)
            if inflated is not None:
                pieces.append(inflated)
                break
        else:
            pieces.append(blob[:MAX_INFLATE_BYTES].decode("latin-1", "ignore"))
    text_sources = pieces or [raw[:MAX_INFLATE_BYTES].decode("latin-1", "ignore")]

    found: list[str] = []
    for source in text_sources:
        for literal in re.findall(r"\(((?:[^()\\]|\\.){2,400})\)", source):
            found.append(literal.replace("\\(", "(").replace("\\)", ")"))
        for hexstr in re.findall(r"<([0-9A-Fa-f]{8,800})>", source):
            try:
                found.append(bytes.fromhex(hexstr).decode("utf-16-be", "ignore") if len(hexstr) % 4 == 0
                             else bytes.fromhex(hexstr).decode("latin-1", "ignore"))
            except ValueError:
                continue
    text = _clean(" ".join(found))
    if len(text) < 24:
        return "", "pdf: no extractable text layer (scanned image or encrypted)"
    return text, "pdf: stdlib stream extractor"



def extract(path: Path, max_bytes: int = 8 * 1024 * 1024) -> tuple[str, str]:
    """Return (text, note). Note is a non-empty skip reason when text is empty."""
    try:
        st = path.stat()
    except OSError as exc:
        return "", f"unreadable: {exc.__class__.__name__}"
    if st.st_size == 0:
        return "", "empty file"
    if st.st_size > max_bytes:
        return "", f"larger than {max_bytes // (1024 * 1024)} MB"
    suffix = path.suffix.lower()
    if suffix in PDF_EXT:
        return _pdf_text(path)
    if suffix in OFFICE_EXT:
        try:
            text = _office_text(path)
        except (zipfile.BadZipFile, OSError, RuntimeError) as exc:
            return "", f"office extract failed: {exc.__class__.__name__}"
        return (text, "") if text else ("", "office: no text")
    if suffix and suffix not in TEXT_EXT:
        return "", f"unsupported type {suffix}"
    try:
        data = path.read_bytes()[:max_bytes]
    except OSError as exc:
        return "", f"unreadable: {exc.__class__.__name__}"
    if b"\x00" in data[:2048]:
        return "", "binary content"
    text = data.decode("utf-8", "ignore")
    if suffix in {".html", ".htm", ".xml"}:
        text = _clean(text)
    return (text.strip(), "") if text.strip() else ("", "no text")


def chunk_text(text: str, size: int = 1200, overlap: int = 150) -> list[str]:
    """Paragraph-first chunking with a hard character ceiling and bounded overlap."""
    text = text.strip()
    if not text:
        return []
    size = max(300, int(size))
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: list[str] = []
    buf = ""
    for para in paragraphs:
        while len(para) > size:
            head, para = para[:size], para[size - overlap if size > overlap else size:]
            if buf:
                chunks.append(buf)
                buf = ""
            chunks.append(head)
        if not buf:
            buf = para
        elif len(buf) + 2 + len(para) <= size:
            buf = buf + "\n\n" + para
        else:
            chunks.append(buf)
            tail = buf[-overlap:] if overlap and len(buf) > overlap else ""
            buf = (tail + "\n\n" + para).strip() if tail else para
    if buf:
        chunks.append(buf)
    return [c for c in chunks if c.strip()]
