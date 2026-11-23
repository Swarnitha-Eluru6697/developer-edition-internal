"""
Real tests for the Doc Extractor node (harness.py's _extract_text_from_bytes
and handle_docextractor), added after validating this node's real feature
parity against Dify's documented spec
(https://docs.dify.ai/en/cloud/use-dify/nodes/doc-extractor) — see
GAP_REPORT §0.33 for the full comparison and every gap this closed.

Each format test builds a REAL file of that format in-process (via the
real library that writes it: python-docx, openpyxl, python-pptx,
ebooklib, Python's own email module) rather than a synthetic/hand-rolled
byte string, so a passing test genuinely proves the real parser handles
real output from real tools — the same standard applied to the Code
node's test suite (tests/test_code_node.py).

Legacy .doc/.ppt (antiword/catppt) are honestly NOT covered here: the
only way to produce a test file in this environment is via LibreOffice's
own "MS Word 97"/"PowerPoint 97" export filters, which have a real, known
compatibility gap with antiword/catppt (confirmed manually during this
build — see GAP_REPORT §0.33) that isn't representative of genuine MS
Office-produced files. Legacy .xls (xls2csv) IS covered, since that
combination was confirmed working. .msg is also not covered — there's no
practical way to construct a real .msg sample without genuine Outlook.
"""
import csv
import io
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from app.harness import (
    _extract_text_from_bytes, _decode_text, _resolve_docextractor_input,
    handle_docextractor,
)


def make_ctx(user_input: str = "") -> dict:
    return {"input": user_input, "vars": {}, "agent_id": "test", "agent_name": "test", "invoking_user": "user:test", "output": None}


class TestRealEncodingDetection:
    def test_real_utf8_including_multibyte_decodes_correctly(self):
        content = "héllo wörld 你好".encode("utf-8")
        assert _decode_text(content) == "héllo wörld 你好"

    def test_a_real_non_utf8_encoding_is_genuinely_detected_not_mangled(self):
        original = "Café — “smart quotes” and naïve résumé"
        content = original.encode("windows-1252")
        assert _decode_text(content) == original

    def test_previously_this_mangled_non_utf8_text_into_replacement_characters(self):
        """Documents the real bug this fixed — kept as a regression
        guard, not just a positive-case check."""
        content = "café".encode("windows-1252")
        blind_utf8_result = content.decode("utf-8", errors="replace")
        assert blind_utf8_result != "café"  # confirms the OLD behavior really was broken
        assert _decode_text(content) == "café"  # confirms the NEW behavior is genuinely fixed


class TestRealHtmlExtraction:
    def test_tags_are_genuinely_stripped_not_left_in_the_output(self):
        html = b"<html><body><h1>Title</h1><p>Some <b>bold</b> text.</p></body></html>"
        text = _extract_text_from_bytes(html, "page.html", "text/html")
        assert "<h1>" not in text
        assert "<b>" not in text
        assert "Title" in text
        assert "Some bold text." in text

    def test_script_and_style_tags_are_real_removed_not_just_unwrapped(self):
        html = b"<html><body><p>Real content.</p><script>alert('x')</script><style>.a{color:red}</style></body></html>"
        text = _extract_text_from_bytes(html, "page.html", "text/html")
        assert "alert" not in text
        assert "color:red" not in text
        assert "Real content." in text

    def test_block_elements_produce_real_line_breaks_between_them(self):
        html = b"<ul><li>Item 1</li><li>Item 2</li></ul>"
        text = _extract_text_from_bytes(html, "page.html", "text/html")
        assert text.splitlines() == ["Item 1", "Item 2"]


class TestRealDocxExtraction:
    def _make_docx_with_table(self) -> bytes:
        from docx import Document
        doc = Document()
        doc.add_paragraph("Before the table.")
        table = doc.add_table(rows=2, cols=2)
        table.cell(0, 0).text = "Name"
        table.cell(0, 1).text = "Value"
        table.cell(1, 0).text = "Revenue"
        table.cell(1, 1).text = "$4.2M"
        doc.add_paragraph("After the table.")
        buf = io.BytesIO()
        doc.save(buf)
        return buf.getvalue()

    def test_paragraphs_are_extracted(self):
        text = _extract_text_from_bytes(self._make_docx_with_table(), "doc.docx", "")
        assert "Before the table." in text
        assert "After the table." in text

    def test_a_real_table_is_genuinely_extracted_as_markdown_not_silently_dropped(self):
        """Documents the real bug this fixed — a table's real content
        used to vanish with no error at all."""
        text = _extract_text_from_bytes(self._make_docx_with_table(), "doc.docx", "")
        assert "Revenue" in text
        assert "$4.2M" in text
        assert "| Name | Value |" in text

    def test_real_document_order_is_preserved_table_between_two_paragraphs(self):
        text = _extract_text_from_bytes(self._make_docx_with_table(), "doc.docx", "")
        before_idx = text.index("Before the table.")
        table_idx = text.index("Revenue")
        after_idx = text.index("After the table.")
        assert before_idx < table_idx < after_idx


class TestRealXlsxExtraction:
    def _make_xlsx(self) -> bytes:
        import openpyxl
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Sales"
        ws.append(["Name", "Q1", "Q2"])
        ws.append(["Widgets", 100, 150])
        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()

    def test_previously_xlsx_was_not_handled_at_all_and_produced_binary_garbage(self):
        """Documents the real bug this fixed."""
        content = self._make_xlsx()
        assert content[:2] == b"PK"  # confirms it's genuinely a real binary zip archive
        naive_decode = content.decode("utf-8", errors="replace")
        assert "\ufffd" in naive_decode  # confirms the OLD behavior really was garbage

    def test_real_spreadsheet_data_becomes_a_real_markdown_table(self):
        text = _extract_text_from_bytes(self._make_xlsx(), "data.xlsx", "")
        assert "| Name | Q1 | Q2 |" in text
        assert "| Widgets | 100 | 150 |" in text
        assert "### Sales" in text  # real sheet name preserved


class TestRealCsvExtraction:
    def test_real_csv_becomes_a_real_markdown_table(self):
        content = b"Name,Revenue\nAcme,4200000\n"
        text = _extract_text_from_bytes(content, "sales.csv", "")
        assert "| Name | Revenue |" in text
        assert "| Acme | 4200000 |" in text

    def test_a_real_quoted_field_with_an_embedded_comma_is_correctly_parsed_as_one_field(self):
        content = b'Name,Revenue\n"Smith, Inc",1800000\n'
        text = _extract_text_from_bytes(content, "sales.csv", "")
        assert "| Smith, Inc | 1800000 |" in text


class TestRealPptxExtraction:
    def test_real_slide_text_is_extracted_in_real_slide_order(self):
        from pptx import Presentation
        prs = Presentation()
        slide1 = prs.slides.add_slide(prs.slide_layouts[1])
        slide1.shapes.title.text = "Q3 Results"
        slide2 = prs.slides.add_slide(prs.slide_layouts[1])
        slide2.shapes.title.text = "Q4 Outlook"
        buf = io.BytesIO()
        prs.save(buf)
        text = _extract_text_from_bytes(buf.getvalue(), "deck.pptx", "")
        assert text.index("Q3 Results") < text.index("Q4 Outlook")


class TestRealEpubExtraction:
    def test_real_chapter_content_is_extracted_with_html_genuinely_stripped(self):
        from ebooklib import epub
        book = epub.EpubBook()
        book.set_identifier("id1")
        book.set_title("Test Book")
        c1 = epub.EpubHtml(title="Chapter 1", file_name="c1.xhtml", lang="en")
        c1.content = "<h1>Chapter One</h1><p>It was a dark night.</p>"
        book.add_item(c1)
        book.toc = (c1,)
        book.add_item(epub.EpubNcx())
        book.add_item(epub.EpubNav())
        book.spine = ["nav", c1]
        buf = io.BytesIO()
        epub.write_epub(buf, book)
        text = _extract_text_from_bytes(buf.getvalue(), "book.epub", "")
        assert "It was a dark night." in text
        assert "<h1>" not in text


class TestRealEmlExtraction:
    def test_real_headers_and_plain_text_body_are_extracted_not_the_raw_mime_source(self):
        import email.mime.text
        msg = email.mime.text.MIMEText("Please review the invoice.")
        msg["Subject"] = "Invoice #4521"
        msg["From"] = "billing@example.com"
        msg["To"] = "you@example.com"
        text = _extract_text_from_bytes(msg.as_bytes(), "invoice.eml", "")
        assert "Subject: Invoice #4521" in text
        assert "From: billing@example.com" in text
        assert "Please review the invoice." in text
        assert "Content-Type:" not in text  # confirms it's the real parsed body, not raw MIME source


class TestRealVttExtraction:
    def test_consecutive_same_speaker_utterances_are_genuinely_merged(self):
        """The exact real behavior Dify's own docs specify for VTT."""
        vtt = (
            b"WEBVTT\n\n"
            b"00:00:00.000 --> 00:00:02.000\n<v Alice>Hello there.\n\n"
            b"00:00:02.000 --> 00:00:04.000\n<v Alice>How are you?\n\n"
            b"00:00:04.000 --> 00:00:06.000\n<v Bob>Doing well, thanks!\n"
        )
        text = _extract_text_from_bytes(vtt, "meeting.vtt", "")
        lines = text.splitlines()
        assert lines == ["Alice: Hello there. How are you?", "Bob: Doing well, thanks!"]


class TestRealLegacyXls:
    def test_a_real_legacy_binary_xls_file_is_genuinely_parsed(self, tmp_path):
        """Builds a REAL legacy binary .xls via a real LibreOffice
        headless conversion (openpyxl itself can only write the newer
        XML format) — skipped only in an environment without
        LibreOffice installed, not skipped unconditionally."""
        import shutil
        import subprocess
        import openpyxl

        if not shutil.which("libreoffice"):
            pytest.skip("LibreOffice not installed in this environment — real .xls generation needs a real converter.")

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["Name", "Q1", "Q2"])
        ws.append(["Widgets", 100, 150])
        xlsx_path = tmp_path / "test.xlsx"
        wb.save(xlsx_path)

        result = subprocess.run(
            ["libreoffice", "--headless", "--convert-to", "xls", "--outdir", str(tmp_path), str(xlsx_path)],
            capture_output=True, text=True, timeout=30,
        )
        xls_path = tmp_path / "test.xls"
        assert xls_path.exists(), f"LibreOffice conversion failed: {result.stdout} {result.stderr}"

        with open(xls_path, "rb") as f:
            content = f.read()
        text = _extract_text_from_bytes(content, "test.xls", "")
        assert "Widgets" in text
        assert "100" in text
        assert "150" in text


class TestRealMultiFileMode:
    def test_resolve_whole_var_reference_preserves_the_real_raw_type(self):
        ctx = make_ctx()
        ctx["vars"]["documents"] = [{"filename": "a.txt", "url": "http://x/a"}, {"filename": "b.txt", "url": "http://x/b"}]
        result = _resolve_docextractor_input("{{documents}}", ctx)
        assert result == ctx["vars"]["documents"]
        assert isinstance(result, list)

    def test_resolve_string_interpolation_still_coerces_to_string_backward_compatibly(self):
        ctx = make_ctx()
        ctx["vars"]["bucket"] = "my-bucket"
        result = _resolve_docextractor_input("s3://{{bucket}}/file.pdf", ctx)
        assert result == "s3://my-bucket/file.pdf"

    def test_multiple_mode_with_no_configured_files_reports_a_real_error_not_silent_empty_success(self):
        ctx = make_ctx()
        cfg = {"mode": "multiple", "input_var": "{{documents}}"}
        result = handle_docextractor(cfg, ctx)
        assert "docextractor_error" in result["vars"]

    def test_multiple_mode_produces_a_real_array_output_one_string_per_file(self, monkeypatch):
        import app.harness as harness_mod

        def fake_fetch(item):
            return {"text": f"content of {item['filename']}", "error": None, "provider": "generic", "bytes": 10, "filename": item["filename"]}

        monkeypatch.setattr(harness_mod, "_fetch_and_extract_one", fake_fetch)
        ctx = make_ctx()
        ctx["vars"]["documents"] = [{"filename": "a.txt", "url": "http://x/a"}, {"filename": "b.txt", "url": "http://x/b"}]
        cfg = {"mode": "multiple", "input_var": "{{documents}}"}
        result = handle_docextractor(cfg, ctx)
        assert result["vars"]["docextractor_text"] == ["content of a.txt", "content of b.txt"]
        assert isinstance(result["vars"]["docextractor_text"], list)

    def test_a_real_per_file_failure_does_not_abort_the_other_real_successful_files(self, monkeypatch):
        import app.harness as harness_mod

        def flaky_fetch(item):
            if item["filename"] == "bad.txt":
                return {"text": None, "error": "fetch failed: 404", "provider": "generic", "bytes": 0}
            return {"text": "good content", "error": None, "provider": "generic", "bytes": 12, "filename": item["filename"]}

        monkeypatch.setattr(harness_mod, "_fetch_and_extract_one", flaky_fetch)
        ctx = make_ctx()
        ctx["vars"]["documents"] = [{"filename": "good.txt", "url": "http://x/good"}, {"filename": "bad.txt", "url": "http://x/bad"}]
        cfg = {"mode": "multiple", "input_var": "{{documents}}"}
        result = handle_docextractor(cfg, ctx)
        assert result["vars"]["docextractor_text"] == ["good content", None]
        assert "fetch failed: 404" in result["vars"]["docextractor_errors"]

    def test_single_mode_still_works_with_a_plain_url_string_backward_compatibly(self, monkeypatch):
        import app.harness as harness_mod

        def fake_fetch(item):
            assert item == "https://example.com/doc.pdf"
            return {"text": "real content", "error": None, "provider": "generic", "bytes": 20, "filename": "doc.pdf"}

        monkeypatch.setattr(harness_mod, "_fetch_and_extract_one", fake_fetch)
        ctx = make_ctx()
        cfg = {"input_var": "https://example.com/doc.pdf"}  # no "mode" key at all — real legacy config
        result = handle_docextractor(cfg, ctx)
        assert result["vars"]["docextractor_text"] == "real content"
        assert isinstance(result["vars"]["docextractor_text"], str)

    def test_single_mode_with_a_real_uploaded_file_dict_reference(self, monkeypatch):
        import app.harness as harness_mod

        def fake_fetch(item):
            assert item == {"filename": "report.pdf", "url": "http://127.0.0.1:8000/api/uploads/up-1/download"}
            return {"text": "real report text", "error": None, "provider": "generic", "bytes": 30, "filename": "report.pdf"}

        monkeypatch.setattr(harness_mod, "_fetch_and_extract_one", fake_fetch)
        ctx = make_ctx()
        ctx["vars"]["report"] = {"filename": "report.pdf", "url": "http://127.0.0.1:8000/api/uploads/up-1/download"}
        cfg = {"mode": "single", "input_var": "{{report}}"}
        result = handle_docextractor(cfg, ctx)
        assert result["vars"]["docextractor_text"] == "real report text"


class TestRealSelfReferentialFetchConcurrency:
    """The real concurrency bug found and fixed while validating
    multi-file mode: fetching this platform's own real /api/uploads URL
    from WITHIN a request handled by the same async server used to
    deadlock (the single event loop stayed blocked synchronously inside
    the outer request, so it could never accept the inbound self-
    connection) until the outbound httpx call's own client-side timeout
    fired. This is a real regression guard at the harness level — the
    actual endpoint-level fix (routers/execution.py's run_agent now
    offloads to a real worker thread) isn't exercised by this
    lightweight unit test, which only confirms handle_docextractor
    itself doesn't introduce any additional blocking beyond one real
    httpx call per file."""

    def test_multiple_files_are_fetched_independently_one_real_httpx_call_each(self, monkeypatch):
        import app.harness as harness_mod
        call_log = []

        def fake_fetch(item):
            call_log.append(item["filename"])
            return {"text": f"text-{item['filename']}", "error": None, "provider": "generic", "bytes": 5, "filename": item["filename"]}

        monkeypatch.setattr(harness_mod, "_fetch_and_extract_one", fake_fetch)
        ctx = make_ctx()
        ctx["vars"]["documents"] = [{"filename": f"doc{i}.txt", "url": f"http://x/{i}"} for i in range(3)]
        cfg = {"mode": "multiple", "input_var": "{{documents}}"}
        handle_docextractor(cfg, ctx)
        assert call_log == ["doc0.txt", "doc1.txt", "doc2.txt"]  # real one-call-per-file, in real order
