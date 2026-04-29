"""Generic single-page PDF splitter, shared by mark-scheme and exam-fill paths."""

from __future__ import annotations

from pathlib import Path


def split_pdf_into_pages(
    pdf_path: Path, output_dir: "Path | None",
) -> tuple[int, list[Path], "Path | None"]:
    """Split *pdf_path* into single-page PDFs under *output_dir*.

    Returns ``(n_pages, page_paths, tmp_dir)``. ``tmp_dir`` is non-None only when
    ``output_dir`` is None (caller is responsible for cleanup). Page files are
    named ``page_1.pdf``, ``page_2.pdf``, … in the target directory.
    """
    import fitz

    _tmp_dir: Path | None = None
    if output_dir is not None:
        pages_dir = output_dir
    else:
        import tempfile
        _tmp_dir = Path(tempfile.mkdtemp())
        pages_dir = _tmp_dir
    pages_dir.mkdir(parents=True, exist_ok=True)

    page_paths: list[Path] = []
    with fitz.open(str(pdf_path)) as _doc:
        n_pages = _doc.page_count
        for _i in range(n_pages):
            _out_path = pages_dir / f"page_{_i + 1}.pdf"
            _out = fitz.open()
            try:
                _out.insert_pdf(_doc, from_page=_i, to_page=_i)
                _out.save(str(_out_path))
            finally:
                _out.close()
            page_paths.append(_out_path)
    return n_pages, page_paths, _tmp_dir
