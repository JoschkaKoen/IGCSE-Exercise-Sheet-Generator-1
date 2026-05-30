# syntax=docker/dockerfile:1
FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# System tools:
#   texlive-*      : pdflatex / xelatex (MCQ explanations, step-13 PDF reports)
#   texlive-lang-cjk : xeCJK + Fandol fonts for the printable handout PDFs
#                    (web/handout_latex.py; FandolSong is bundled here, no system font)
#   pdfjam / pdftk : 2-up / 4-up variants (via pdftk-java in texlive-extra-utils)
#   poppler-utils  : pdfinfo / pdfimages / pdftotext used by scan pipeline
#   tesseract-ocr  : OCR fallback used by some preprocessing steps
#   openjdk-21-*   : javac/java for the server-side Java runner (web/java_runner.py).
#                    Base image is Debian trixie (no openjdk-17); JDK 21 + `--release 8`
#                    still emits Java-8 bytecode, so the runner is unchanged.
#   gcc            : C compiler for the server-side C runner (web/c_runner.py).
#                    Links against the glibc/libm already in the base image (-lm).
RUN apt-get update && apt-get install -y --no-install-recommends \
    texlive-extra-utils \
    texlive-latex-extra \
    texlive-fonts-extra \
    texlive-xetex \
    texlive-lang-cjk \
    poppler-utils \
    tesseract-ocr \
    openjdk-21-jdk-headless \
    gcc \
 && rm -rf /var/lib/apt/lists/*

# mhchem (chemistry notation) is not a standalone apt package in this Debian
# release; vendor the single .sty file under vendor/ (see vendor/mhchem.sty).
RUN mkdir -p /usr/share/texmf/tex/latex/mhchem
COPY vendor/mhchem.sty /usr/share/texmf/tex/latex/mhchem/mhchem.sty
RUN mktexlsr

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Run as a non-root user for security.
RUN useradd -m app && chown -R app:app /app
USER app

EXPOSE 8000

CMD ["uvicorn", "web.app:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
