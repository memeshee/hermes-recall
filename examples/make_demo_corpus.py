#!/usr/bin/env python3
"""Build a small, realistic document corpus so `recall` can be demoed or tested without touching
the user's real files.

    python3 examples/make_demo_corpus.py ~/demo-corpus

Writes PDFs with a real Flate-compressed text layer (no third-party writer needed), a .docx, an
.xlsx, a lease in Markdown, a CSV, a fake scanned image, and a .env that must stay invisible to
the index.
"""

from __future__ import annotations

import sys
import zipfile
import zlib
from pathlib import Path


def write_pdf(path: Path, lines: list[str]) -> None:
    """A minimal PDF with a Flate-compressed content stream and extractable text."""
    content = ["BT", "/F1 11 Tf", "14 TL", "40 800 Td"]
    for line in lines:
        safe = line.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
        content.append(f"({safe}) Tj T*")
    content.append("ET")
    raw = "\n".join(content).encode("latin-1", "ignore")
    packed = zlib.compress(raw, 9)

    objects: list[bytes] = []
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>")
    objects.append(
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"
    )
    objects.append(
        b"<< /Length " + str(len(packed)).encode() + b" /Filter /FlateDecode >>\nstream\n"
        + packed + b"\nendstream"
    )
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_at}\n%%EOF\n"
    ).encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(out))


def write_docx(path: Path, paragraphs: list[str]) -> None:
    body = "".join(
        f"<w:p><w:r><w:t xml:space=\"preserve\">{p}</w:t></w:r></w:p>" for p in paragraphs
    )
    xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}</w:body></w:document>"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", xml)


def write_xlsx(path: Path, rows: list[list[str]]) -> None:
    shared = sorted({c for row in rows for c in row})
    index = {value: i for i, value in enumerate(shared)}
    strings = "".join(f"<si><t>{value}</t></si>" for value in shared)
    sheet_rows = []
    for r, row in enumerate(rows, start=1):
        cells = "".join(
            f'<c r="{"ABCDEFGH"[c]}{r}" t="s"><v>{index[value]}</v></c>'
            for c, value in enumerate(row)
        )
        sheet_rows.append(f'<row r="{r}">{cells}</row>')
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "xl/sharedStrings.xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            f'<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">{strings}</sst>',
        )
        zf.writestr(
            "xl/worksheets/sheet1.xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f"<sheetData>{''.join(sheet_rows)}</sheetData></worksheet>",
        )


def build(root: Path) -> None:
    write_pdf(
        root / "invoices" / "acme-robotics-INV-4471-2026-03.pdf",
        [
            "ACME ROBOTICS GMBH",
            "Invoice INV-4471   Date 2026-03-14   Payment terms: net 30",
            "Widget assembly line 6 - quarterly maintenance plan   2 900.00 EUR",
            "Spare part kit A-12 (servo, belt, controller)         1 220.00 EUR",
            "Subtotal                                              4 120.00 EUR",
            "VAT 0% (reverse charge, EU B2B)                         0.00 EUR",
            "BALANCE DUE                                           4 120.00 EUR",
            "IBAN DE44 5001 0517 5407 3249 31   Due date 2026-04-13",
        ],
    )
    write_pdf(
        root / "invoices" / "northwind-supply-INV-2210-2026-02.pdf",
        [
            "NORTHWIND SUPPLY CO",
            "Invoice INV-2210   Date 2026-02-02   Payment terms: net 14",
            "Steel fasteners assorted (box of 500)    318.40 EUR",
            "Powder coating, matte black (batch)      940.00 EUR",
            "BALANCE DUE                             1 258.40 EUR",
            "NOTE: price of powder coating rose 12% versus January.",
        ],
    )
    write_docx(
        root / "contracts" / "supplier-agreement-acme-2026-01.docx",
        [
            "SUPPLIER AGREEMENT",
            "Parties: Acme Robotics GmbH (Supplier) and Kite Manufacturing BV (Buyer).",
            "Term: 12 months from 2026-01-15, renewing automatically unless either party gives "
            "60 days written notice.",
            "Payment: net 30 from a valid invoice. Late payment accrues 1.5% monthly interest.",
            "Service levels: maintenance visits within 5 business days of a support ticket; "
            "two missed visits in a quarter allow the Buyer to withhold 10% of the next invoice.",
            "Warranty: spare parts carry a 24 month warranty from delivery.",
            "Confidentiality survives termination by 3 years.",
        ],
    )
    write_xlsx(
        root / "finance" / "expenses-2026-q1.xlsx",
        [
            ["date", "vendor", "item", "amount"],
            ["2026-01-09", "Acme Robotics", "maintenance plan", "2900.00"],
            ["2026-02-02", "Northwind Supply", "fasteners", "318.40"],
            ["2026-03-14", "Acme Robotics", "spare part kit A-12", "1220.00"],
        ],
    )
    (root / "leases").mkdir(parents=True, exist_ok=True)
    (root / "leases" / "office-lease-2026.md").write_text(
        "# Office lease — Unit 4, Industrieweg 22\n\n"
        "Term: 2026-04-01 to 2029-03-31. Rent 2 400 EUR per month, indexed annually to CPI.\n\n"
        "Parking: two bays (B12 and B13) are included for the tenant's vehicles. Visitor parking\n"
        "requires a permit from the landlord, issued per quarter.\n\n"
        "Subletting: not permitted without written consent. Early termination after 24 months\n"
        "carries a 3 month penalty equal to the then-current rent.\n",
        encoding="utf-8",
    )
    (root / "finance" / "cashflow-notes.txt").write_text(
        "Cashflow notes, 2026 Q1\n\n"
        "The Acme maintenance plan is our largest recurring cost at 2 900 EUR per quarter.\n"
        "Northwind raised powder-coating prices twice; ask for a fixed price for Q3.\n"
        "Parking permits for visitors cost 40 EUR each and are billed quarterly.\n",
        encoding="utf-8",
    )
    (root / "scans").mkdir(parents=True, exist_ok=True)
    (root / "scans" / "receipt-scan-hardware-store.png").write_bytes(
        bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000"
                      "1f15c4890000000a49444154789c6300010000050001")
    )
    (root / ".env").write_text("OPENAI_API_KEY=sk-should-never-be-indexed\n", encoding="utf-8")
    (root / "notes" / "passwords-please-ignore.txt").parent.mkdir(parents=True, exist_ok=True)
    (root / "notes" / "passwords-please-ignore.txt").write_text(
        "this file is deliberately a normal .txt, so it IS indexed — the denylist is by name/kind\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "demo-corpus").expanduser()
    build(target)
    print(f"demo corpus written to {target}")
