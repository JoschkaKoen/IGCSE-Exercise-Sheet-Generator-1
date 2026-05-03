"""Compare vision models on the step-14 handwriting check.

Targets the false-positive on page 36 of run
``output/xscore/s23_12/2026-05-03_01-50-02`` (Lucas's back blank page) plus
known true negatives (8, 12, 24) and true positives (2, 14, 26) as a regression
guard. Calls the production ``_has_handwriting`` for each (model, page) so the
prompt and parser exactly match step 14.

Run:
    python scripts/diagnose_handwriting_models.py

Optional env:
    HANDWRITING_DIAG_MODELS  comma-separated; default
        "qwen3-vl-flash,qwen3.6-flash,qwen3-vl-plus,gemini-3-flash-preview"
    HANDWRITING_DIAG_PAGES   comma-separated 1-based scan pages; default
        "2,8,12,14,24,26,36"
    HANDWRITING_DIAG_PDF     scan PDF; default merged_scan.pdf from the run
        listed in the module docstring
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eXercise.env_load import load_project_env

load_project_env()

from xscore.marking.blank_page_detection import (
    _build_client_state,
    _has_handwriting,
    _render_page_jpeg,
)


# Ground truth from the original run's handwriting.json + user adjudication
EXPECTED: dict[int, bool] = {
    2: True,    # Cici p2 — clear answers
    8: False,   # Cici p7 — printed only
    12: False,  # Cici back blank
    14: True,   # Kim p2 — clear answers
    24: False,  # Kim back blank
    26: True,   # Lucas p2 — clear answers
    36: False,  # Lucas back blank — current model false-positives here
}

DEFAULT_PDF = Path(
    "output/xscore/s23_12/2026-05-03_01-50-02/04_merge_duplex_scans/merged_scan.pdf"
)
DEFAULT_MODELS = "qwen3-vl-flash,qwen3.6-flash,qwen3-vl-plus,gemini-3-flash-preview"
DEFAULT_PAGES = "2,8,12,14,24,26,36"


def main() -> int:
    pdf_path = Path(os.environ.get("HANDWRITING_DIAG_PDF", str(DEFAULT_PDF))).resolve()
    if not pdf_path.is_file():
        print(f"FAIL: scan PDF not found: {pdf_path}", file=sys.stderr)
        return 1

    models = [m.strip() for m in os.environ.get(
        "HANDWRITING_DIAG_MODELS", DEFAULT_MODELS).split(",") if m.strip()]
    pages = [int(p) for p in os.environ.get(
        "HANDWRITING_DIAG_PAGES", DEFAULT_PAGES).split(",") if p.strip()]

    print(f"PDF: {pdf_path}")
    print(f"Models: {models}")
    print(f"Pages: {pages}")
    print()

    # Pre-render JPEGs once (same DPI / colorspace as production: 150, gray)
    render_t0 = time.perf_counter()
    page_jpegs: dict[int, bytes] = {p: _render_page_jpeg(pdf_path, p) for p in pages}
    print(f"Rendered {len(pages)} pages in {time.perf_counter() - render_t0:.2f}s")
    print()

    # results[model][page] = (hw, conf, reason, dur_s, error)
    results: dict[str, dict[int, tuple[bool | None, int | None, str, float, str | None]]] = {}

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        for model in models:
            print(f"── {model} " + "─" * (60 - len(model)))
            client_or_err = _build_client_state(model)
            if isinstance(client_or_err, str):
                print(f"  SKIP: {client_or_err}")
                results[model] = {p: (None, None, "", 0.0, client_or_err) for p in pages}
                continue
            state = client_or_err
            results[model] = {}
            for page in pages:
                save_path = tmp_dir / f"{model}_{page}_prompt.txt"
                t0 = time.perf_counter()
                try:
                    hw, _pn, _ic, conf, reason = _has_handwriting(
                        state, model, page_jpegs[page], save_path,
                    )
                    err = None
                except Exception as exc:  # noqa: BLE001
                    hw, conf, reason = None, None, ""
                    err = repr(exc)
                dur = time.perf_counter() - t0
                results[model][page] = (hw, conf, reason, dur, err)
                expected = EXPECTED[page]
                got_str = "?" if hw is None else str(hw)
                ok = (hw == expected)
                marker = "✓" if ok else ("·" if hw is None else "✗")
                conf_str = f"c{conf}" if conf is not None else "c?"
                print(
                    f"  {marker} p{page:>2}  exp={expected!s:<5}  got={got_str:<5}"
                    f"  {conf_str:<3}  {dur:>5.1f}s  {(reason or err or '')[:80]}"
                )
            print()

    # Summary table
    print()
    print("Summary (✓ correct, ✗ wrong, · inconclusive):")
    header = "page  expected     " + "  ".join(f"{m:<22}" for m in models)
    print(header)
    print("-" * len(header))
    correct_per_model = {m: 0 for m in models}
    for page in pages:
        cells = []
        for m in models:
            hw, conf, _r, _d, err = results[m][page]
            if hw is None:
                cells.append(f"  ·  ({err or 'inconcl'})"[:22].ljust(22))
            else:
                ok = (hw == EXPECTED[page])
                if ok:
                    correct_per_model[m] += 1
                marker = "✓" if ok else "✗"
                cells.append(f"{marker} {hw!s:<5} c{conf}".ljust(22))
        print(f"  p{page:>2}  {EXPECTED[page]!s:<11}  " + "  ".join(cells))
    print("-" * len(header))
    print(
        "TOTAL CORRECT".ljust(20)
        + "  ".join(f"{correct_per_model[m]}/{len(pages)}".ljust(22) for m in models)
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
