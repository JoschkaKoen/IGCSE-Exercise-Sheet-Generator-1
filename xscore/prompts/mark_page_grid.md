---
name: mark_page_grid
version: v1
description: Marking system prompt — grid navigation (Section E). Appended only when the page is split into a multi-subpage layout (rows × cols > 1). Placeholders ${rows}, ${cols}, ${subpage_ref} filled by the caller.
---
This page is divided into a ${rows}×${cols} grid — the ${subpage_ref} at the top of the blueprint label each quadrant. Each question's subpage_row and subpage_col identify its quadrant; do not confuse answers from different quadrants. order_in_subpage (1 = topmost) gives the vertical position within a quadrant. The same question number may appear more than once — always identify questions by subpage_row + subpage_col + question text, not by number alone.
