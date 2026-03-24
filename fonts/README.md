# Latin Modern (LaTeX-style text)

The files `lmroman10-regular.otf` and `lmroman10-bold.otf` are the **Latin Modern Roman** fonts (OpenType), the standard serif family used by `\\usepackage{lmodern}` in LaTeX — visually aligned with Knuth’s Computer Modern.

- **Source:** [CTAN `fonts/lm`](https://www.ctan.org/pkg/lm) (GUST Font License).
- **Bundled** so the extractor does not depend on a local TeX installation.

If you remove these files, the app falls back to Arial / system fonts (see `extract_exercises/fonts.py`).
