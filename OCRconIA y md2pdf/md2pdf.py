"""
md2pdf_ironpress.py — Convierte el Markdown del OCR en un PDF limpio.

Dependencia única:
    pip install ironpress

Uso:
    python md2pdf_ironpress.py output.md [documento.pdf] [--title "Título"]
"""

import re
import sys
from pathlib import Path

import ironpress

# --------------------------------------------------------------------------- #
# Preprocesado: elimina TODOS los marcadores de página del OCR
# --------------------------------------------------------------------------- #
# Formatos cubiertos:
#   <!-- === páginas 51–52 === -->
#   <!-- page 51 -->
#   [Páginas 51–52]   [Página 51]
#   -51-   · 51 ·   ( 51 )   línea suelta con solo el número
PAGE_MARKER_PATTERNS = [
    # Comentarios HTML del extractor
    re.compile(r"<!--\s*===\s*p[áa]ginas\s+\d+[–\-]\d+\s*===\s*-->", re.IGNORECASE),
    re.compile(r"<!--\s*page\s+\d+\s*-->", re.IGNORECASE),
    # Etiquetas entre corchetes que pudieran haberse colado
    re.compile(r"\[\s*P[áa]ginas?\s+\d+([–\-]\d+)?\s*\]", re.IGNORECASE),
    # Números de página sueltos en su propia línea: -51-, · 51 ·, ( 51 )
    re.compile(r"^[\s\-·•()\[\]]*\d{1,4}[\s\-·•()\[\]]*$", re.MULTILINE),
]


def _strip_markers(md_text: str) -> str:
    for pat in PAGE_MARKER_PATTERNS:
        md_text = pat.sub("", md_text)
    # Colapsa líneas en blanco consecutivas que dejen los borrados
    md_text = re.sub(r"\n{3,}", "\n\n", md_text)
    return md_text.strip() + "\n"


# --------------------------------------------------------------------------- #
# Conversión
# --------------------------------------------------------------------------- #
def md_to_pdf(
    md_path: str | Path,
    pdf_path: str | Path | None = None,
    title: str | None = None,
) -> Path:
    md_path = Path(md_path)
    if pdf_path is None:
        pdf_path = md_path.with_suffix(".pdf")
    pdf_path = Path(pdf_path)

    raw = md_path.read_text(encoding="utf-8")
    prepared = _strip_markers(raw)

    conv = ironpress.HtmlConverter()
    conv.page_size("A4")
    conv.margin_sides(72, 72, 72, 72)

    if title:
        conv.header(title)
    conv.footer("Página {page} de {pages}")

    pdf_bytes = conv.convert_markdown(prepared)
    pdf_path.write_bytes(pdf_bytes)
    return pdf_path


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        sys.exit(
            'Uso: python md2pdf_ironpress.py <entrada.md> [salida.pdf] [--title "Título"]'
        )

    src = args[0]
    dst = None
    title = None

    if len(args) > 1 and not args[1].startswith("--"):
        dst = args[1]
        rest = args[2:]
    else:
        rest = args[1:]

    if "--title" in rest:
        i = rest.index("--title")
        if i + 1 < len(rest):
            title = rest[i + 1]

    out = md_to_pdf(src, dst, title)
    print(f"[+] PDF -> {out}  ({out.stat().st_size / 1024:.0f} KB)")
