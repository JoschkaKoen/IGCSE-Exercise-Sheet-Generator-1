"""Pixel-statistics blank-page check used by xscore.preprocessing.scan_orientation."""

from __future__ import annotations

import numpy as np
from PIL import Image

# ---------------------------------------------------------------------------
# Configuration defaults
# ---------------------------------------------------------------------------
BLANK_MEAN_THRESHOLD = 250  # Pages with grayscale mean above this are considered blank (0-255)
BLANK_STD_THRESHOLD = 6     # Pages with grayscale std below this are considered blank


def is_blank_page(
    image: Image.Image,
    mean_threshold: float = BLANK_MEAN_THRESHOLD,
    std_threshold: float = BLANK_STD_THRESHOLD,
) -> bool:
    gray_img = image if image.mode == "L" else image.convert("L")
    gray = np.array(gray_img, dtype=np.float32)
    mean = gray.mean()
    std = gray.std()
    return (mean >= mean_threshold) and (std <= std_threshold)
