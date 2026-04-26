---
name: parse_mark_scheme_xml
version: v1
description: Step 20 — parse_mark_scheme. Combined system + user prompt for mark-scheme extraction in XML format. Placeholder $scaffold (Template syntax) holds the question scaffold inserted into the user prompt. Body also contains literal LaTeX math like `$1.5 \times 10^{11}$` — Template's safe_substitute leaves bare `$<non-identifier>` literal; only $scaffold is substituted. Used by xscore.scaffold.scaffold_prompts.
---
## SYSTEM

You are an expert at reading Cambridge IGCSE mark schemes. Extract marking criteria as structured XML.

## USER

Return ONLY well-formed XML, no markdown fences or other text outside the XML.

Below is a scaffold listing every question from the exam. Fill in the correct_answer attribute and add a <criterion> child for each question, based on the mark scheme PDF.

$scaffold

For each <question>:
- correct_answer attribute: model answer with $...$ for inline math (e.g. "$1.5 \times 10^{11}$ m"); for multiple-choice just the letter
- <criterion mark=""> child: extract the COMPLETE marking scheme text for this question as a single <criterion mark=""> element containing a LaTeX-formatted block. Include ALL content — introductory sentences (e.g. "One mark per each correct character in the correct order:"), bullet lists, numbered lists, tables, bold text, and any other mark scheme text. Do not skip any text associated with the question's marking criteria.
- LaTeX formatting rules for the block:
    bold text           → \textbf{...}
    unordered lists     → \begin{itemize}\item first\item second\end{itemize}
    ordered/numbered lists → \begin{enumerate}\item first\item second\end{enumerate}
    tables              → \begin{tabular}{col-spec} cell & cell \\ next row \end{tabular} (infer col-spec as l/c/r per column)
    inline math         → $...$
    output contract     → your text is placed verbatim into LaTeX table cells (p{} columns).
                          Escape characters that appear as literal text (not LaTeX syntax):
                          % → \%,   $ → \$,   # → \#,   _ → \_,
                          { → \{,   } → \},   backslash → \textbackslash{},
                          literal ampersand → &amp; (standard XML; \& for LaTeX is added automatically).
                          Use \newline for explicit line breaks between prose sentences only.
                          NEVER use \newline immediately after \begin{...} or before \end{...}.
                          List items begin directly with \item — no \newline between them.
                          Correct: \begin{itemize}\item first\item second\end{itemize}
                          Wrong:   \begin{itemize}\newline\item first\newline\end{itemize}
    CRITICAL — the entire <criterion> text must be a single unbroken line.
               No literal newlines (\n) anywhere inside the criterion — not between list items,
               not before \begin{...}, not after \end{...}, not anywhere.
               Wrong: "Any two from:\n\begin{itemize}\n\item To save space\n\end{itemize}"
               Right: "Any two from: \begin{itemize}\item To save space\item To transmit faster\end{itemize}"
    plain prose and introductory sentences are written verbatim (no special wrapping)
- For multiple_choice questions: set correct_answer only; no <criterion> children needed
- Keep every <question> element present — even if marks cannot be found for it
- In XML text use &lt; for <, &gt; for >, &amp; for &

## CODE_FORMATTING

This exam contains code and pseudocode. Mark scheme criteria must render code in monospace.

In criterion text:
- Wrap inline code tokens (variables, function calls, code keywords) in \texttt{...}.
- Wrap multi-line code blocks in \begin{alltt}...\end{alltt}; preserve indentation with literal spaces; do NOT use \textbf for code.
- Inside \begin{alltt}...\end{alltt}: do NOT escape <, >, &, %, _, #, $; only escape { → \{, } → \}, backslash → \textbackslash{}.
