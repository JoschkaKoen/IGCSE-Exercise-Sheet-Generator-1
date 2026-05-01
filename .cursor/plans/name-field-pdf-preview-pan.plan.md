---
name: Name field + PDF preview pan
overview: "Shift the name block 1pt left; clarify n-up name-field behavior; fix horizontal pan at high zoom by addressing the real CSS constraint (max-width on page wraps) with JS/CSS fallbacks."
todos:
  - id: name-pt
    content: Reduce _NAME_BOX_PAD_X 6→5 in rendering.py; confirm pdfjam_post erase band still covers (optional _NF_LINE tweak if line alignment is audited)
  - id: preview-max-width
    content: Remove or relax .pdf-page-wrap max-width so zoomed page width can exceed viewport and horizontal scroll works
  - id: preview-fallback-wheel
    content: Only if needed after CSS fix — wheel deltaX → scrollLeft on .pdf-viewport-scroll (non-passive where preventDefault required; never break ctrl/meta pinch on pdfPane)
---

# Revised plan: Name label, n-up explanation, PDF horizontal pan

## 1. Move the name block 1pt left

**File:** [extract_exercises/rendering.py](extract_exercises/rendering.py)

**Change:** Set **`_NAME_BOX_PAD_X` from `6.0` to `5.0`**. The origin `x0 = _MARGIN_PT + _NAME_BOX_PAD_X` drives the **"Name: "** text and the write-in box (`box_x0 = x0 + w_label`) together; the erase rect already spans `_MARGIN_PT` through the box — everything stays aligned.

**pdfjam / n-up cleanup:** [extract_exercises/pdfjam_post.py](extract_exercises/pdfjam_post.py) uses a **fixed strip** `_NF_X0,_NF_Y0` → `_NF_X1,_NF_Y1` with **`_NF_X0 = 0`**, **`_NF_X1 = 170`** — a generous left band. Shifting the drawn name field **1pt left** stays inside that rectangle; **no constant update is strictly required** for the white-out. If you ever tighten `_NF_*` to minimal bounds, re-derive them from `_MARGIN_PT`, `_NAME_BOX_PAD_X`, `_NAME_BOX_W`, and label metrics in one place.

**Optional audit:** `_NF_LINE_X0 = 18.0` matches the **centred label’s** left decorative segment (`_MARGIN_PT + 8` pad in `_draw_label`), not `x0` for "Name:". Erasing and line redraw are separate; only change `_NF_LINE_*` if visual QA shows a gap after margin/name tweaks.

---

## 2. Why name fields appear on “wrong” subpages (n-up)

**Cause:** In 1-up, every page with a header includes `_draw_name_box()`. **pdfjam** places **whole pages** into a 2×2 or 2×1 grid, so **each tile carries a full copy** of the header — including "Name:" — until post-processing runs.

**Mitigation (already in repo):** [`_fix_nup_name_fields`](extract_exercises/pdfjam_post.py) **whites out** the name-field band on **every sub-slot except** **`row == 0` and `col == 0`**, then redraws the IGCSE horizontal line across that band so only the write-in area disappears.

**If something still looks wrong:** (a) pdfjam not invoked or post-step skipped on a code path; (b) future drift between `_draw_name_box` geometry and `_NF_*`; (c) misunderstanding of which tile is “first” — only **top-left** of each output page keeps the name field.

---

## 3. Horizontal trackpad pan at high zoom (revised root cause)

### Primary fix: `.pdf-page-wrap { max-width: 100%; }`

**File:** [web/static/css/09-overview.css](web/static/css/09-overview.css) (lines ~129–135)

Each page wrap gets an explicit **`width` in pixels** from [`renderPdfContinuous`](web/templates/index.html) (`pageWrap.style.width = dispW + 'px'`), where `dispW` grows with **`userScale = baseFit * s.zoom`**. **`max-width: 100%`** caps the **used width** to the scroll container’s content width, so when zoomed in the layout no longer exceeds the viewport — **`scrollWidth` ≈ `clientWidth`** and **there is nothing to scroll horizontally**. Vertical scroll still works because the **column** of pages grows in height.

**Direction:** Remove **`max-width: 100%`** from `.pdf-page-wrap`, or override it in the preview context (e.g. `.pdf-viewport-scroll .pdf-page-wrap { max-width: none; }` in [web/static/css/08-workspace-pdf-preview.css](web/static/css/08-workspace-pdf-preview.css)) so zoomed pages can be wider than the pane.

**Regression check:** `baseFit` uses `min(scaleW, scaleH) * 1.02`; at **zoom 1** pages are usually ≤ viewport width, but a **small** horizontal scrollbar could appear in edge cases if the product marginally exceeds width — confirm visually. If that is undesirable, scope the override to **preview mode + zoomed** via a class toggled from the existing PDF state (heavier; only if QA requires it).

### Pinch zoom and `transform` (context only)

[`index.html`](web/templates/index.html) applies **`transform: scale(...)`** on **`.pdf-pages-stack`** during **ctrl/meta + wheel** pinch. That path clears on settle and calls `renderPdfContinuous`. Steady-state “high zoom” issues are still overwhelmingly explained by **`max-width`** on wraps; the transform is transient.

### Secondary fallback (only if needed after removing `max-width`)

If Safari still maps horizontal trackpad to **`wheel`** with **`deltaX`** in a way that does not scroll the element:

- Listen on **`.pdf-viewport-scroll`** (same nodes as `bindPdfSmoothWheelScroll`), not only `#pdf-preview-pane`.
- When **`!e.ctrlKey && !e.metaKey`** and horizontal intent is clear (`Math.abs(deltaX) > Math.abs(deltaY)` or `deltaX` threshold), **`scrollLeft += deltaX`**.
- Use **`{ passive: false }`** only if **`preventDefault()`** is required; avoid stealing events from the pinch handler on **`pdfPane`** (which already gates on ctrl/meta).

---

## 4. Implementation order

1. **`_NAME_BOX_PAD_X`** — one line; run a generated n-up PDF through a quick visual check.
2. **`max-width` fix** on `.pdf-page-wrap` — retest macOS trackpad horizontal pan at high zoom on exercise + 2-up tabs.
3. **`deltaX` wheel fallback** — only if step 2 is insufficient.

## 5. Verification checklist

- Name field 1pt left vs previous export (1-up PDF).
- 2-up / 4-up: only top-left tile shows name; line continuous on other tiles.
- Preview: zoom in until page width > pane; two-finger horizontal pan moves content; vertical pan unchanged; pinch-zoom unchanged.
