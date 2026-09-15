# math_converter — handwritten math PDF → LaTeX

Converts a handwritten math PDF into clean, compilable LaTeX using a local
vision-capable LLM (Qwen3.5-9B with mmproj) served by KoboldCPP. The
conversion rules come from `SKILL.md`: **transcribe, never solve** — the model
must preserve the author's math exactly, including any errors, and mark truly
ambiguous symbols with `\text{[UNCLEAR]}`.

Pure Python 3 standard library — no `pip install` needed.

## Pipeline

```
homework.pdf ──pdftoppm──▶ page PNGs ──Kobold vision API──▶ per-page LaTeX ──stitch──▶ homework.tex ──(pdflatex)──▶ homework.pdf
```

1. `pdftoppm` renders each page to PNG (200 dpi by default).
2. Each page image is sent to the model (OpenAI-compatible
   `/v1/chat/completions` with an image part). Page 1 returns a full document;
   later pages return body fragments.
3. Pages are stitched into one `.tex` (preamble from page 1 is kept).
4. With `--compile`, `pdflatex` runs two passes to produce a PDF.

## Prerequisites

- **KoboldCPP** — download the binary for your platform from
  <https://github.com/LostRuins/koboldcpp/releases>.
- A **vision model + its mmproj projector** from the same Hugging Face repo
  (e.g. Qwen3.5-9B VL GGUF). Put both files in one directory.
- `poppler-utils` (provides `pdftoppm`):
  `sudo apt install poppler-utils` (Debian/Ubuntu).
- `texlive` (provides `pdflatex`) — optional, only needed for `--compile`:
  `sudo apt install texlive` (CJK output additionally needs `texlive-xetex`
  and a CJK font such as `fonts-noto-cjk`).

Start the server with vision enabled:

```bash
koboldcpp --mmproj /path/to/mmproj.gguf /path/to/model.gguf --port 5001
# wait for "Please connect to custom endpoint"
```

## Install

```bash
git clone https://github.com/<you>/math_converter.git
cd math_converter
sudo install -m 0755 latexify /usr/local/bin/latexify   # optional
```

Or skip the install and run `./latexify ...` directly from the repo —
the wrapper resolves `pipeline.py` relative to its own location, so the
clone can live anywhere.

## Usage

```bash
# terminal 1 — KoboldCPP with vision loaded (see above)

# terminal 2 — convert
latexify homework.pdf --pdf ~/out/homework_final.pdf

# preview where everything would go, without converting anything
latexify homework.pdf --dry-run
```

The stitched `.tex` is the intermediate artifact and always stays in
`homework_latex/homework.tex` next to the input. With `--pdf PATH` the final
compiled PDF is written to exactly `PATH` (implies `--compile`); with plain
`--compile` it lands next to the `.tex` as `<pdf stem>.pdf`.

The wrapper passes everything through, so `python3 pipeline.py ...` from the
repo root keeps working too.

Useful flags:

| flag | default | meaning |
|---|---|---|
| `-o DIR` | `<pdf stem>_latex/` | output directory (holds the intermediate .tex) |
| `--pdf PATH` | `<outdir>/<pdf stem>.pdf` | exact path for the final PDF (implies `--compile`) |
| `--compile` | off | compile the .tex; PDF lands next to it |
| `--port 5001` | 5001 | KoboldCPP port |
| `--pages 2-5` | all | page subset |
| `--dpi 250` | 200 | render resolution |
| `--max-tokens 1600` | 1600 | generation cap per page |
| `--temperature 0.2` | 0.2 | keep low for faithful transcription |
| `--keep-pages` | off | keep the rendered PNGs next to the .tex |
| `--dry-run` | off | print input/output paths (PDF, .tex, outdir) and exit; nothing written |

## Tests

`tests/` contains a sample PDF (generated, not handwritten, please only use as smoke test) and its expected `.tex` output for
manual comparison.

## Troubleshooting

- **"attempt N failed" / connection refused** — the server isn't up yet; wait
  for the model to finish loading (~30 s).
- **Vision not used / text-only reply** — confirm the server log says
  `MultimodalVision` under *Active Modules*. If not, the mmproj failed to
  load; check the `--mmproj` path you passed to KoboldCPP.
- **Truncated/damaged LaTeX on dense pages** — raise `--max-tokens`, or
  convert with `--pages` in chunks.
- **`\text{[UNCLEAR]}` in output** — the model couldn't resolve the symbol
  from context; check that spot in the original by hand.
- **Handwritten Chinese/CJK characters slightly off** — known weak spot;
  the math is unaffected and the text lands in `\text{...}` where it's easy
  to correct manually. Printed CJK transcribes better than cursive.
- **Compile fails** — open the `.log` file next to the `.tex`; syntax fixes
  must not change the math content.
