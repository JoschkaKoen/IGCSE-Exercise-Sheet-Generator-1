# syntax=docker/dockerfile:1
#
# Multi-stage build:
#   deps — system packages (TeX/JDK/gcc/poppler/tesseract) + Python deps. Changes
#          rarely, so BuildKit reuses this whole stage on code-only rebuilds.
#   app  — the application source on top of deps; the only layer that rebuilds on a
#          normal code push.
# apt and pip use BuildKit cache mounts, so a cold rebuild reuses already-downloaded
# Debian packages and Python wheels instead of re-fetching them.

FROM python:3.12-slim AS deps

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# System tools:
#   texlive-*        : pdflatex / xelatex (MCQ explanations, step-13 PDF reports)
#   texlive-lang-cjk : xeCJK + Fandol fonts for the printable handout PDFs
#                      (web/handout_latex.py; FandolSong is bundled here, no system font)
#   pdfjam / pdftk   : 2-up / 4-up variants (via pdftk-java in texlive-extra-utils)
#   poppler-utils    : pdfinfo / pdfimages / pdftotext used by the scan pipeline
#   tesseract-ocr    : OCR fallback used by some preprocessing steps
#   openjdk-21-*     : javac/java for the server-side Java runner (web/java_runner.py);
#                      Debian trixie has no openjdk-17, JDK 21 + `--release 8` still emits
#                      Java-8 bytecode.
#   gcc, libc6-dev   : C compiler + C standard-library headers for the server-side C
#                      runner (web/c_runner.py). libc6-dev is REQUIRED for <stdio.h>,
#                      <math.h>, and the `-lm` link symlink, and --no-install-recommends
#                      skips it (a Recommends of gcc) unless listed here.
# The apt cache mounts keep the downloaded .deb archives and package lists in the
# BuildKit cache (not the image), so we neither re-download them on a cold rebuild
# nor need `rm -rf /var/lib/apt/lists` to stay slim.
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
    texlive-extra-utils \
    texlive-latex-extra \
    texlive-fonts-extra \
    texlive-xetex \
    texlive-lang-cjk \
    poppler-utils \
    tesseract-ocr \
    openjdk-21-jdk-headless \
    gcc \
    libc6-dev

# mhchem (chemistry notation) is not a standalone apt package in this Debian
# release; vendor the single .sty file (see vendor/mhchem.sty).
RUN mkdir -p /usr/share/texmf/tex/latex/mhchem
COPY vendor/mhchem.sty /usr/share/texmf/tex/latex/mhchem/mhchem.sty
RUN mktexlsr

# Python deps via a pip cache mount (wheels are cached, so no --no-cache-dir needed).
# Then force the *headless* OpenCV: rapidocr-onnxruntime pulls full opencv-python in
# transitively, but this app makes no GUI cv2 calls, so swap it for the slimmer
# headless build (one cv2, no GTK/Qt). See requirements.txt — opencv is left to rapidocr.
COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements.txt \
 && pip uninstall -y opencv-python \
 && pip install "opencv-python-headless>=4.9.0"

# Create the non-root user here so the app stage can COPY --chown against it.
RUN useradd -m app

FROM deps AS app

WORKDIR /app

# Make the volume mountpoints app-owned so fresh output_data / log_data named volumes
# are writable by the non-root user (uid 1000). Done before COPY so the code copy
# (which excludes output/ and logs/ via .dockerignore) cannot clobber the ownership.
RUN mkdir -p /app/output /app/logs && chown app:app /app/output /app/logs

# COPY --chown folds ownership into the copy, avoiding a full-tree `chown -R /app`
# layer on every code-only rebuild.
COPY --chown=app:app . .

USER app

EXPOSE 8000

CMD ["uvicorn", "web.app:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
