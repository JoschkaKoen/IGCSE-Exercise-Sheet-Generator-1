# Exam PDF libraries

These directories hold Cambridge-style question papers (and mark schemes) used by **natural-language** mode (`resolve_natural_language` lists filenames for the model), the on-screen **eXam** practice bank, and the **xScore** marking pipeline's calibration set.

The layout is `exams/<level>/<subject>_<syllabus_code>/`:

| Path | Level | Subject (Cambridge syllabus code) |
|------|-------|------------------------------------|
| `igcse/physics_0625/` | IGCSE | Physics (0625) |
| `igcse/chemistry_0620/` | IGCSE | Chemistry (0620) |
| `igcse/biology_0610/` | IGCSE | Biology (0610) |
| `igcse/mathematics_0580/` | IGCSE | Mathematics (0580) |
| `igcse/computer_science_0478/` | IGCSE | Computer Science (0478) |
| `igcse/business_studies_0450/` | IGCSE | Business Studies (0450) |
| `igcse/economics_0455/` | IGCSE | Economics (0455) |
| `a_level/physics_9702/` | A-Level | Physics (9702) |
| `a_level/biology_9700/` | A-Level | Biology (9700) |
| `a_level/chemistry_9701/` | A-Level | Chemistry (9701) |
| `a_level/computer_science_9618/` | A-Level | Computer Science (9618) |
| `a_level/business_9609/` | A-Level | Business (9609) |
| `a_level/economics_9708/` | A-Level | Economics (9708) |

Paths are configured in `eXercise/config.py` as `EXAM_ROOT_BY_KEY`, keyed by `<level>_<subject>` slugs (e.g. `igcse_physics`, `a_level_business`).

**Licensing:** Past papers are © Cambridge University Press & Assessment. Use only in line with their terms; this repo stores them for personal/educational tooling convenience.
