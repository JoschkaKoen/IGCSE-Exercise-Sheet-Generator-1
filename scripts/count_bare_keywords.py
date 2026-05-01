import re, sys, glob

KEYWORDS = r"REPEAT|UNTIL|FOR|NEXT|ENDFOR|WHILE|ENDWHILE|IF|THEN|ELSE|ENDIF|CASE|ENDCASE|OTHERWISE|PROCEDURE|ENDPROCEDURE|FUNCTION|ENDFUNCTION|RETURN|DECLARE|INPUT|OUTPUT"
pat = re.compile(rf"(?<![A-Za-z\\{{])({KEYWORDS})(?![A-Za-z}}])")
alltt = re.compile(r"\\begin\{alltt\}.*?\\end\{alltt\}", re.S)

total = 0
files_with_hits = 0
for f in glob.glob(sys.argv[1]):
    t = alltt.sub("", open(f).read())
    hits = pat.findall(t)
    if hits:
        files_with_hits += 1
        total += len(hits)
        print(f"{f}: {len(hits)} bare keyword(s) — {sorted(set(hits))}")
print(f"\nTOTAL: {total} bare keyword occurrence(s) across {files_with_hits} file(s)")
