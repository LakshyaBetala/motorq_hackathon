"""Render the solution document to a print-quality PDF.

Markdown -> styled HTML -> Chromium print. Playwright is already a dev dependency for the UI
smoke test, so this adds no new tooling, and Chromium's print engine handles page breaks,
widow control and table splitting better than any pure-Python PDF library would.

Usage:  python tools/build_pdf.py docs/SOLUTION-DOCUMENT.md submission/Solution-Document.pdf
"""

from __future__ import annotations

import pathlib
import re
import sys

import markdown
from playwright.sync_api import sync_playwright

CSS = """
@page {
  size: A4;
  margin: 18mm 16mm 20mm 16mm;
  @bottom-center { content: counter(page); }
}

:root {
  --ink:      #14181d;
  --ink-soft: #4a5560;
  --rule:     #d7dde3;
  --accent:   #1b4d7a;
  --code-bg:  #f4f6f8;
}

* { box-sizing: border-box; }

body {
  font-family: "Charter", "Georgia", "Times New Roman", serif;
  font-size: 10.2pt;
  line-height: 1.52;
  color: var(--ink);
  margin: 0;
  -webkit-font-smoothing: antialiased;
}

/* Title block ------------------------------------------------------------ */
h1 {
  font-family: "Segoe UI", "Helvetica Neue", Arial, sans-serif;
  font-size: 21pt;
  line-height: 1.2;
  letter-spacing: -0.015em;
  margin: 0 0 6mm;
  padding-bottom: 4mm;
  border-bottom: 2px solid var(--accent);
  color: var(--accent);
}

/* The metadata table that follows the title: no header row, label column fixed. */
h1 + table { margin-bottom: 8mm; font-size: 9pt; }
h1 + table thead { display: none; }
h1 + table tbody tr { background: none !important; }
h1 + table td { border-bottom: 0.5px solid var(--rule); padding: 1.9mm 2.4mm; }
h1 + table td:first-child { width: 42mm; color: var(--accent); white-space: nowrap; }

/* Headings --------------------------------------------------------------- */
h2, h3, h4 {
  font-family: "Segoe UI", "Helvetica Neue", Arial, sans-serif;
  color: var(--accent);
  page-break-after: avoid;
  break-after: avoid;
}
h2 {
  font-size: 13.5pt;
  margin: 9mm 0 3mm;
  padding-bottom: 1.6mm;
  border-bottom: 1px solid var(--rule);
  letter-spacing: -0.01em;
}
/* Each numbered section starts a fresh page -- a 30-page document that reads as
   discrete sections is far easier to mark than one continuous scroll. */
h2.section-start { page-break-before: always; break-before: page; }
h3 { font-size: 11pt;   margin: 6mm 0 2mm; }
h4 { font-size: 10.2pt; margin: 5mm 0 2mm; color: var(--ink); }

p { margin: 0 0 3mm; orphans: 3; widows: 3; }

/* Tables ----------------------------------------------------------------- */
table {
  width: 100%;
  border-collapse: collapse;
  margin: 3.5mm 0 5mm;
  font-family: "Segoe UI", "Helvetica Neue", Arial, sans-serif;
  font-size: 8.6pt;
  page-break-inside: auto;
}
thead { display: table-header-group; }   /* repeat the header when a table splits */
tr { page-break-inside: avoid; break-inside: avoid; }
th {
  text-align: left;
  background: #eef2f6;
  color: var(--accent);
  font-weight: 600;
  padding: 2mm 2.4mm;
  border-bottom: 1.2px solid var(--accent);
}
td {
  padding: 1.8mm 2.4mm;
  border-bottom: 0.5px solid var(--rule);
  vertical-align: top;
}
tbody tr:nth-child(even) { background: #fafbfc; }

/* Code ------------------------------------------------------------------- */
code {
  font-family: "Cascadia Mono", "Consolas", "SF Mono", monospace;
  font-size: 8.4pt;
  background: var(--code-bg);
  padding: 0.3mm 1mm;
  border-radius: 2px;
}
pre {
  background: var(--code-bg);
  border-left: 2.5px solid var(--accent);
  padding: 3mm 3.5mm;
  margin: 3mm 0 4.5mm;
  font-size: 8.1pt;
  line-height: 1.42;
  overflow-wrap: break-word;
  white-space: pre-wrap;
  page-break-inside: avoid;
  break-inside: avoid;
}
pre code { background: none; padding: 0; font-size: inherit; }

blockquote {
  margin: 3.5mm 0;
  padding: 2.5mm 4mm;
  border-left: 3px solid var(--accent);
  background: #f6f9fb;
  color: var(--ink);
}
blockquote p:last-child { margin-bottom: 0; }

ul, ol { margin: 0 0 3.5mm; padding-left: 6mm; }
li { margin-bottom: 1.2mm; }

hr { border: none; border-top: 1px solid var(--rule); margin: 6mm 0; }

strong { font-weight: 600; }
a { color: var(--accent); text-decoration: none; }
"""

HEADER = """
<div style="font-family:'Segoe UI',Arial,sans-serif;font-size:7pt;color:#8a949e;
            width:100%;padding:0 16mm;display:flex;justify-content:space-between;">
  <span>ReadyLine &mdash; Connected Vehicle Intelligence Hackathon</span>
  <span>Lakshya Betala</span>
</div>
"""

FOOTER = """
<div style="font-family:'Segoe UI',Arial,sans-serif;font-size:7pt;color:#8a949e;
            width:100%;padding:0 16mm;display:flex;justify-content:space-between;">
  <span>Solution Document v1.0</span>
  <span class="pageNumber"></span><span>&nbsp;/&nbsp;</span><span class="totalPages"></span>
</div>
"""


def build(src: pathlib.Path, out: pathlib.Path) -> None:
    text = src.read_text(encoding="utf-8")

    html_body = markdown.markdown(
        text,
        extensions=["tables", "fenced_code", "sane_lists", "attr_list"],
    )

    # Start each numbered top-level section on a new page, but not section 1 (it would
    # leave the title page empty).
    def mark(m: re.Match) -> str:
        n = int(m.group(1))
        cls = "" if n == 1 else ' class="section-start"'
        return f"<h2{cls}>{m.group(1)}. {m.group(2)}"

    html_body = re.sub(r"<h2>(\d+)\.\s*([^<]*)", mark, html_body)

    page = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<style>{CSS}</style></head><body>{html_body}</body></html>"
    )

    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp.html")
    tmp.write_text(page, encoding="utf-8")

    with sync_playwright() as p:
        browser = p.chromium.launch()
        pg = browser.new_page()
        pg.goto(tmp.resolve().as_uri(), wait_until="networkidle")
        pg.pdf(
            path=str(out),
            format="A4",
            print_background=True,
            display_header_footer=True,
            header_template=HEADER,
            footer_template=FOOTER,
            margin={"top": "20mm", "bottom": "18mm", "left": "16mm", "right": "16mm"},
        )
        browser.close()

    tmp.unlink(missing_ok=True)
    print(f"{out}  ({out.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    build(pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]))
