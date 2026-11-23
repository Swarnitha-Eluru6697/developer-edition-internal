"""Document parsing must not be able to consume the API process.

The archive controls (entry count, expansion, compression ratio) were
already in place, and the four ZIP-based formats already ran in a worker
pool. Everything else did not: PDF was parsed with `PdfReader` inline on
the request thread, and csv/eml/msg/vtt/html/doc/xls/ppt went straight
through `_EXTRACTORS` the same way — no timeout, no CPU ceiling, no
memory ceiling.

The HTML parser is quadratic in nesting depth, which is how a document
that violates no size limit at all becomes an unbounded CPU burn inside
the process that also has to answer health checks. Measured on this
build before the fix:

      2,000 nested <div>s (  21 KB)  ->   0.09s
     20,000 nested <div>s ( 214 KB)  ->   5.03s
    200,000 nested <div>s (2.1 MB)   ->   still running after 10 minutes

After: the 2.1 MB document stops at its 15s budget with an honest
error, and 4 concurrent copies of it left /readyz answering 95 times in
25 seconds with a worst case of 40 ms and no failures.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from app.harness import (_DEFAULT_PARSE_TIMEOUT_SECONDS, _PARSE_TIMEOUT_SECONDS,
                         _PARSE_WORKER_CPU_SECONDS, _PARSE_WORKER_MEMORY_MB,
                         _extract_text_from_bytes, _limit_parse_worker)


def nested_html(levels: int) -> bytes:
    return b"<div>" * levels + b"hi" + b"</div>" * levels


class TestPathologicalDocumentsAreBounded:
    def test_the_quadratic_html_case_stops_at_its_budget(self):
        """The document from the report's own concern: no size limit is
        violated, and before the fix this ran for over ten minutes."""
        began = time.monotonic()
        out = _extract_text_from_bytes(nested_html(200_000), "bomb.html", "")
        took = time.monotonic() - began

        assert "extraction failed" in out
        assert "wall-clock limit" in out
        # Generous upper bound: the point is that it STOPS, near its
        # budget, rather than running for minutes.
        assert took < _DEFAULT_PARSE_TIMEOUT_SECONDS + 15, f"took {took:.1f}s"

    def test_an_ordinary_document_of_the_same_format_still_parses(self):
        """The guard on the guard: a timeout that refused real documents
        would just be a different outage."""
        out = _extract_text_from_bytes(b"<html><body><p>hello world</p></body></html>",
                                       "fine.html", "")
        assert "hello world" in out
        assert "extraction failed" not in out

    @pytest.mark.parametrize("name,content", [
        ("x.csv", b"a,b\n1,2\n"),
        ("x.txt", b"plain text"),
        ("x.vtt", b"WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nhello\n"),
    ])
    def test_ordinary_documents_of_every_routed_format_parse(self, name, content):
        out = _extract_text_from_bytes(content, name, "")
        assert "extraction failed" not in out, out


class TestEveryParserRunsInTheConstrainedWorker:
    def test_pdf_no_longer_parses_inline(self):
        """PDF was the clearest gap: PdfReader on the request thread,
        with no timeout and no memory ceiling."""
        import inspect

        from app import harness

        src = inspect.getsource(harness._extract_text_from_bytes)
        assert "_run_extractor_with_limits" in src
        # The inline PdfReader call is gone from the dispatch.
        assert "PdfReader(io.BytesIO(content))" not in src

    def test_the_worker_entry_point_handles_pdf_zip_and_the_rest(self):
        import inspect

        from app import harness

        src = inspect.getsource(harness._parse_document_in_worker)
        assert "PdfReader" in src
        assert "_ZIP_BASED_EXTRACTORS" in src
        assert "_EXTRACTORS" in src

    def test_a_real_pdf_still_extracts_through_the_worker(self):
        import io

        import pypdf

        writer = pypdf.PdfWriter()
        writer.add_blank_page(width=200, height=200)
        buf = io.BytesIO()
        writer.write(buf)
        out = _extract_text_from_bytes(buf.getvalue(), "one.pdf", "application/pdf")
        assert "extraction failed" not in out, out


class TestWorkerResourceLimits:
    def test_cpu_and_memory_ceilings_are_configured(self):
        assert _PARSE_WORKER_CPU_SECONDS > 0
        assert _PARSE_WORKER_MEMORY_MB > 0

    def test_the_limiter_uses_data_not_address_space(self):
        """RLIMIT_DATA bounds the data segment, which is what a runaway
        allocation grows. RLIMIT_AS bounds virtual address space, which
        a Python process maps far more of than it commits — both work,
        but only one of them means what the limit is for."""
        import inspect

        src = inspect.getsource(_limit_parse_worker)
        assert "RLIMIT_DATA" in src
        assert "RLIMIT_CPU" in src

    def test_the_limiter_does_not_set_a_per_uid_process_limit(self):
        """RLIMIT_NPROC is per real UID, not per process, so setting it
        in a worker also constrains the parent and anything else running
        as the same user."""
        import inspect

        src = inspect.getsource(_limit_parse_worker)
        assert "resource.RLIMIT_NPROC" not in src.replace("RLIMIT_NPROC is", "")

    def test_the_limiter_survives_a_platform_that_refuses_a_limit(self, monkeypatch):
        """It must apply the limits it can rather than none: a platform
        that declines one should still get the others, and the
        wall-clock timeout does not depend on any of them."""
        import resource

        def refuse(limit, values):
            if limit == resource.RLIMIT_DATA:
                raise ValueError("not supported here")

        monkeypatch.setattr(resource, "setrlimit", refuse)
        _limit_parse_worker()  # must not raise


class TestTimeoutsArePerFormat:
    def test_pdf_has_its_own_budget(self):
        """Formats are not comparable: a large PDF legitimately takes
        longer than a .vtt, and one budget for both is either too tight
        or too loose."""
        assert _PARSE_TIMEOUT_SECONDS["pdf"] >= _DEFAULT_PARSE_TIMEOUT_SECONDS

    def test_every_zip_format_has_a_budget(self):
        for ext in ("docx", "xlsx", "pptx", "epub"):
            assert _PARSE_TIMEOUT_SECONDS[ext] > 0

    def test_an_unlisted_format_still_gets_one(self):
        assert _DEFAULT_PARSE_TIMEOUT_SECONDS > 0


class TestArchiveLimitsAreStillEnforcedFirst:
    def test_container_safety_runs_before_any_parse(self):
        """The cheap metadata-only check must stay in front of the
        expensive worker call — it exists to avoid the decompression
        that a parser would otherwise perform."""
        import inspect

        from app import harness

        src = inspect.getsource(harness._run_zip_extractor_with_limits)
        assert src.index("validate_zip_container_safety(content)") < \
               src.index("_run_extractor_with_limits(")

    def test_a_zip_bomb_is_still_refused(self):
        import io
        import zipfile

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            # Highly compressible: a small archive claiming a huge member.
            zf.writestr("word/document.xml", b"0" * 200_000_000)
        out = _extract_text_from_bytes(buf.getvalue(), "bomb.docx", "")
        assert "extraction failed" in out, out
