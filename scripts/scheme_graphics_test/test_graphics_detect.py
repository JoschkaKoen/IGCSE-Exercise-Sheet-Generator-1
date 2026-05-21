"""Mark-scheme graphic detection: rasterize page → send PNG → get bboxes → crop from vector PDF."""
from __future__ import annotations
import os, sys, time, re, tempfile
from pathlib import Path
from pydantic import BaseModel

from dotenv import load_dotenv
load_dotenv("/Users/joschka/Desktop/Programming/eXercise/default.env")
load_dotenv("/Users/joschka/Desktop/Programming/eXercise/api-keys.env")
load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
RASTER_DPI   = 300   # DPI for page image sent to Gemini
CROP_DPI     = 300   # DPI for final saved crop (from vector PDF)
PAD_FRACTION = 0.02  # 2% padding around detected bbox
OUT_BASE     = Path("/Users/joschka/Desktop/Programming/eXercise/scheme_graphics_test")

MODELS = [
]

QWEN_MODELS = [
    "qwen3-vl-plus",
]

PDFS = [
    Path("/Users/joschka/Desktop/IGCSE Computer Science 25/Scanned Exams/s23 12/CS s23 12 Ex. all_answers.pdf"),
    Path("/Users/joschka/Desktop/IGCSE Computer Science 25/Scanned Exams/s23 22/CS s23 22 Ex. all_answers.pdf"),
    Path("/Users/joschka/Desktop/IGCSE Computer Science 25/Scanned Exams/w23 13/CS w23 13 Ex. all_answers.pdf"),
    Path("/Users/joschka/Desktop/IGCSE Computer Science 25/Scanned Exams/w23 23/CS w23 23 Ex. all_answers.pdf"),
]

# ---------------------------------------------------------------------------
# Output schema — Gemini native bbox: [y_min, x_min, y_max, x_max] on 0-1000
# ---------------------------------------------------------------------------
class Graphic(BaseModel):
    bbox: list[int]   # [y_min, x_min, y_max, x_max], integers 0-1000
    description: str  # e.g. "circuit diagram", "network diagram"

class PageGraphics(BaseModel):
    graphics: list[Graphic]

SCHEMA = PageGraphics.model_json_schema()

# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------
PROMPT = (
    "Identify diagrams, figures, and illustrations on this page — things a human would "
    "describe as 'a drawing' or 'a figure'. This includes circuit diagrams, logic gate "
    "diagrams, network diagrams, ray diagrams, graphs with plotted data or axes, labeled "
    "physical setups, geometric figures, flowcharts, and maps.\n\n"
    "This does NOT include: tables (even tables with borders), truth tables, mathematical "
    "equations or expressions, pseudocode, program code, text with unusual formatting, "
    "page decorations, logos, or page numbers. Don't include text lines beside the graphic. \n\n"
    "For each graphic, return its bounding box as [y_min, x_min, y_max, x_max] with "
    "integer coordinates on a 0–1000 scale (0=top-left, 1000=bottom-right of the image). "
    "Cover the complete graphic including all its labels, annotations, and captions. "
    "Return an empty list if the page has no graphics."
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def bbox_to_clip(bbox: list[int], page_rect) -> "fitz.Rect":
    import fitz
    y_min, x_min, y_max, x_max = [c / 1000 for c in bbox]
    clip = fitz.Rect(
        x_min * page_rect.width,
        y_min * page_rect.height,
        x_max * page_rect.width,
        y_max * page_rect.height,
    )
    pad = PAD_FRACTION * max(page_rect.width, page_rect.height)
    clip += (-pad, -pad, pad, pad)
    clip &= page_rect
    return clip


def run(pdf_path: Path, client, model: str, out_dir: Path) -> None:
    import fitz, json
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from google.genai import types as T

    print(f"\n  {pdf_path.name}")
    doc = fitz.open(str(pdf_path))
    n   = doc.page_count

    # Rasterize all pages to PNG bytes at RASTER_DPI
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
                max_output_tokens=16384,
                thinking_config=T.ThinkingConfig(thinking_budget=8192, include_thoughts=False),
            ),
        )
        data = json.loads(resp.text or '{"graphics":[]}')
        graphics = [Graphic(**g) for g in data.get("graphics", [])]
        return page_num, graphics, round(time.perf_counter() - t0, 1)

    # Query all pages in parallel
    results: dict[int, list[Graphic]] = {}
    with ThreadPoolExecutor(max_workers=n) as pool:
        futures = {pool.submit(query_page, p): p for p in range(1, n + 1)}
        for fut in as_completed(futures):
            page_num, graphics, elapsed = fut.result()
            results[page_num] = graphics
            print(f"    p{page_num}: {len(graphics)} graphic(s)  ({elapsed}s)")
    results_list = [(p, results[p]) for p in range(1, n + 1)]

    # Save crops from the original vector PDF
    found_any = False
    for page_num, graphics in results_list:
        page = doc[page_num - 1]
        for idx, g in enumerate(graphics, 1):
            clip = bbox_to_clip(g.bbox, page.rect)
            pix  = page.get_pixmap(dpi=CROP_DPI, clip=clip)
            safe = re.sub(r"[^\w]", "_", g.description)[:40]
            path = out_dir / f"p{page_num}_{idx:02d}_{safe}.png"
            pix.save(str(path))
            print(f"    page {page_num}  bbox={g.bbox}  {g.description}")
            print(f"    → {path.name}  ({pix.width}×{pix.height}px)")
            found_any = True

    if not found_any:
        print("    no graphics found")


def run_qwen(pdf_path: Path, client, model: str, out_dir: Path) -> None:
    import fitz, json, base64
    from concurrent.futures import ThreadPoolExecutor, as_completed

    print(f"\n  {pdf_path.name}")
    doc = fitz.open(str(pdf_path))
    n   = doc.page_count

    page_pngs: dict[int, bytes] = {}
    for i in range(n):
        pix = doc[i].get_pixmap(dpi=RASTER_DPI)
        page_pngs[i + 1] = pix.tobytes("png")

    schema_str = json.dumps(SCHEMA, indent=2)
    system_msg = (
        f"You are a graphic-detection assistant. "
        f"Respond ONLY with valid JSON matching this schema:\n{schema_str}\n\n"
        f"Return bounding boxes as [x_min, y_min, x_max, y_max] with integer "
        f"coordinates on a 0–1000 scale (0=top-left, 1000=bottom-right of the image)."
    )

    def query_page(page_num: int) -> tuple[int, list[Graphic], float]:
        t0  = time.perf_counter()
        b64 = base64.b64encode(page_pngs[page_num]).decode()
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user", "content": [
                    {"type": "image_url",
                     "image_url": {"url": f"data:image/png;base64,{b64}"}},
                    {"type": "text", "text": PROMPT.replace(
                        "[y_min, x_min, y_max, x_max]",
                        "[x_min, y_min, x_max, y_max]",
                    )},
                ]},
            ],
            response_format={"type": "json_object"},
            extra_body={"enable_thinking": False},
        )
        data = json.loads(resp.choices[0].message.content or '{"graphics":[]}')
        graphics = []
        for g in data.get("graphics", []):
            # Qwen-VL returns [x_min, y_min, x_max, y_max]; convert to [y_min, x_min, y_max, x_max]
            x0, y0, x1, y1 = g["bbox"]
            graphics.append(Graphic(bbox=[y0, x0, y1, x1], description=g["description"]))
        return page_num, graphics, round(time.perf_counter() - t0, 1)

    results: dict[int, list[Graphic]] = {}
    with ThreadPoolExecutor(max_workers=n) as pool:
        futures = {pool.submit(query_page, p): p for p in range(1, n + 1)}
        for fut in as_completed(futures):
            page_num, graphics, elapsed = fut.result()
            results[page_num] = graphics
            print(f"    p{page_num}: {len(graphics)} graphic(s)  ({elapsed}s)")
    results_list = [(p, results[p]) for p in range(1, n + 1)]

    found_any = False
    for page_num, graphics in results_list:
        page = doc[page_num - 1]
        for idx, g in enumerate(graphics, 1):
            clip = bbox_to_clip(g.bbox, page.rect)
            pix  = page.get_pixmap(dpi=CROP_DPI, clip=clip)
            safe = re.sub(r"[^\w]", "_", g.description)[:40]
            path = out_dir / f"p{page_num}_{idx:02d}_{safe}.png"
            pix.save(str(path))
            print(f"    page {page_num}  bbox={g.bbox}  {g.description}")
            print(f"    → {path.name}  ({pix.width}×{pix.height}px)")
            found_any = True

    if not found_any:
        print("    no graphics found")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
import fitz

api_key = (os.environ.get("GEMINI_API_KEY", "") or os.environ.get("GOOGLE_API_KEY", "")).strip()
if not api_key:
    sys.exit("GEMINI_API_KEY not set")

from google import genai as gai
client = gai.Client(api_key=api_key)

import shutil

for model in MODELS:
    print(f"\n{'='*55}\n{model}\n{'='*55}")
    out_dir = OUT_BASE / model
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    for pdf in PDFS:
        run(pdf, client, model, out_dir)

if QWEN_MODELS:
    from openai import OpenAI
    dashscope_key = os.environ.get("DASHSCOPE_API_KEY", "").strip()
    if not dashscope_key:
        sys.exit("DASHSCOPE_API_KEY not set")
    qwen_client = OpenAI(
        api_key=dashscope_key,
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
    )
    for model in QWEN_MODELS:
        print(f"\n{'='*55}\n{model}\n{'='*55}")
        out_dir = OUT_BASE / model
        if out_dir.exists():
            shutil.rmtree(out_dir)
        out_dir.mkdir(parents=True)
        for pdf in PDFS:
            run_qwen(pdf, qwen_client, model, out_dir)

print(f"\nImages saved to: {OUT_BASE}")
