"""Mark-scheme graphic index: per page, detect whether graphics exist and return exercise numbers."""
from __future__ import annotations
import os, sys, time
from pathlib import Path
from pydantic import BaseModel

from dotenv import load_dotenv
load_dotenv("/Users/joschka/Desktop/Programming/eXercise/default.env")
load_dotenv("/Users/joschka/Desktop/Programming/eXercise/api-keys.env")
load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
RASTER_DPI = 300
OUT_BASE   = Path("/Users/joschka/Desktop/Programming/eXercise/scheme_graphics_test")

MODELS = [
    "gemini-2.5-flash-lite",
]

PDFS = [
    Path("/Users/joschka/Desktop/IGCSE Computer Science 25/Scanned Exams/s23 12/CS s23 12 Ex. all_answers.pdf"),
    Path("/Users/joschka/Desktop/IGCSE Computer Science 25/Scanned Exams/s23 22/CS s23 22 Ex. all_answers.pdf"),
    Path("/Users/joschka/Desktop/IGCSE Computer Science 25/Scanned Exams/w23 13/CS w23 13 Ex. all_answers.pdf"),
    Path("/Users/joschka/Desktop/IGCSE Computer Science 25/Scanned Exams/w23 23/CS w23 23 Ex. all_answers.pdf"),
]

# ---------------------------------------------------------------------------
# Output schema
# ---------------------------------------------------------------------------
class Graphic(BaseModel):
    exercise: str  # e.g. "1(a)", "3(b)(ii)"

class PageGraphics(BaseModel):
    graphics: list[Graphic]

SCHEMA = PageGraphics.model_json_schema()

# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------
PROMPT = (
    "Look at this exam mark scheme page. Does it contain any diagrams, figures, or "
    "illustrations — things a human would describe as 'a drawing' or 'a figure'? "
    "This includes circuit diagrams, logic gate diagrams, network diagrams, ray diagrams, "
    "graphs with plotted data or axes, labeled physical setups, geometric figures, "
    "flowcharts, and maps.\n\n"
    "This does NOT include: tables (even tables with borders), truth tables, mathematical "
    "equations or expressions, pseudocode, program code, text with unusual formatting, "
    "page decorations, logos, or page numbers.\n\n"
    "For each graphic found, return the exercise or question number it belongs to "
    "(e.g. '1(a)', '2', '3(b)(ii)'). "
    "Return an empty list if the page has no graphics."
)


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
def run(pdf_path: Path, client, model: str) -> None:
    import fitz, json
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from google.genai import types as T

    print(f"\n  {pdf_path.name}")
    doc = fitz.open(str(pdf_path))
    n   = doc.page_count

    page_pngs: dict[int, bytes] = {}
    for i in range(n):
        pix = doc[i].get_pixmap(dpi=RASTER_DPI)
        page_pngs[i + 1] = pix.tobytes("png")

    def query_page(page_num: int) -> tuple[int, list[Graphic], float]:
        t0 = time.perf_counter()
        resp = client.models.generate_content(
            model=model,
            contents=[
                T.Part.from_bytes(data=page_pngs[page_num], mime_type="image/png"),
                T.Part.from_text(text=PROMPT),
            ],
            config=T.GenerateContentConfig(
                response_mime_type="application/json",
                response_json_schema=SCHEMA,
                max_output_tokens=1024,
                thinking_config=T.ThinkingConfig(thinking_budget=8192, include_thoughts=False),
            ),
        )
        data = json.loads(resp.text or '{"graphics":[]}')
        graphics = [Graphic(**g) for g in data.get("graphics", [])]
        return page_num, graphics, round(time.perf_counter() - t0, 1)

    results: dict[int, list[Graphic]] = {}
    with ThreadPoolExecutor(max_workers=n) as pool:
        futures = {pool.submit(query_page, p): p for p in range(1, n + 1)}
        for fut in as_completed(futures):
            page_num, graphics, elapsed = fut.result()
            results[page_num] = graphics
            print(f"    p{page_num}: {len(graphics)} graphic(s)  ({elapsed}s)")

    for page_num in range(1, n + 1):
        for g in results[page_num]:
            print(f"    page {page_num}  exercise {g.exercise}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
import fitz

api_key = (os.environ.get("GEMINI_API_KEY", "") or os.environ.get("GOOGLE_API_KEY", "")).strip()
if not api_key:
    sys.exit("GEMINI_API_KEY not set")

from google import genai as gai
client = gai.Client(api_key=api_key)

for model in MODELS:
    print(f"\n{'='*55}\n{model}\n{'='*55}")
    for pdf in PDFS:
        run(pdf, client, model)
