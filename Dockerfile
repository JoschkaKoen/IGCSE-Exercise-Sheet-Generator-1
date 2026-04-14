# syntax=docker/dockerfile:1
FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# System tools: pdflatex (MCQ explanations), pdfjam (2-up / 4-up variants)
# mhchem (chemistry notation) is not a standalone apt package in this Debian
# release; download the single .sty file directly from CTAN.
RUN apt-get update && apt-get install -y --no-install-recommends \
    texlive-extra-utils \
    texlive-latex-extra \
    texlive-fonts-extra \
    wget \
 && rm -rf /var/lib/apt/lists/* \
 && mkdir -p /usr/share/texmf/tex/latex/mhchem \
 && wget -q -O /usr/share/texmf/tex/latex/mhchem/mhchem.sty \
      "https://mirrors.ctan.org/macros/latex/contrib/mhchem/mhchem.sty" \
 && mktexlsr \
 && apt-get purge -y --auto-remove wget

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

CMD ["uvicorn", "web.app:app", "--host", "0.0.0.0", "--port", "8000"]
