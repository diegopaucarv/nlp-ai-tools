"""
Extrae un PDF en pares de páginas (imágenes) y usa GLM-5.3-Flash vía Together
con structured output para producir Markdown acumulativo.

Optimizaciones específicas para zai-org/GLM-5.3-Flash:
  - temperature=1.0 y top_p=0.95 (valores recomendados por Z.AI).
  - reasoning_effort="max" para máxima calidad de transcripción.
  - max_tokens=131072 (límite real del modelo, no 8192).
  - Imágenes JPEG a DPI moderado para respetar el límite de 10 MB por imagen.
  - Manejo explícito del campo reasoning_content (thinking siempre activo).
  - Reintentos con backoff exponencial para rate limits dinámicos.
  - System prompt con el esquema JSON incluido (recomendación de Together).
"""

import base64
import io
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import fitz  # PyMuPDF
from PIL import Image
from pydantic import BaseModel, Field
from together import Together


# --------------------------------------------------------------------------- #
# 1) Esquema estructurado (Pydantic)
# --------------------------------------------------------------------------- #
class PagePair(BaseModel):
    markdown: str = Field(
        description="Contenido transcrito en Markdown. Conserva títulos, listas, "
        "tablas GFM, bloques de código y fórmulas LaTeX. No resumas."
    )
    languages: list[str] = Field(
        default_factory=list,
        description="Códigos ISO de los idiomas detectados en las páginas.",
    )
    has_tables: bool = Field(default=False)
    has_figures: bool = Field(default=False)
    pages_covered: list[int] = Field(
        default_factory=list,
        description="Números de página (1-indexados) que cubre esta transcripción.",
    )


SYSTEM_PROMPT = """Eres un motor OCR experto especializado en documentos técnicos y académicos.

Recibirás UNA imagen que contiene DOS páginas consecutivas de un PDF (izquierda→derecha).

REGLAS ESTRICTAS:
1. Marca el inicio de cada página con exactamente este formato: <!-- page N -->
2. Conserva el orden de lectura natural y la estructura jerárquica.
3. Títulos: # hasta ######. Listas: -, 1., a). Tablas: GFM (| col | col |).
4. Bloques de código: ```lenguaje. Fórmulas: $inline$ o $$bloque$$.
5. Figuras: ![figura: descripción breve](fig)
6. Une palabras cortadas por guión al final de línea.
7. NO resumas, NO interpretes, NO inventes. Transcribe literalmente.
8. Si una página está en blanco o es una imagen sin texto, indícalo brevemente.

Esquema de salida (JSON):
{schema}
"""


# --------------------------------------------------------------------------- #
# 2) Render PDF -> imagen de 2 páginas por hoja
# --------------------------------------------------------------------------- #
def render_pairs(
    pdf_path: str,
    out_dir: str,
    dpi: int = 150,  # reducido: 150 DPI es suficiente para OCR y mantiene
    # las imágenes bien por debajo del límite de 10 MB.
    quality: int = 80,  # JPEG más ligero, menos latencia de subida.
    gutter_px: int = 12,  # separación visual clara entre las dos páginas.
    max_dim: int = 4000,  # redimensiona si alguna dimensión excede este valor.
) -> list[Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    doc = fitz.open(pdf_path)
    n = len(doc)
    zoom = dpi / 72.0
    mat = fitz.Matrix(zoom, zoom)
    paths: list[Path] = []

    for i in range(0, n, 2):
        page_end = min(i + 2, n)
        out_path = out / f"pair_{i // 2:04d}_p{i + 1}-{page_end}.jpg"
        if out_path.exists():
            paths.append(out_path)
            continue

        pages = [doc.load_page(i)]
        if i + 1 < n:
            pages.append(doc.load_page(i + 1))

        imgs = []
        for p in pages:
            pix = p.get_pixmap(matrix=mat, alpha=False)
            img = Image.open(io.BytesIO(pix.tobytes("png")))
            # Redimensionar si excede max_dim
            w, h = img.size
            if w > max_dim or h > max_dim:
                ratio = min(max_dim / w, max_dim / h)
                img = img.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)
            imgs.append(img)

        if len(imgs) == 1:
            canvas = imgs[0]
        else:
            w = sum(im.width for im in imgs) + gutter_px
            h = max(im.height for im in imgs)
            canvas = Image.new("RGB", (w, h), "white")
            x = 0
            for im in imgs:
                canvas.paste(im, (x, 0))
                x += im.width + gutter_px

        canvas.save(out_path, "JPEG", quality=quality, optimize=True)
        paths.append(out_path)

    doc.close()
    return paths


def _to_data_url(path: Path) -> str:
    data = base64.b64encode(path.read_bytes()).decode()
    return f"data:image/jpeg;base64,{data}"


# --------------------------------------------------------------------------- #
# 3) Llamada a Together con structured output + retries
# --------------------------------------------------------------------------- #
def extract_pair(
    client: Together,
    img_path: Path,
    page_start: int,
    model: str,
    max_retries: int = 5,
    base_delay: float = 2.0,
) -> tuple[int, PagePair]:
    """Extrae un par de páginas con backoff exponencial y manejo de thinking."""
    data_url = _to_data_url(img_path)
    schema_json = json.dumps(PagePair.model_json_schema())
    system = SYSTEM_PROMPT.format(schema=schema_json)

    last_err: Exception | None = None

    for attempt in range(max_retries):
        try:
            resp = client.chat.completions.create(
                model=model,
                temperature=1.0,  # recomendado por Z.AI
                top_p=0.95,  # recomendado por Z.AI
                max_tokens=131072,  # límite real del modelo
                #                reasoning_effort="min",  # máxima calidad de razonamiento
                messages=[
                    {"role": "system", "content": system},
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": f"Extrae las páginas {page_start}-{page_start + 1}.",
                            },
                            {"type": "image_url", "image_url": {"url": data_url}},
                        ],
                    },
                ],
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "PagePair",
                        "schema": PagePair.model_json_schema(),
                    },
                },
            )

            # El modelo siempre incluye thinking en reasoning_content.
            # El JSON estructurado está en content.
            payload = resp.choices[0].message.content
            if not payload:
                raise ValueError("Respuesta vacía del modelo")

            return page_start, PagePair.model_validate_json(payload)

        except Exception as e:
            last_err = e
            if attempt < max_retries - 1:
                delay = base_delay * (2**attempt)
                print(
                    f"  ↻ reintento {attempt + 1}/{max_retries} "
                    f"para páginas {page_start}-{page_start + 1} "
                    f"en {delay:.1f}s: {e}"
                )
                time.sleep(delay)

    raise last_err  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# 4) Orquestador
# --------------------------------------------------------------------------- #
def main(
    pdf_path: str,
    out_md: str = "output.md",
    work_dir: str = "_pages",
    model: str = "zai-org/GLM-5.3-Flash",
    dpi: int = 150,
    quality: int = 80,
    max_workers: int = 4,  # ajusta según tu plan de rate limits
) -> None:
    api_key = os.environ.get("TOGETHER_API_KEY")
    if not api_key:
        api_key = ""

    client = Together(api_key=api_key)

    images = render_pairs(pdf_path, work_dir, dpi=dpi, quality=quality)
    print(f"[+] {len(images)} pares de páginas listos")

    results: dict[int, PagePair] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {
            ex.submit(extract_pair, client, img, i * 2 + 1, model): img
            for i, img in enumerate(images)
        }
        for fut in as_completed(futures):
            try:
                ps, pair = fut.result()
                results[ps] = pair
                print(f"[✓] páginas {ps}-{ps + 1}")
            except Exception as e:
                print(f"[✗] {futures[fut].name}: {e}")

    # Escritura final ordenada
    with open(out_md, "w", encoding="utf-8") as f:
        f.write(f"# {Path(pdf_path).stem}\n\n")
        for ps in sorted(results):
            f.write(f"\n<!-- === páginas {ps}-{ps + 1} === -->\n\n")
            f.write(results[ps].markdown.strip() + "\n")

    print(f"[+] Markdown -> {out_md}")


if __name__ == "__main__":
    pdf = sys.argv[1] if len(sys.argv) > 1 else "input.pdf"
    main(pdf)
