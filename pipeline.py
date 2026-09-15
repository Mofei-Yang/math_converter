#!/usr/bin/env python3
"""
math_converter/pipeline.py
Handwritten math PDF -> LaTeX via the local KoboldCPP model (vision mode).

Pipeline:
  1. pdftoppm renders each PDF page to PNG (default 200 dpi).
  2. Each page image is sent to Kobold's OpenAI-compatible chat API
     (/v1/chat/completions) as a base64 image, with a system prompt derived
     from math_converter/SKILL.md (transcribe, don't solve).
  3. Page 1 returns a full LaTeX document; pages 2+ return body fragments.
     The script stitches them into one .tex file (the intermediate artifact).
  4. Optionally compiles with pdflatex; use --pdf to choose the exact path
     the final PDF is written to.

Usage:
  latexify homework.pdf
  latexify homework.pdf --pdf ~/out/homework_final.pdf
  python3 math_converter/pipeline.py homework.pdf -o out/ --dpi 250 --compile

Requires: pdftoppm (poppler-utils), pdflatex (optional, for --compile/--pdf),
and KoboldCPP running with --mmproj (vision) on http://localhost:<port>.
"""

import argparse
import base64
import json
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------------------
# System prompt — condensed from math_converter/SKILL.md to fit the 4k context
# alongside page images. Same priority order: fidelity > completeness > ...
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """\
You are a transcription and typesetting engine for handwritten mathematics. \
You convert handwriting to LaTeX. You are NOT a solver.

Rules (from the project skill, highest priority first):
1. Mathematical fidelity: reproduce exactly what is written. Never fix, \
simplify, complete, reorder, or "improve" the author's math. If the author \
made an error, transcribe the error.
2. If a symbol is genuinely ambiguous, resolve it from mathematical context; \
if unresolvable, output \\text{[UNCLEAR]} instead of guessing.
3. Crossed-out material: ignore it if a replacement follows; otherwise mark
   it explicitly as \\sout{what was written} (e.g. \\sout{graph}); use
   \\sout{[illegible]} if the struck-through text is unreadable. Do not
   incorporate margin notes or arrows unless they are part of the solution.
4. Typesetting: use amsmath style. Inline math in $...$, standalone equations \
in \\[...\\], chains of equalities in align* aligned at '='. Use \\frac, \\sqrt, \
\\sum_{i=1}^{n}, \\mathbb{R} etc. as appropriate. Align at meaningful operators.
5. Structure: infer problems/proofs/subparts from the layout. Use \
\\section*{Problem N} only if the page shows such structure.
6. Distinguish commonly confused symbols using context: 0/O, 1/l, x/\\times, \
u/\\mu, v/\\nu, e/\\epsilon, \\subset/\\subseteq, sub/superscript grouping.

Output format (STRICT):
- If this is PAGE 1 of the document: output a COMPLETE standalone LaTeX \
document with a minimal preamble (documentclass article 11pt; packages \
amsmath, amssymb, amsthm, geometry margin=1in; add others only if needed), \
containing ONLY the content visible on this page.
- If this is a LATER page: output ONLY the LaTeX body fragment for that page. \
No preamble, no \\documentclass, no \\begin{document}.
- Raw LaTeX only. No markdown fences, no commentary, no explanations.
- Transcribe all visible content on the page; omit nothing.\
"""

EOG_STRINGS = ("<|im_end|>", "<|endoftext|>")

# Han, kana, hangul ranges — if the transcription contains these, pdflatex
# cannot handle it; we add fontspec + a CJK font family and compile with
# xelatex, wrapping CJK runs so they typeset with the right font.
CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]")
CJK_RUN_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]+")
CJK_PREAMBLE = ("\\usepackage{fontspec}\n"
                "\\newfontfamily\\cjkfamily{Noto Serif CJK SC}\n")
ULEM_PREAMBLE = "\\usepackage[normalem]{ulem}\n"
# Minimal strikeout for TeX installs without ulem: draw a rule through the box.
SOUT_FALLBACK = ("\\providecommand{\\sout}[1]{{\\setbox0=\\hbox{#1}%\n"
                 " \\rlap{\\rule[-0.55ex]{\\wd0}{0.4pt}}\\box0}}\n")
_ulem_available: bool | None = None


def _has_ulem() -> bool:
    global _ulem_available
    if _ulem_available is None:
        kp = shutil.which("kpsewhich")
        _ulem_available = bool(kp) and subprocess.run(
            ["kpsewhich", "ulem.sty"], capture_output=True, text=True
        ).stdout.strip() != ""
    return _ulem_available


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def log(msg: str) -> None:
    print(msg, flush=True)


def render_pdf_pages(pdf_path: Path, workdir: Path, dpi: int,
                     pages: str | None) -> list[Path]:
    """Render PDF pages to PNGs with pdftoppm; return sorted page paths."""
    png_prefix = workdir / "page"
    cmd = ["pdftoppm", "-png", "-r", str(dpi)]
    if pages:
        cmd += ["-f", pages.split("-")[0]]
        if "-" in pages:
            cmd += ["-l", pages.split("-")[1]]
    cmd += [str(pdf_path), str(png_prefix)]
    log(f"[1/4] Rendering {pdf_path.name} at {dpi} dpi ...")
    subprocess.run(cmd, check=True)
    out = sorted(workdir.glob("page-*.png"))
    if not out:
        raise RuntimeError("pdftoppm produced no pages")
    log(f"      {len(out)} page(s) rendered.")
    return out


def strip_fences(text: str) -> str:
    """Remove markdown code fences if the model added them."""
    text = text.strip()
    m = re.match(r"^```(?:latex|tex)?\s*\n(.*?)\n?```\s*$", text, re.S)
    if m:
        return m.group(1).strip()
    # unfenced opening fence with no closer (truncated generation)
    if text.startswith("```"):
        text = re.sub(r"^```(?:latex|tex)?\s*\n?", "", text)
        text = re.sub(r"\n?```\s*$", "", text)
    return text.strip()


def trim_eog(text: str) -> str:
    """Cut anything after an end-of-generation marker the API passed through."""
    for eog in EOG_STRINGS:
        idx = text.find(eog)
        if idx != -1:
            text = text[:idx]
    return text.strip()


def chat_once(base_url: str, png: Path, page_no: int, total: int,
              max_tokens: int, temperature: float) -> str:
    """One chat completion with a page image; returns the raw text."""
    b64 = base64.b64encode(png.read_bytes()).decode()
    data_uri = f"data:image/png;base64,{b64}"

    user_rule = (
        f"This is page {page_no} of {total} of the handwritten document."
        + (" Output the COMPLETE standalone LaTeX document for this page."
           if page_no == 1 else
           " Output ONLY the LaTeX body fragment for this page (no preamble).")
    )
    payload = {
        "model": "local",
        "max_tokens": max_tokens,
        "temperature": temperature,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": [
                {"type": "text", "text": user_rule},
                {"type": "image_url", "image_url": {"url": data_uri}},
            ]},
        ],
    }
    req = urllib.request.Request(
        f"{base_url}/v1/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=600) as resp:
        body = json.load(resp)
    return body["choices"][0]["message"]["content"] or ""


def convert_page(base_url: str, png: Path, page_no: int, total: int,
                 max_tokens: int, temperature: float, retries: int) -> str:
    """Convert one page with retries; returns cleaned LaTeX text."""
    label = f"[2/4] Page {page_no}/{total} ({png.name})"
    for attempt in range(1, retries + 1):
        t0 = time.time()
        try:
            log(f"{label} sending to model (attempt {attempt}) ...")
            raw = chat_once(base_url, png, page_no, total, max_tokens,
                            temperature)
            text = trim_eog(strip_fences(raw))
            if not text:
                raise RuntimeError("empty response")
            log(f"      done in {time.time() - t0:.0f}s, "
                f"{len(text)} chars"
                + ("  [contains \\text{[UNCLEAR]}]" if "[UNCLEAR]" in text else ""))
            return text
        except (urllib.error.URLError, urllib.error.HTTPError,
                RuntimeError, KeyError, json.JSONDecodeError) as e:
            log(f"      attempt {attempt} failed: {e}")
            if attempt == retries:
                raise RuntimeError(
                    f"page {page_no}: giving up after {retries} attempts") from e
            time.sleep(3 * attempt)
    raise RuntimeError("unreachable")


def strip_page_number(tex: str) -> str:
    """Remove centered blocks containing only digits (rendered page-number
    footers that the model sometimes transcribes as content)."""
    return re.sub(
        r"\\begin\{center\}\s*\d{1,4}\s*\\end\{center\}\s*", "", tex)


def extract_body(tex: str) -> str:
    """Return the body between \\begin{document} and \\end{document}."""
    m = re.search(r"\\begin\{document\}(.*?)\\end\{document\}", tex, re.S)
    return strip_page_number(m.group(1).strip() if m else tex.strip())


def stitch(page_texes: list[str]) -> str:
    """Stitch per-page LaTeX into one document; return final .tex source."""
    body = [extract_body(t) for t in page_texes]
    joined = "\n\n% ---- page break ----\n\n".join(body)
    if "\\documentclass" in page_texes[0]:
        # Reuse page 1's preamble, swap in the full stitched body.
        first = page_texes[0]
        m = re.match(r"(.*?)(\\begin\{document\}).*(\\end\{document\}).*$",
                     first, re.S)
        if m:
            return f"{m.group(1)}{m.group(2)}\n\n{joined}\n\n{m.group(3)}\n"
    # Fallback: model never produced a preamble — synthesize one.
    return (
        "\\documentclass[11pt]{article}\n\n"
        "\\usepackage{amsmath,amssymb,amsthm}\n"
        "\\usepackage{mathtools}\n"
        "\\usepackage{geometry}\n\n"
        "\\geometry{margin=1in}\n\n"
        "\\begin{document}\n\n"
        f"{joined}\n\n"
        "\\end{document}\n"
    )


def ensure_packages(tex: str) -> str:
    """Inject packages the model's markup needs but didn't declare
    (e.g. ulem for \\sout, or a fallback macro if ulem is not installed).
    Purely mechanical; content untouched."""
    if "\\sout" in tex and "ulem" not in tex:
        inject = ULEM_PREAMBLE if _has_ulem() else SOUT_FALLBACK
        anchor = "\\usepackage{geometry}"
        if anchor in tex:
            return tex.replace(anchor, anchor + "\n" + inject, 1)
        return tex.replace("\\begin{document}", inject + "\n\\begin{document}", 1)
    return tex


def add_cjk_support(tex: str) -> str:
    """If the body contains CJK text, patch the preamble for xelatex/fontspec
    and wrap CJK runs in the CJK font family. Content is not altered."""
    if not CJK_RE.search(tex):
        return tex
    # Wrap maximal CJK runs so they typeset with the CJK font.
    head, sep, body = tex.partition("\\begin{document}")
    if sep:
        body = CJK_RUN_RE.sub(r"{\\cjkfamily \g<0>}", body)
        tex = head + sep + body
    # Insert fontspec packages after geometry (or documentclass as fallback).
    if "\\usepackage{geometry}" in tex:
        return tex.replace("\\usepackage{geometry}",
                           "\\usepackage{geometry}\n" + CJK_PREAMBLE, 1)
    return tex.replace("\\begin{document}", CJK_PREAMBLE + "\n\\begin{document}", 1)


def compile_tex(tex_path: Path, pdf_out: Path | None = None) -> Path | None:
    """Compile with pdflatex (xelatex if CJK present); return final PDF or None.

    The .tex file is only the intermediate artifact and stays where it is;
    when pdf_out is given, the compiled PDF is moved to that exact path."""
    tex = tex_path.read_text()
    engine = "xelatex" if CJK_RE.search(tex) else "pdflatex"
    if shutil.which(engine) is None:
        log(f"[4/4] {engine} not found — skipping compile.")
        return None
    log(f"[4/4] Compiling LaTeX ({engine}) ...")
    for _ in range(2):  # two passes for stable layout
        proc = subprocess.run(
            [engine, "-interaction=nonstopmode", "-halt-on-error",
             tex_path.name],
            cwd=tex_path.parent, capture_output=True, text=True)
        if proc.returncode != 0:
            log("      compile FAILED — see .log next to the .tex")
            tail = "\n".join(proc.stdout.splitlines()[-15:])
            log(tail)
            return None
    pdf_path = tex_path.with_suffix(".pdf")
    if pdf_out is not None:
        pdf_out = pdf_out.expanduser()
        if not pdf_out.is_absolute():
            pdf_out = Path.cwd() / pdf_out
        pdf_out.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(pdf_path), str(pdf_out))
        pdf_path = pdf_out
    log(f"      OK -> {pdf_path}")
    return pdf_path


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(
        description="Handwritten math PDF -> LaTeX via local KoboldCPP vision model")
    ap.add_argument("pdf", type=Path, help="input PDF (handwritten math)")
    ap.add_argument("-o", "--out", type=Path, default=None,
                    help="output directory (default: <pdf stem>_latex/)")
    ap.add_argument("--port", type=int, default=5001,
                    help="KoboldCPP port (default 5001)")
    ap.add_argument("--dpi", type=int, default=200,
                    help="render resolution (default 200)")
    ap.add_argument("--pages", type=str, default=None,
                    help="page range, e.g. 2-5 (default all)")
    ap.add_argument("--max-tokens", type=int, default=1600,
                    help="max tokens per page (default 1600)")
    ap.add_argument("--temperature", type=float, default=0.2,
                    help="sampling temperature (default 0.2)")
    ap.add_argument("--retries", type=int, default=3,
                    help="attempts per page (default 3)")
    ap.add_argument("--compile", action="store_true",
                    help="compile the intermediate .tex with pdflatex")
    ap.add_argument("--pdf", type=Path, default=None, dest="pdf_out",
                    metavar="PATH",
                    help="exact path for the final compiled PDF "
                         "(implies --compile; default: <outdir>/<pdf stem>.pdf)")
    ap.add_argument("--keep-pages", action="store_true",
                    help="keep rendered page PNGs")
    ap.add_argument("--dry-run", action="store_true",
                    help="show where inputs/outputs would go and exit "
                         "(no conversion, nothing written)")
    args = ap.parse_args()

    pdf_path = args.pdf.resolve()
    if not pdf_path.is_file():
        log(f"error: {pdf_path} not found")
        return 1
    pdf_out = (args.pdf_out.expanduser() if args.pdf_out else None)
    if pdf_out is not None and not pdf_out.is_absolute():
        pdf_out = Path.cwd() / pdf_out
    outdir = (args.out or pdf_path.parent / f"{pdf_path.stem}_latex").resolve()
    tex_path = outdir / f"{pdf_path.stem}.tex"
    if args.pdf_out:
        args.compile = True  # an explicit PDF path implies compiling
    default_pdf = tex_path.with_suffix(".pdf")

    if args.dry_run:
        log("Dry run — nothing will be written or converted.")
        log(f"  Input PDF:          {pdf_path}")
        log(f"  Output directory:   {outdir}")
        log(f"  Intermediate .tex:  {tex_path}")
        if args.compile:
            log(f"  Final PDF:          {pdf_out or default_pdf}")
        else:
            log("  Final PDF:          (skipped — pass --compile or --pdf)")
        log(f"  Model endpoint:     http://localhost:{args.port}")
        if pdf_out is not None:
            if pdf_out.exists():
                log("  note: --pdf target exists and will be overwritten")
            else:
                log("  note: --pdf parent directory will be created if missing")
        return 0

    outdir.mkdir(parents=True, exist_ok=True)
    base_url = f"http://localhost:{args.port}"

    pages = render_pdf_pages(pdf_path, outdir, args.dpi, args.pages)

    page_texes: list[str] = []
    for i, png in enumerate(pages, start=1):
        page_texes.append(convert_page(base_url, png, i, len(pages),
                                       args.max_tokens, args.temperature,
                                       args.retries))

    final_tex = ensure_packages(add_cjk_support(stitch(page_texes)))
    tex_path.write_text(final_tex)
    log(f"[3/4] Wrote {tex_path} (intermediate)")

    if not args.keep_pages:
        for png in pages:
            png.unlink(missing_ok=True)

    if args.compile:
        final_pdf = compile_tex(tex_path, pdf_out)
        if final_pdf:
            log(f"\nDone. Final PDF: {final_pdf}")
            log(f"      Intermediate .tex kept at: {tex_path}")
            return 0
        log(f"\nCompile failed — intermediate .tex kept at: {tex_path}")
        return 1

    log(f"\nDone. Intermediate .tex: {tex_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
