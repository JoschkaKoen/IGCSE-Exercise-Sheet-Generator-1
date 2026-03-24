# IGCSE Exercise Sheet Generator

Extract chosen questions from Cambridge-style IGCSE question papers (PDF) and lay them out into a single printable PDF. Optionally pull matching answers from a mark scheme PDF. Natural-language mode uses an LLM to pick papers and question numbers from your exam folders; legacy mode takes explicit paths.

## Requirements

- **Python 3.10+** (3.12+ recommended)
- **Exam PDFs** for natural-language mode: bundled under `exams/physics/` and `exams/computer_science/` (see `exams/README.md`). Override paths in `extract_exercises/config.py` if you keep papers elsewhere.
- **xAI API key** for natural-language mode only (legacy CLI does not need it).

## Setup

```bash
cd "/path/to/Exercise Sheet Generator"
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Configuration

Create a `.env` file next to the project (same folder as `requirements.txt`):

| Variable | Required | Description |
|----------|----------|-------------|
| `XAI_API_KEY` | Yes for NL mode | API key for the OpenAI-compatible xAI endpoint |
| `XAI_MODEL` | No | Defaults to a Grok fast model; override if needed |

Natural-language mode also loads `.env` from the current working directory if present.

## Usage

**Natural language** (one quoted sentence):

```bash
python extract_exercises.py "Winter 2024 Physics paper 21, questions 12–14, include mark scheme"
```

**Legacy** (explicit PDFs and question numbers):

```bash
python extract_exercises.py /path/to/qp.pdf output.pdf 12 13 14
python extract_exercises.py /path/to/qp.pdf output.pdf 12-14 --ms /path/to/ms.pdf
```

**Module invocation**:

```bash
python -m extract_exercises --help
```

## Web UI

The site is **not** started automatically—you must keep a terminal open with Uvicorn running while you use the browser.

Run a local browser UI (same natural-language flow as the one-argument CLI: prompt → generated PDFs, plus an exam library page for bundled PDFs):

```bash
cd "/path/to/Exercise Sheet Generator"
source .venv/bin/activate
pip install -r requirements.txt
uvicorn web.app:app --reload --host 127.0.0.1 --port 8001
```

Open [http://127.0.0.1:8001](http://127.0.0.1:8001) (use the same port as in the command). Set `XAI_API_KEY` in `.env` as for CLI natural-language mode. Jobs run in the background; the page polls until your sheet (and optional `*_answers.pdf`) is ready.

**If the page does not load:** (1) Confirm the terminal shows `Uvicorn running on http://127.0.0.1:…`—if you see `Address already in use`, pick another port, e.g. `--port 8002`. (2) On many Macs, **port 8000 is already taken** (often by Docker), so use `8001` or higher instead of `8000`. (3) Use the exact URL printed by Uvicorn, including the port.

**Programmatic**:

```python
from extract_exercises import run_extraction_jobs

run_extraction_jobs(
    [{"input_pdf": "...", "questions": [1, 2], "mark_scheme_pdf": "..."}],
    "sheet.pdf",
    exam_key="physics",  # or None for legacy-style labelling
)
```

## Output

- Bare filenames (e.g. `sheet.pdf`) are written under `output/run_YYYYMMDD_HHMMSS/`.
- A mark scheme run also produces `sheet_answers.pdf` beside the main output when applicable.

## Project layout

| Path | Role |
|------|------|
| `extract_exercises.py` | Thin CLI entry point |
| `extract_exercises/` | Package: config, question detection, raster layout, mark schemes, NL resolver, pipeline |
| `web/` | FastAPI app, templates, and static assets for the local web UI |
| `exams/physics/`, `exams/computer_science/` | Bundled question paper & mark scheme PDFs for NL mode |
| `fonts/lmroman10-*.otf` | Latin Modern Roman (LaTeX `lmodern` text) for raster labels; see `fonts/README.md` |

## License

Add a `LICENSE` file if you want to specify terms; the repository currently has no default license.
