---
name: detect_graphics_user
version: v1
description: User prompt for the per-page mark-scheme graphics detector (step 18).
---

Identify diagrams, figures, and illustrations on this page — things a human would describe as 'a drawing' or 'a figure'. This includes circuit diagrams, logic gate diagrams, network diagrams, ray diagrams, graphs with plotted data or axes, labeled physical setups, geometric figures, flowcharts, and maps.

This does NOT include: tables (even tables with borders), truth tables, mathematical equations or expressions, pseudocode, program code, text with unusual formatting, page decorations, logos, or page numbers. Don't include text lines beside the graphic.

For each graphic return:
  question_number — the question number as printed in the mark scheme (e.g. "3(b)(ii)")
  bbox            — [x_min, y_min, x_max, y_max] as integers on a 0–1000 scale
  description     — short label (e.g. "circuit diagram")

Return an empty graphics list if the page has no graphics.
