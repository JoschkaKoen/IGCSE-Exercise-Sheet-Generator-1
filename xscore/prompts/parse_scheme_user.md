---
name: parse_scheme_user
version: v1
description: User prompt for mark-scheme extraction. {scaffold} is a Python str.format placeholder; literal LaTeX braces are escaped as {{ and }}. Callers do `body.format(scaffold=...)`.
---
Return ONLY well-formed XML, no markdown fences or other text outside the XML.

Below is a scaffold listing every question from the exam. Fill in the correct_answer attribute and add a <criterion> child for each question, based on the mark scheme PDF.

{scaffold}

For each <question>:
- correct_answer attribute: model answer with $...$ for inline math (e.g. "$1.5 \times 10^{{11}}$ m"); for multiple-choice just the letter
- <criterion mark=""> child: extract the COMPLETE marking scheme text for this question as a single <criterion mark=""> element containing a LaTeX-formatted block. Include ALL content — introductory sentences (e.g. "One mark per each correct character in the correct order:"), bullet lists, numbered lists, tables, bold text, and any other mark scheme text. Do not skip any text associated with the question's marking criteria.
- LaTeX formatting rules for the block:
    bold text           → \textbf{{...}}
    unordered lists     → \begin{{itemize}}\item first\item second\end{{itemize}}
    ordered/numbered lists → \begin{{enumerate}}\item first\item second\end{{enumerate}}
    tables              → \begin{{tabular}}{{col-spec}} cell & cell \\ next row \end{{tabular}} (infer col-spec as l/c/r per column)
    inline math         → $...$
    output contract     → your text is placed verbatim into LaTeX table cells (p{{}} columns).
                          Escape characters that appear as literal text (not LaTeX syntax):
                          % → \%,   $ → \$,   # → \#,   _ → \_,
                          {{ → \{{,   }} → \}},   backslash → \textbackslash{{}},
                          literal ampersand → &amp; (standard XML; \& for LaTeX is added automatically).
                          Use \newline for explicit line breaks between prose sentences only.
                          NEVER use \newline immediately after \begin{{...}} or before \end{{...}}.
                          List items begin directly with \item — no \newline between them.
                          Correct: \begin{{itemize}}\item first\item second\end{{itemize}}
                          Wrong:   \begin{{itemize}}\newline\item first\newline\end{{itemize}}
    CRITICAL — the entire <criterion> text must be a single unbroken line.
               No literal newlines (\n) anywhere inside the criterion — not between list items,
               not before \begin{{...}}, not after \end{{...}}, not anywhere.
               Wrong: "Any two from:\n\begin{{itemize}}\n\item To save space\n\end{{itemize}}"
               Right: "Any two from: \begin{{itemize}}\item To save space\item To transmit faster\end{{itemize}}"
    plain prose and introductory sentences are written verbatim (no special wrapping)
- For multiple_choice questions: set correct_answer only; no <criterion> children needed
- Keep every <question> element present — even if marks cannot be found for it
- In XML text use &lt; for <, &gt; for >, &amp; for &
