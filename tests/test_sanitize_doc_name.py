"""Tests for _sanitize_doc_name in converter module."""
from __future__ import annotations

from openkb.converter import _sanitize_doc_name


class TestSanitizeDocName:
    def test_strips_parentheses(self):
        assert _sanitize_doc_name("Book (Author)") == "Book Author"

    def test_strips_nested_parentheses(self):
        assert _sanitize_doc_name("Running Lean (3rd Ed) (site.com)") == "Running Lean 3rd Ed site.com"

    def test_strips_quotes(self):
        assert _sanitize_doc_name('He said "hello"') == "He said hello"

    def test_strips_single_quotes(self):
        assert _sanitize_doc_name("it's a test") == "its a test"

    def test_strips_mixed(self):
        assert _sanitize_doc_name("Book (Author) \"Chapter\" 'Part'") == "Book Author Chapter Part"

    def test_clean_name_unchanged(self):
        assert _sanitize_doc_name("clean-book-name") == "clean-book-name"

    def test_strips_whitespace(self):
        assert _sanitize_doc_name("  padded  ") == "padded"

    def test_complex_real_filename(self):
        name = "Running Lean, 3rd Edition  Iterate from Plan A to a Plan That Works (Ash Maurya) (z-library.sk, 1lib.sk, z-lib.sk)"
        result = _sanitize_doc_name(name)
        assert "(" not in result
        assert ")" not in result
        assert "Running Lean" in result
