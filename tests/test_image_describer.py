"""Tests for openkb.image_describer module."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch, MagicMock

from openkb.image_describer import describe_images, _encode_image


def _make_image(path: Path, size_bytes: int = 100) -> Path:
    """Create a dummy PNG image file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * (size_bytes - 8))
    return path


def _mock_response(text: str) -> MagicMock:
    """Create a mock litellm response."""
    msg = MagicMock()
    msg.content = text
    choice = MagicMock()
    choice.message = msg
    resp = MagicMock()
    resp.choices = [choice]
    return resp


class TestDescribeImages:
    def test_no_images_returns_unchanged(self):
        result = describe_images("Just text, no images.", Path("/tmp/kb"), "gpt-4o")
        assert result == "Just text, no images."

    def test_describe_single_image(self, tmp_path: Path):
        kb_dir = tmp_path
        img_path = kb_dir / "wiki" / "sources" / "images" / "doc" / "fig1.png"
        _make_image(img_path)

        md = "See diagram:\n\n![image](sources/images/doc/fig1.png)\n\nNext paragraph."
        mock_resp = _mock_response("A bar chart showing revenue growth.")

        with patch("openkb.image_describer.completion", return_value=mock_resp) as mock_call:
            result = describe_images(md, kb_dir, "gpt-4o")

        assert "![image](sources/images/doc/fig1.png)" in result
        assert "*[Figure: A bar chart showing revenue growth.]*" in result
        assert "Next paragraph." in result
        mock_call.assert_called_once()

    def test_describe_multiple_images(self, tmp_path: Path):
        kb_dir = tmp_path
        img1 = kb_dir / "wiki" / "sources" / "images" / "doc" / "a.png"
        img2 = kb_dir / "wiki" / "sources" / "images" / "doc" / "b.png"
        _make_image(img1)
        _make_image(img2)

        md = "![img](sources/images/doc/a.png)\n\nText\n\n![img](sources/images/doc/b.png)"
        responses = [_mock_response("Diagram A"), _mock_response("Diagram B")]

        with patch("openkb.image_describer.completion", side_effect=responses):
            result = describe_images(md, kb_dir, "gpt-4o")

        assert "*[Figure: Diagram A]*" in result
        assert "*[Figure: Diagram B]*" in result

    def test_missing_image_file_skipped(self, tmp_path: Path):
        kb_dir = tmp_path
        md = "![image](sources/images/doc/missing.png)\n\nText after."

        result = describe_images(md, kb_dir, "gpt-4o")
        assert result == md  # Unchanged

    def test_vlm_failure_graceful(self, tmp_path: Path):
        kb_dir = tmp_path
        img_path = kb_dir / "wiki" / "sources" / "images" / "doc" / "fail.png"
        _make_image(img_path)

        md = "![image](sources/images/doc/fail.png)\n\nAfter."

        with patch("openkb.image_describer.completion", side_effect=RuntimeError("API error")):
            result = describe_images(md, kb_dir, "gpt-4o")

        assert result == md  # Unchanged, no crash

    def test_image_retained_with_description(self, tmp_path: Path):
        kb_dir = tmp_path
        img_path = kb_dir / "wiki" / "sources" / "images" / "doc" / "x.png"
        _make_image(img_path)

        md = "Before.\n\n![diagram](sources/images/doc/x.png)\n\nAfter."
        with patch("openkb.image_describer.completion", return_value=_mock_response("Neural network")):
            result = describe_images(md, kb_dir, "gpt-4o")

        assert "![diagram](sources/images/doc/x.png)" in result
        assert "*[Figure: Neural network]*" in result

    def test_litellm_prefix_stripped(self, tmp_path: Path):
        kb_dir = tmp_path
        img_path = kb_dir / "wiki" / "sources" / "images" / "doc" / "t.png"
        _make_image(img_path)

        md = "![x](sources/images/doc/t.png)"

        with patch("openkb.image_describer.completion", return_value=_mock_response("Desc")) as mock_call:
            describe_images(md, kb_dir, "litellm/gpt-4o")

        assert mock_call.call_args[1]["model"] == "gpt-4o"

    def test_path_traversal_blocked(self, tmp_path: Path):
        """Images with ../ in path are rejected."""
        kb_dir = tmp_path
        # Create a real file outside wiki
        secret = tmp_path / "secret.txt"
        secret.write_text("passwords")

        md = "![img](../../secret.txt)"
        result = describe_images(md, kb_dir, "gpt-4o")
        assert result == md  # Unchanged, no VLM call

    def test_max_images_limit(self, tmp_path: Path):
        """VLM calls capped at max_images."""
        kb_dir = tmp_path
        for i in range(5):
            _make_image(kb_dir / "wiki" / f"img{i}.png")

        md = "\n".join(f"![img](img{i}.png)" for i in range(5))

        with patch("openkb.image_describer.completion", return_value=_mock_response("Desc")) as mock_call:
            result = describe_images(md, kb_dir, "gpt-4o", max_images=2)

        assert mock_call.call_count == 2  # Only 2 VLM calls, not 5

    def test_multiline_vlm_response_sanitized(self, tmp_path: Path):
        """VLM responses with newlines/markdown are cleaned."""
        kb_dir = tmp_path
        _make_image(kb_dir / "wiki" / "img.png")

        md = "![x](img.png)"

        with patch(
            "openkb.image_describer.completion",
            return_value=_mock_response("Line 1\nLine 2\n![link](http://x)"),
        ):
            result = describe_images(md, kb_dir, "gpt-4o")

        assert "\n" not in result.split("*[Figure:")[1].split("]*")[0]
        assert "![" not in result.split("*[Figure:")[1].split("]*")[0]

    def test_empty_vlm_response_skipped(self, tmp_path: Path):
        """Empty VLM response doesn't inject empty figure."""
        kb_dir = tmp_path
        _make_image(kb_dir / "wiki" / "img.png")

        md = "![x](img.png)"

        with patch("openkb.image_describer.completion", return_value=_mock_response("")):
            result = describe_images(md, kb_dir, "gpt-4o")

        assert "*[Figure:" not in result

    def test_large_image_skipped(self, tmp_path: Path):
        """Images exceeding size limit are skipped."""
        kb_dir = tmp_path
        # Create a large fake image (21MB)
        large_img = kb_dir / "wiki" / "big.png"
        large_img.parent.mkdir(parents=True, exist_ok=True)
        large_img.write_bytes(b"\x89PNG" + b"\x00" * (21 * 1024 * 1024))

        md = "![big](big.png)"

        with patch("openkb.image_describer.completion") as mock_call:
            result = describe_images(md, kb_dir, "gpt-4o")

        mock_call.assert_not_called()
        assert result == md


class TestEncodeImage:
    def test_encodes_to_data_uri(self, tmp_path: Path):
        img = tmp_path / "test.png"
        img.write_bytes(b"\x89PNG\r\n\x1a\n")
        result = _encode_image(img)
        assert result.startswith("data:image/png;base64,")


class TestImagePathsSpecialCharacters:
    def test_path_with_parentheses_in_filename(self, tmp_path: Path):
        """Image paths containing parentheses (common in book names) must match."""
        kb_dir = tmp_path
        # Create directory and image with parens in name
        img_dir = kb_dir / "wiki" / "sources" / "images" / "Book (Author) (site.com)"
        img_path = img_dir / "p1_img1.png"
        _make_image(img_path)

        md = "![image](sources/images/Book (Author) (site.com)/p1_img1.png)"

        with patch("openkb.image_describer.completion", return_value=_mock_response("Chart")):
            result = describe_images(md, kb_dir, "gpt-4o")

        assert "*[Figure: Chart]*" in result

    def test_multiple_images_with_parens(self, tmp_path: Path):
        """Multiple images with parenthesized paths all get described."""
        kb_dir = tmp_path
        base = kb_dir / "wiki" / "sources" / "images" / "My Book (2nd Ed)"
        _make_image(base / "a.png")
        _make_image(base / "b.png")

        md = (
            "![x](sources/images/My Book (2nd Ed)/a.png)\n\n"
            "Text\n\n"
            "![y](sources/images/My Book (2nd Ed)/b.png)"
        )
        with patch(
            "openkb.image_describer.completion",
            side_effect=[_mock_response("Img A"), _mock_response("Img B")],
        ):
            result = describe_images(md, kb_dir, "gpt-4o")

        assert "*[Figure: Img A]*" in result
        assert "*[Figure: Img B]*" in result
