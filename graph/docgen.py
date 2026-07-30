#!/usr/bin/env python3
"""Shared page furniture for the generated documents: CSS, and HTML -> PDF.

Both documents print through headless Chrome rather than a PDF library. The
tables carry real data with real column counts, and a browser is the only
renderer here that reflows a 43-column table without being told how.
"""
from __future__ import annotations

import base64
import html
import pathlib
import shutil
import subprocess

CSS = """
body{font:13.5px/1.6 -apple-system,Segoe UI,Roboto,sans-serif;color:#1b2631;
 max-width:1120px;margin:0 auto;padding:40px 28px}
h1{font-size:29px;border-bottom:3px solid #1f5fbf;padding-bottom:10px;margin-bottom:2px}
h2{font-size:20px;margin-top:36px;border-bottom:1px solid #d5d8dc;padding-bottom:5px;
 color:#1f5fbf;page-break-after:avoid}
h3{font-size:15.5px;margin-top:22px;color:#34495e;page-break-after:avoid}
h4{font-size:13.5px;margin:16px 0 6px;color:#5d6d7e;page-break-after:avoid}
table{border-collapse:collapse;width:100%;margin:12px 0;font-size:12px}
th{background:#1f5fbf;color:#fff;text-align:left;padding:6px 8px;font-weight:600;
 vertical-align:top}
td{border-bottom:1px solid #e5e8ec;padding:5px 8px;vertical-align:top}
tr:nth-child(even) td{background:#fafbfc}
code{background:#f4f6f8;padding:1px 5px;border-radius:3px;font-size:11.5px;
 font-family:SFMono-Regular,Consolas,monospace}
pre{background:#f4f6f8;border-left:3px solid #1f5fbf;padding:11px 13px;
 overflow-x:auto;font-size:11.5px;line-height:1.5;page-break-inside:avoid}
.note{background:#fff8e6;border-left:3px solid #e0a800;padding:9px 13px;margin:12px 0}
.warn{background:#fdeceb;border-left:3px solid #c0392b;padding:9px 13px;margin:12px 0}
.ok{background:#eafaf1;border-left:3px solid #27ae60;padding:9px 13px;margin:12px 0}
.sub{color:#5d6d7e;font-size:13px;margin-top:0}
.meta{color:#7f8c8d;font-size:11.5px;margin:2px 0 8px}
img{max-width:100%;height:auto;border:1px solid #d5d8dc;border-radius:4px}
/* Transposed sample tables: the column name is the row label. */
.tp td:first-child{font-family:SFMono-Regular,Consolas,monospace;font-size:11px;
 white-space:nowrap;background:#f4f6f8;font-weight:600;width:1%}
.tp td{font-size:11px;max-width:230px;word-break:break-word}
.src{page-break-inside:avoid}
.toc a{color:#1f5fbf;text-decoration:none}
.toc li{margin:2px 0}
@media print{body{max-width:none;padding:10px}
 table{page-break-inside:auto}tr{page-break-inside:avoid}
 thead{display:table-header-group}}
"""

E = html.escape


def page(title: str, body: str) -> str:
    return (f"<!doctype html><meta charset=utf-8><title>{E(title)}</title>"
            f"<style>{CSS}</style>{body}")


def table(headers: list[str], rows: list[list[str]], cls: str = "") -> str:
    """A table whose cells are already-escaped-or-plain strings."""
    c = f' class="{cls}"' if cls else ""
    h = "".join(f"<th>{x}</th>" for x in headers)
    b = "".join("<tr>" + "".join(f"<td>{c_}</td>" for c_ in r) + "</tr>"
                for r in rows)
    head = f"<thead><tr>{h}</tr></thead>" if headers else ""
    return f"<table{c}>{head}<tbody>{b}</tbody></table>"


def embed_png(path: pathlib.Path) -> str:
    """Inline a PNG as a data URI.

    Chrome's headless PDF export runs with file access restricted; a relative
    <img src> renders as a broken-image box in the PDF while looking correct
    in the browser, which is the worst of both.
    """
    if not path.exists():
        return f"<div class=warn>missing image: {E(str(path))}</div>"
    b64 = base64.b64encode(path.read_bytes()).decode()
    return f'<img src="data:image/png;base64,{b64}" alt="schema">'


CHROME = ("chrome", "google-chrome", "chromium", "msedge",
          r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
          r"C:\Program Files\Google\Chrome\Application\chrome.exe",
          r"C:\Program Files\Microsoft\Edge\Application\msedge.exe")


def render(html_text: str, out_html: pathlib.Path,
           out_pdf: pathlib.Path) -> None:
    out_html.write_text(html_text, encoding="utf-8")
    print(f"wrote {out_html}  ({out_html.stat().st_size / 1024:.0f} KB)")

    for exe in CHROME:
        path = shutil.which(exe) or (exe if pathlib.Path(exe).exists() else None)
        if not path:
            continue
        try:
            subprocess.run([path, "--headless", "--disable-gpu",
                            f"--print-to-pdf={out_pdf}",
                            "--no-pdf-header-footer",
                            out_html.as_uri()],
                           check=True, timeout=600, capture_output=True)
            if out_pdf.exists():
                print(f"wrote {out_pdf}  "
                      f"({out_pdf.stat().st_size / 1024:.0f} KB)")
                return
        except Exception as e:                               # noqa: BLE001
            print(f"  {exe}: {type(e).__name__}")
    print("no browser found - open the HTML and print to PDF")
