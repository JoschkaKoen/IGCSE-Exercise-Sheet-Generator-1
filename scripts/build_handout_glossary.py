# -*- coding: utf-8 -*-
"""Build a handout's glossary files + stamp its meta, after the .md is authored.

Given an ordered ``english<TAB>简体中文`` pairs file (first-appearance order),
this:
  1. auto-generates the pinyin column with pypinyin,
  2. writes ``output/eXam/handouts/<subject>/NN.glossary.tsv`` in that order,
  3. merges the terms into the per-subject master ``_glossary.tsv``
     (adds new english; WARNS if an existing english maps to different
     Chinese — the cross-file consistency check), re-sorted alphabetically,
  4. stamps ``NN.meta.yaml`` with language / simplified_at / glossed_at.

Usage::

    .venv/bin/python -m scripts.build_handout_glossary <subject> <topic> <pairs.tsv>
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml
from pypinyin import Style, pinyin

REPO = Path(__file__).resolve().parent.parent
ROOT = REPO / "output" / "eXam" / "handouts"
HEADER = "english\t简体中文\tpinyin\n"


def _py(zh: str) -> str:
    return " ".join(s[0] for s in pinyin(zh, style=Style.TONE))


def _read_pairs(path: Path) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        if "\t" in line:
            parts = line.split("\t")
        elif "=>" in line:
            parts = line.split("=>")
        else:
            continue
        if len(parts) < 2:
            continue
        eng, zh = parts[0].strip(), parts[1].strip()
        if eng.lower() == "english" or not eng or not zh:
            continue
        pairs.append((eng, zh))
    return pairs


def _write_tsv(path: Path, rows: list[tuple[str, str, str]]) -> None:
    path.write_text(HEADER + "".join(f"{e}\t{z}\t{p}\n" for e, z, p in rows), encoding="utf-8")


def _load_master(path: Path) -> dict[str, tuple[str, str, str]]:
    out: dict[str, tuple[str, str, str]] = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split("\t")
        if len(parts) < 2 or parts[0].strip().lower() == "english":
            continue
        e, z = parts[0].strip(), parts[1].strip()
        p = parts[2].strip() if len(parts) > 2 and parts[2].strip() else _py(z)
        out[e.lower()] = (e, z, p)
    return out


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    subject, topic, pairs_file = argv
    subj_dir = ROOT / subject
    if not subj_dir.is_dir():
        print(f"no such subject dir: {subj_dir}", file=sys.stderr)
        return 2
    pt = f"{int(topic):02d}"
    pairs = _read_pairs(Path(pairs_file))
    if not pairs:
        print("no pairs read", file=sys.stderr)
        return 2

    # 1–2. per-handout glossary (given order).
    per_rows = [(e, z, _py(z)) for e, z in pairs]
    _write_tsv(subj_dir / f"{pt}.glossary.tsv", per_rows)

    # 3. merge into master, warn on inconsistency.
    master_path = subj_dir / "_glossary.tsv"
    master = _load_master(master_path)
    warnings: list[str] = []
    for e, z in pairs:
        key = e.lower()
        if key in master:
            if master[key][1] != z:
                warnings.append(f"  WARN {e!r}: master={master[key][1]} but handout={z}")
        else:
            master[key] = (e, z, _py(z))
    _write_tsv(master_path, sorted(master.values(), key=lambda r: r[0].lower()))

    # 4. stamp meta.
    meta_path = subj_dir / f"{pt}.meta.yaml"
    meta = {}
    if meta_path.is_file():
        meta = yaml.safe_load(meta_path.read_text(encoding="utf-8")) or {}
    from datetime import datetime, timezone
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    meta["language"] = "en-simplified+zh-gloss"
    meta.setdefault("simplified_at", ts)
    meta["glossed_at"] = ts
    meta["author"] = "claude"
    meta_path.write_text(
        yaml.safe_dump(meta, sort_keys=False, allow_unicode=True, default_flow_style=False),
        encoding="utf-8",
    )

    print(f"{subject}/{pt}: {len(per_rows)} terms; master now {len(master)} terms")
    for w in warnings:
        print(w)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
