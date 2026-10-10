import html
import json
import os
import re
import shutil
import subprocess
import sys

import markdown
import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# --- CONFIGURACIÓN DE ESTILOS CSS PARA LOS DOCUMENTOS PDF ---
ESTILOS_CSS = """
<style>
    @page {
        margin: 18mm 15mm;
        @bottom-right { content: counter(page); font-size: 10px; color: #6e7781; }
    }
    body {
        font-family: -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif;
        line-height: 1.55;
        color: #24292e;
        font-size: 13px;
    }
    h1 { color: #0969da; border-bottom: 2px solid #eaecef; padding-bottom: 6px; font-size: 22px; margin-top: 20px; }
    h2 { color: #1f2328; border-bottom: 1px solid #eaecef; padding-bottom: 4px; margin-top: 18px; font-size: 16px; page-break-after: avoid; }
    h3 { color: #24292f; margin-top: 14px; font-size: 14px; }

    /* Bloques de código y Modelfiles */
    pre {
        background: #f6f8fa;
        border: 1px solid #d0d7de;
        padding: 12px;
        border-radius: 6px;
        overflow-x: auto;
        font-family: Consolas, monospace;
        font-size: 12px;
        white-space: pre-wrap;
        word-break: break-all;
    }
    code {
        font-family: Consolas, monospace;
        background: #eff1f3;
        padding: 2px 4px;
        border-radius: 4px;
        font-size: 12px;
    }

    /* Tablas de Benchmarks y Datos */
    table { width: 100%; border-collapse: collapse; margin: 16px 0; font-size: 12px; }
    th, td { border: 1px solid #d0d7de; padding: 7px 10px; text-align: left; }
    th { background-color: #f6f8fa; font-weight: 600; }
    tr:nth-child(even) { background-color: #fbfcfd; }

    /* Contenedores de metadatos e índices */
    .meta-box { background: #f6f8fa; border: 1px solid #d0d7de; border-radius: 6px; padding: 12px; margin-bottom: 16px; }
    .page-break { page-break-before: always; }
    ul { padding-left: 20px; }
    li { margin-bottom: 4px; }
</style>
"""


def compilar_pdf(html_body: str, output_filename: str):
    """Compila el documento HTML final a PDF usando Chromium sin dependencias externas."""
    html_doc = f"<!DOCTYPE html><html><head><meta charset='utf-8'>{ESTILOS_CSS}</head><body>{html_body}</body></html>"
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(html_doc, wait_until="networkidle")
        page.pdf(
            path=output_filename,
            format="A4",
            margin={"top": "15mm", "bottom": "15mm", "left": "15mm", "right": "15mm"},
            print_background=True,
        )
        browser.close()
    print(f"  -> [OK] PDF generado: {output_filename}\n")


# ==============================================================================
# MÓDULO 1: FABRIC (PATRONES DE PROMPTING RECURSIVOS)
# ==============================================================================
def ejecutar_pipeline_fabric():
    print("=== [1/3] Procesando Fabric Patterns ===")
    repo_url = "https://github.com/danielmiessler/fabric.git"
    local_dir = os.path.abspath("./fabric_repo")
    output_pdf = "Fabric_Patterns.pdf"

    if not os.path.exists(local_dir):
        print("  Clonando repositorio de Fabric...")
        subprocess.run(
            ["git", "clone", "--depth", "1", repo_url, local_dir], check=True
        )
    else:
        print("  Actualizando Fabric (git pull)...")
        subprocess.run(["git", "-C", local_dir, "pull"], check=False)

    # Detección dinámica de patrones con system.md
    patrones = {}
    for root, _, files in os.walk(local_dir):
        if ".git" in root:
            continue
        if "system.md" in files:
            nombre = os.path.basename(root)
            patrones[nombre] = os.path.join(root, "system.md")

    # Auto-reparación en caso de que el clone previo haya quedado truncado
    if not patrones:
        print("  [!] Repositorio incompleto. Re-clonando limpio...")
        shutil.rmtree(local_dir, ignore_errors=True)
        subprocess.run(
            ["git", "clone", "--depth", "1", repo_url, local_dir], check=True
        )
        for root, _, files in os.walk(local_dir):
            if ".git" not in root and "system.md" in files:
                patrones[os.path.basename(root)] = os.path.join(root, "system.md")

    nombres_ordenados = sorted(patrones.keys())
    print(f"  Patrones detectados: {len(nombres_ordenados)}")

    md_content = (
        "# Catálogo de Patrones de Prompting (Fabric)\n\n## Índice de Patrones\n\n"
    )
    for p in nombres_ordenados:
        md_content += f"- [{p}](#{p})\n"
    md_content += "\n<div class='page-break'></div>\n\n"

    for p in nombres_ordenados:
        with open(patrones[p], "r", encoding="utf-8", errors="ignore") as f:
            contenido = f.read()
            md_content += f"<a id='{p}'></a>\n## Patrón: {p}\n\n{contenido}\n\n<div class='page-break'></div>\n\n"

    html_body = markdown.markdown(md_content, extensions=["tables", "fenced_code"])
    compilar_pdf(html_body, output_pdf)


# ==============================================================================
# MÓDULO 2: PROMPTING GUIDE DAIR.AI (FILTRADO POR IDIOMA Y LIMPIEZA JSX)
# ==============================================================================
def ejecutar_pipeline_dair(idioma: str = "es"):
    print(
        f"=== [2/3] Procesando DAIR.AI Prompting Guide (Idioma: {idioma.upper()}) ==="
    )
    repo_url = "https://github.com/dair-ai/Prompt-Engineering-Guide.git"
    local_dir = os.path.abspath("./prompt_guide_repo")
    output_pdf = f"PromptingGuide_DAIR_{idioma.upper()}.pdf"

    if not os.path.exists(local_dir):
        print("  Clonando Prompt-Engineering-Guide...")
        subprocess.run(
            ["git", "clone", "--depth", "1", repo_url, local_dir], check=True
        )
    else:
        print("  Actualizando guía (git pull)...")
        subprocess.run(["git", "-C", local_dir, "pull"], check=False)

    pages_dir = os.path.join(local_dir, "pages")
    archivos = []
    regex_idioma = re.compile(r"\.([a-z]{2})\.(?:md|mdx)$", re.IGNORECASE)

    for root, _, files in os.walk(pages_dir):
        for file in files:
            if file.startswith("_") or not file.endswith((".md", ".mdx")):
                continue

            match = regex_idioma.search(file)
            if match:
                if match.group(1).lower() == idioma.lower():
                    archivos.append(os.path.join(root, file))
            elif idioma.lower() == "en":
                # Si no tiene sufijo de idioma, es la versión en inglés por defecto
                archivos.append(os.path.join(root, file))

    archivos.sort()
    print(f"  Capítulos encontrados: {len(archivos)}")

    md_content = f"# Guía de Ingeniería de Instrucciones (DAIR.AI) - [{idioma.upper()}]\n\n## Índice\n\n"
    for ruta in archivos:
        base = os.path.basename(ruta)
        titulo = (
            re.sub(r"\.([a-z]{2})?\.(md|mdx)$", "", base, flags=re.IGNORECASE)
            .replace("-", " ")
            .capitalize()
        )
        ancla = re.sub(r"[^a-zA-Z0-9]", "", titulo).lower()
        md_content += f"- [{titulo}](#{ancla})\n"
    md_content += "\n<div class='page-break'></div>\n\n"

    for ruta in archivos:
        base = os.path.basename(ruta)
        titulo = (
            re.sub(r"\.([a-z]{2})?\.(md|mdx)$", "", base, flags=re.IGNORECASE)
            .replace("-", " ")
            .capitalize()
        )
        ancla = re.sub(r"[^a-zA-Z0-9]", "", titulo).lower()

        with open(ruta, "r", encoding="utf-8", errors="ignore") as f:
            texto = f.read()
            # Limpieza A: Quitar frontmatter YAML
            texto = re.sub(r"^---[\s\S]*?---\n", "", texto)
            # Limpieza B: Quitar imports de React
            texto = re.sub(
                r"^import\s+.*?from\s+[\'\"].*?[\'\"];?\n?",
                "",
                texto,
                flags=re.MULTILINE,
            )
            # Limpieza C: Quitar tags JSX preservando su contenido interno
            texto = re.sub(
                r"</?(?:Callout|Cards|Card|Screenshot|Tabs|Tab|Steps|FileTree)[^>]*>",
                "",
                texto,
            )

            md_content += f"<a id='{ancla}'></a>\n## {titulo}\n\n{texto}\n\n<div class='page-break'></div>\n\n"

    html_body = markdown.markdown(md_content, extensions=["tables", "fenced_code"])
    compilar_pdf(html_body, output_pdf)


# ==============================================================================
# MÓDULO 3: OLLAMA MODEL CARDS (METADATOS, REGISTRY API Y PROMPTING FORMATS)
# ==============================================================================
def obtener_modelfile_registry(modelo: str, tag: str = "latest"):
    """Descarga únicamente capas de metadatos de texto sin descargar pesos GGUF."""
    manifest_url = f"https://registry.ollama.ai/v2/library/{modelo}/manifests/{tag}"
    headers = {
        "Accept": "application/vnd.docker.distribution.manifest.v2+json, application/json"
    }
    resultado = {"template": None, "system": None, "params": None}

    capas_texto = ["template", "system", "params"]

    try:
        r = requests.get(manifest_url, headers=headers, timeout=10)
        if r.status_code == 200:
            manifest = r.json()
            for layer in manifest.get("layers", []):
                media_type = layer.get("mediaType", "").lower()
                digest = layer.get("digest", "")

                # Ignorar de inmediato pesos binarios
                if not digest or "model" in media_type:
                    continue

                # Procesar únicamente metadatos válidos
                if any(k in media_type for k in capas_texto):
                    blob_url = (
                        f"https://registry.ollama.ai/v2/library/{modelo}/blobs/{digest}"
                    )
                    blob_resp = requests.get(blob_url, timeout=10)
                    if blob_resp.status_code == 200:
                        texto = blob_resp.text
                        if "template" in media_type:
                            resultado["template"] = texto
                        elif "system" in media_type:
                            resultado["system"] = texto
                        elif "params" in media_type:
                            resultado["params"] = texto
    except Exception as e:
        print(f"    [Advertencia] Error de API Registry en {modelo}: {e}")

    return resultado


def obtener_datos_web_ollama(modelo: str):
    """Extrae el contenido de documentación, tablas de benchmark y tags del modelo."""
    url = f"https://ollama.com/library/{modelo}"
    headers = {"User-Agent": "Mozilla/5.0"}
    readme_html = ""

    try:
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            prose = soup.find("div", class_=lambda c: c and "prose" in c)
            if prose:
                for b in prose.find_all("button"):
                    b.decompose()
                readme_html = str(prose)
    except Exception as e:
        print(f"    [Advertencia] Error al scrapear página web de {modelo}: {e}")

    return readme_html


def ejecutar_pipeline_ollama():
    print("=== [3/3] Procesando Fichas Técnicas de Ollama ===")
    modelos = ["deepseek-r1", "nemotron", "llama3.2", "qwen2.5", "mistral", "phi4"]
    output_pdf = "Ollama_Technical_Model_Cards.pdf"

    html_acumulado = """
    <h1>Fichas Técnicas de Modelos: Ollama</h1>
    <p>Documentación técnica de arquitectura, plantillas de tokens de control (Templates), parámetros de inferencia y benchmarks.</p>
    <div class='page-break'></div>
    """

    for m in modelos:
        print(f"  Extrayendo metadatos y arquitectura de: {m}...")
        mf = obtener_modelfile_registry(m)
        readme = obtener_datos_web_ollama(m)

        html_acumulado += f"<h1>Modelo: {m.upper()}</h1>"
        html_acumulado += f"<div class='meta-box'><strong>Ejecución local rápida:</strong> <code>ollama run {m}</code></div>"

        # 1. Arquitectura de Tokens y Prompt Template
        html_acumulado += "<h2>1. Arquitectura de Prompts y Modelfile</h2>"
        if mf["template"]:
            # html.escape es fundamental para que el navegador no borre <|im_start|> o <think>
            template_seguro = html.escape(mf["template"])
            html_acumulado += f"<h3>Prompt Template (Estructura de Tokens):</h3><pre><code>{template_seguro}</code></pre>"
        else:
            html_acumulado += "<p><em>Template integrado internamente en los pesos GGUF del modelo.</em></p>"

        if mf["system"]:
            system_seguro = html.escape(mf["system"])
            html_acumulado += f"<h3>System Prompt por Defecto:</h3><pre><code>{system_seguro}</code></pre>"

        if mf["params"]:
            try:
                params_json = json.dumps(json.loads(mf["params"]), indent=2)
                html_acumulado += f"<h3>Parámetros de Inferencia:</h3><pre><code>{params_json}</code></pre>"
            except Exception:
                html_acumulado += f"<h3>Parámetros de Inferencia:</h3><pre><code>{html.escape(mf['params'])}</code></pre>"

        # 2. Documentación y Benchmarks formateados
        html_acumulado += "<h2>2. Documentación Oficial y Benchmarks</h2>"
        if readme:
            html_acumulado += readme
        else:
            html_acumulado += (
                "<p>No se pudo obtener el cuerpo de documentación web.</p>"
            )

        html_acumulado += "<div class='page-break'></div>"

    compilar_pdf(html_acumulado, output_pdf)


# ==============================================================================
# ENTRADA PRINCIPAL
# ==============================================================================
if __name__ == "__main__":
    print("\nIniciando compilación del pipeline de documentación técnica...\n")
    ejecutar_pipeline_fabric()
    ejecutar_pipeline_dair(idioma="es")  # Cambiar a "en" para la versión en inglés
    ejecutar_pipeline_ollama()
    print(
        "Pipeline finalizado con éxito. Los PDFs están listos en el directorio actual."
    )
