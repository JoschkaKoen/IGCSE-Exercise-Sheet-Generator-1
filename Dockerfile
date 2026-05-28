# syntax=docker/dockerfile:1
FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# System tools:
#   texlive-*      : pdflatex / xelatex (MCQ explanations, step-13 PDF reports)
#   pdfjam / pdftk : 2-up / 4-up variants (via pdftk-java in texlive-extra-utils)
#   poppler-utils  : pdfinfo / pdfimages / pdftotext used by scan pipeline
#   tesseract-ocr  : OCR fallback used by some preprocessing steps
RUN apt-get update && apt-get install -y --no-install-recommends \
    texlive-extra-utils \
    texlive-latex-extra \
    texlive-fonts-extra \
    texlive-xetex \
    poppler-utils \
    tesseract-ocr \
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
