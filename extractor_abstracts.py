import argparse
import asyncio
import base64
import csv
import io
import json
import logging
import re
import sqlite3
import sys
import time
from datetime import datetime
from typing import List
from urllib.parse import urljoin

from curl_cffi import requests
from PIL import Image
from playwright.async_api import TimeoutError as PlaywrightTimeout
from playwright.async_api import async_playwright
from pydantic import BaseModel, Field
from pypdf import PdfReader
from together import Together

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
import nest_asyncio

nest_asyncio.apply()

# ==============================================================================
# CONFIGURACIÓN DE LOGS (Reemplaza a los prints)
# ==============================================================================
logging.basicConfig(
    filename="scraper_runtime.log",
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    encoding="utf-8",
)
logger = logging.getLogger(__name__)


# ==============================================================================
# GESTOR DE REPORTE Y BASE DE DATOS
# ==============================================================================
class ExecutionReport:
    def __init__(self):
        self.stats = {
            "total_papers_found": 0,
            "pdfs_downloaded": 0,
            "abstracts_via_api": 0,
            "abstracts_via_ocr": 0,
            "abstracts_missing": 0,
            "llm_interpretations": 0,
        }
        self.paper_details = []

    def add_paper(
        self,
        doi: str,
        title: str,
        pdf_found: bool,
        abstract_source: str,
        llm_interpreted: bool,
    ):
        self.stats["total_papers_found"] += 1
        if pdf_found:
            self.stats["pdfs_downloaded"] += 1

        if abstract_source == "OCR":
            self.stats["abstracts_via_ocr"] += 1
        elif abstract_source == "API/Text":
            self.stats["abstracts_via_api"] += 1
        else:
            self.stats["abstracts_missing"] += 1

        if llm_interpreted:
            self.stats["llm_interpretations"] += 1

        self.paper_details.append(
            {
                "doi": doi,
                "title": title[:75] + ("..." if len(title) > 75 else ""),
                "pdf": "YES" if pdf_found else "NO",
                "abstract": abstract_source,
                "llm": "YES" if llm_interpreted else "NO",
            }
        )

    def generate_log_report(self, filepath="scraping_report.log"):
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(f"=== SCRAPING EXECUTION REPORT ===\n")
            f.write(f"Generated on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")

            f.write("--- AGGREGATE RESULTS ---\n")
            f.write(f"Total Papers Found:      {self.stats['total_papers_found']}\n")
            f.write(f"PDFs Downloaded:         {self.stats['pdfs_downloaded']}\n")
            f.write(f"Abstracts (API/Text):    {self.stats['abstracts_via_api']}\n")
            f.write(f"Abstracts (OCR):         {self.stats['abstracts_via_ocr']}\n")
            f.write(f"Abstracts Missing:       {self.stats['abstracts_missing']}\n")
            f.write(f"LLM Interpretations:     {self.stats['llm_interpretations']}\n\n")

            f.write("--- PER PAPER SUMMARY ---\n")
            for p in self.paper_details:
                f.write(
                    f"DOI: {p['doi'].ljust(30)} | PDF: {p['pdf'].ljust(3)} | Abstract: {p['abstract'].ljust(10)} | LLM: {p['llm'].ljust(3)} | Title: {p['title']}\n"
                )


class DatabaseManager:
    def __init__(self, db_path="academic_papers.db"):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS papers (
                    doi TEXT PRIMARY KEY,
                    issn TEXT,
                    journal_name TEXT,
                    issue TEXT,
                    pub_date TEXT,
                    title TEXT,
                    authors TEXT,
                    abstract TEXT,
                    pdf_interpretation TEXT
                )
            """)
            conn.commit()

    def upsert_paper(self, paper_data: dict):
        """Inserta el paper o actualiza los datos si el DOI ya existe (evita duplicados)."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO papers (doi, issn, journal_name, issue, pub_date, title, authors, abstract, pdf_interpretation)
                VALUES (:doi, :issn, :journal_name, :issue_str, :date, :title, :authors, :abstract, :pdf_interpretation)
                ON CONFLICT(doi) DO UPDATE SET
                    issn=excluded.issn,
                    journal_name=excluded.journal_name,
                    issue=excluded.issue,
                    pub_date=excluded.pub_date,
                    title=excluded.title,
                    authors=excluded.authors,
                    abstract=excluded.abstract,
                    pdf_interpretation=excluded.pdf_interpretation
            """,
                paper_data,
            )
            conn.commit()


# ==============================================================================
# MODELOS DE DATOS (OCR)
# ==============================================================================
def clean_crossref_abstract(raw_abstract: str) -> str:
    if not raw_abstract:
        return ""
    clean_text = re.sub(r"<[^>]+>", "", raw_abstract)
    return clean_text.strip()


class ArticuloOCR(BaseModel):
    contenido: str = Field(
        description="Texto íntegro o transcripción completa del abstract/resumen"
    )


class ResultadoOCR(BaseModel):
    articulos: List[ArticuloOCR]


# ==============================================================================
# CLIENTES DE API Y SCRAPING
# ==============================================================================
class TogetherOCRClient:
    def __init__(self, api_key: str):
        self.client = Together(api_key=api_key, timeout=100)
        self.model_name = "Prism-ML/Ternary-Bonsai-27B"

    def procesar_captura_bytes(self, image_bytes: bytes, prompt: str) -> str:
        base64_img = base64.b64encode(image_bytes).decode("utf-8")
        try:
            response = self.client.chat.completions.create(
                model=self.model_name,
                messages=[
                    {
                        "role": "system",
                        "content": "Analiza la imagen y extrae la información requerida en formato JSON estricto siguiendo el esquema provisto.",
                    },
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/jpeg;base64,{base64_img}"
                                },
                            },
                        ],
                    },
                ],
                response_format={
                    "type": "json_object",
                    "schema": ResultadoOCR.model_json_schema(),
                },
            )
            resultado = json.loads(response.choices[0].message.content)
            articulos = resultado.get("articulos", [])
            if articulos:
                return articulos[0].get("contenido", "")
            return ""
        except Exception as e:
            logger.error(f"Error procesando OCR con Together AI: {e}")
            return ""


class ArticleInterpreter:
    def __init__(self, api_key: str):
        self.client = Together(api_key=api_key)
        self.model_name = "meta-llama/Llama-3-8b-chat-hf"

    def interpret_pdf(self, title: str, full_text: str) -> str:
        if not full_text:
            return ""
        trimmed_text = full_text[:25000]
        prompt = f"""You are an expert academic research assistant.
        Paper Title: {title}

        Full Text (truncated):
        {trimmed_text}

        Please perform the following analysis based on the text provided:
        1. Classify the paper as either 'Theoretical' or 'Empirical'.
        2. If 'Theoretical': Identify and explain the main idea, its relevance, and the key supporting arguments or ideas.
        3. If 'Empirical': Identify and describe the most impactful findings or the traits that mostly stand out from the paper that are NOT typically mentioned in a brief abstract.

        Output the analysis in clear Markdown format. Provide direct, insightful answers without filler text."""

        try:
            logger.info(f"LLM analizando texto del PDF: {title}")
            response = self.client.chat.completions.create(
                model=self.model_name,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=1024,
                temperature=0.2,
            )
            return response.choices[0].message.content.strip()
        except Exception as e:
            logger.error(f"Error en la Interpretación LLM: {e}")
            return "Error procesando la interpretación del PDF."


class ChromiumDownloader:
    def __init__(self, timeout: int = 60):
        self.timeout_ms = timeout * 1000

    async def download_and_extract_text(self, pdf_links: list) -> str:
        async with async_playwright() as p:
            try:
                browser = await p.chromium.connect_over_cdp("http://localhost:9222")
                context = browser.contexts[0]
            except Exception as e:
                logger.error("No se encontró el navegador remoto en puerto 9222.")
                return None

            for url in pdf_links:
                page = await context.new_page()
                pdf_bytes = None
                try:
                    await page.goto(
                        url, wait_until="domcontentloaded", timeout=self.timeout_ms
                    )
                    detected_pdf_url = None
                    await page.wait_for_timeout(3000)

                    if "tandfonline.com" in url:
                        detected_pdf_url = url.split("?")[0] + "?download=true"
                    else:
                        for iframe in await page.locator("iframe").all():
                            src = await iframe.get_attribute("src")
                            if src and ("pdf" in src.lower() or "epdf" in src.lower()):
                                detected_pdf_url = src
                                break

                    if detected_pdf_url:
                        detected_pdf_url = urljoin(url, detected_pdf_url)
                        try:
                            async with page.expect_download(
                                timeout=self.timeout_ms
                            ) as download_info:
                                try:
                                    await page.goto(
                                        detected_pdf_url,
                                        wait_until="commit",
                                        timeout=self.timeout_ms,
                                    )
                                except Exception as nav_error:
                                    error_str = str(nav_error)
                                    if (
                                        "ERR_ABORTED" not in error_str
                                        and "Download is starting" not in error_str
                                    ):
                                        raise nav_error

                            download = await download_info.value
                            temp_path = await download.path()

                            if temp_path:
                                with open(temp_path, "rb") as f:
                                    header = f.read(4)
                                    if header == b"%PDF":
                                        f.seek(0)
                                        pdf_bytes = f.read()
                                    else:
                                        logger.warning(
                                            f"Archivo no es PDF. Header: {header}"
                                        )
                        except PlaywrightTimeout:
                            logger.warning(
                                f"Timeout al descargar desde {detected_pdf_url}"
                            )
                except Exception as e:
                    logger.error(f"Error general en descarga: {e}")
                finally:
                    await page.close()

                if pdf_bytes:
                    return self._extract_from_bytes(pdf_bytes)

            await browser.close()
        return None

    def _extract_from_bytes(self, pdf_bytes: bytes) -> str:
        try:
            pdf_file = io.BytesIO(pdf_bytes)
            reader = PdfReader(pdf_file)
            return "\n".join(
                [page.extract_text() for page in reader.pages if page.extract_text()]
            )
        except Exception as e:
            logger.error(f"Error al leer el formato del PDF: {e}")
            return None

    async def screenshot_preview(self, doi: str) -> bytes:
        async with async_playwright() as p:
            try:
                browser = await p.chromium.connect_over_cdp("http://localhost:9222")
                context = (
                    await browser.new_context()
                    if not browser.contexts
                    else browser.contexts[0]
                )
            except Exception as e:
                logger.error(f"Error conectando CDP para screenshot: {e}")
                return None

            page = await context.new_page()
            screenshot_bytes = None
            try:
                stealth_script = "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
                await page.add_init_script(stealth_script)

                doi_url = f"https://doi.org/{doi}"
                await page.goto(
                    doi_url, wait_until="domcontentloaded", timeout=self.timeout_ms
                )
                await page.wait_for_timeout(10000)

                try:
                    await page.evaluate("""
                        document.querySelectorAll('div[id*="cookie"], div[class*="cookie"], div[id*="banner"], header, nav').forEach(e => e.style.display = 'none');
                    """)
                except:
                    pass

                screenshot_bytes = await page.screenshot(
                    full_page=True, type="jpeg", quality=80
                )
            except PlaywrightTimeout:
                logger.warning(f"Timeout visual al intentar capturar {doi}")
            except Exception as e:
                logger.error(f"Error capturando {doi}: {e}")
            finally:
                await page.close()
                await browser.close()

            return screenshot_bytes


class SemanticScholarClient:
    def __init__(self):
        self.base_url = "https://api.semanticscholar.org/graph/v1/paper/DOI:"

    def get_abstract(self, doi: str) -> str:
        url = f"{self.base_url}{doi}?fields=abstract"
        try:
            res = requests.get(
                url, headers={"User-Agent": "MyAcademicBot/1.0"}, timeout=10
            )
            if res.status_code == 200:
                abstract = res.json().get("abstract")
                if abstract:
                    return abstract.strip()
        except requests.RequestException as e:
            logger.error(f"Error conectando a Semantic Scholar: {e}")
        return ""


class OpenAlexScraper:
    def __init__(self, email: str):
        self.email = email
        self.base_url = "https://api.openalex.org/works"

    def fetch_recent_articles(self, issn: str, limit: int = 200) -> list:
        """Obtiene hasta los últimos 200 artículos y extrae su Año, Volumen y Número."""
        params = {
            "filter": f"primary_location.source.issn:{issn}",
            "sort": "publication_date:desc",
            "per-page": limit,
            "mailto": self.email,
        }

        try:
            response = requests.get(self.base_url, params=params, timeout=15)
            if response.status_code != 200:
                logger.error(f"Error de OpenAlex: {response.status_code}")
                return []

            items = response.json().get("results", [])
            articles = []

            for item in items:
                raw_doi = item.get("doi")
                doi = raw_doi.replace("https://doi.org/", "") if raw_doi else None
                title = item.get("title", "Sin título")
                authors = ", ".join(
                    [a["author"]["display_name"] for a in item.get("authorships", [])]
                )
                journal_name = (
                    item.get("primary_location", {})
                    .get("source", {})
                    .get("display_name", "Desconocido")
                )

                # Extracción precisa de Volumen y Número (Issue)
                biblio = item.get("biblio") or {}
                volume = biblio.get("volume", "")
                issue = biblio.get("issue", "")
                pub_year = item.get("publication_year", "Desconocido")
                pub_date = item.get("publication_date", "Desconocida")
                abstract = self._reconstruct_abstract(
                    item.get("abstract_inverted_index")
                )

                if doi:
                    articles.append(
                        {
                            "issn": issn,
                            "doi": doi,
                            "title": title,
                            "authors": authors,
                            "journal_name": journal_name,
                            "year": pub_year,
                            "volume": volume,
                            "issue": issue,
                            "date": pub_date,
                            "abstract": abstract,
                        }
                    )
            return articles
        except requests.exceptions.RequestException as e:
            logger.error(f"Error conectando a OpenAlex: {e}")
            return []

    def _reconstruct_abstract(self, inverted_index: dict) -> str:
        if not inverted_index:
            return ""
        try:
            max_pos = max(
                [pos for positions in inverted_index.values() for pos in positions]
            )
            words = [""] * (max_pos + 1)
            for word, positions in inverted_index.items():
                for pos in positions:
                    words[pos] = word
            return " ".join(words).strip()
        except Exception:
            return ""


class UnpaywallClient:
    def __init__(self, email: str):
        self.email = email
        self.base_url = "https://api.unpaywall.org/v2/"

    def get_article_data(self, doi: str) -> dict:
        url = f"{self.base_url}{doi}?email={self.email}"
        try:
            res = requests.get(url, timeout=10)
            if res.status_code == 200:
                data = res.json()
                pdf_links = [
                    loc["url_for_pdf"]
                    for loc in data.get("oa_locations", [])
                    if loc.get("url_for_pdf")
                ]
                return {"is_oa": data.get("is_oa", False), "pdf_links": pdf_links}
        except requests.RequestException as e:
            logger.error(f"Error conectando a Unpaywall: {e}")
        return None


# ==============================================================================
# ORQUESTADOR PRINCIPAL
# ==============================================================================
async def process_journal_pipeline(
    issn: str, email: str, api_key: str, latest: bool, specific_year: str
):
    logger.info("Iniciando pipeline de scraping...")

    scraper = OpenAlexScraper(email)
    unpaywall = UnpaywallClient(email)
    s2_client = SemanticScholarClient()
    downloader = ChromiumDownloader(timeout=30)
    llm = ArticleInterpreter(api_key=api_key)

    db = DatabaseManager("academic_papers.db")
    report = ExecutionReport()

    print(f"\n[*] Consultando todos los artículos de la revista (ISSN: {issn})...")
    all_recent_articles = scraper.fetch_recent_articles(issn, limit=10000)

    if not all_recent_articles:
        print("[-] No se encontraron artículos recientes para este ISSN.")
        return

    # Agrupar los artículos por Año, Volumen y Número
    from collections import OrderedDict

    issues_dict = OrderedDict()

    for art in all_recent_articles:
        # Si el usuario pasó --year, filtramos en este paso
        if specific_year and str(art["year"]) != specific_year:
            continue

        label_parts = [str(art["year"])]
        if art["volume"]:
            label_parts.append(f"Vol {art['volume']}")
        if art["issue"]:
            label_parts.append(f"Issue {art['issue']}")

        # Etiqueta dinámica: Ej: "2023 - Vol 12 - Issue 4"
        label = " - ".join(label_parts)

        if label not in issues_dict:
            issues_dict[label] = []
        issues_dict[label].append(art)

    if not issues_dict:
        print(f"[-] No se encontraron artículos para el año {specific_year}.")
        return

    # LÓGICA DE SELECCIÓN INTERACTIVA
    if latest:
        selected_label = list(issues_dict.keys())[0]
        articles = issues_dict[selected_label]
        print(
            f"[*] Modo: --latest activado. Procesando la edición más reciente: {selected_label}"
        )
    else:
        print("\nEdiciones recientes disponibles:")
        labels = list(issues_dict.keys())
        for i, label in enumerate(
            labels[:15]
        ):  # Muestra las últimas 15 ediciones publicadas
            count = len(issues_dict[label])
            print(f"  [{i + 1}] {label} ({count} artículos)")

        choice = input(
            f"\nSelecciona el número de edición (1-{min(15, len(labels))}) o pulsa Enter para la más reciente: "
        ).strip()

        if choice.isdigit() and 1 <= int(choice) <= len(labels):
            selected_label = labels[int(choice) - 1]
        else:
            selected_label = labels[0]

        articles = issues_dict[selected_label]
        print(f"[*] Seleccionado: {selected_label}")

    csv_data = []
    total_articles = len(articles)
    if total_articles > 0:
        print(
            f"\n[*] Procesando {total_articles} artículos. Los detalles van a 'scraper_runtime.log'."
        )

    for idx, art in enumerate(articles, start=1):
        # ---------------------------------------------------------
        # CONTADOR DE PORCENTAJE (Actualización en la misma línea)
        # ---------------------------------------------------------
        porcentaje = (idx / total_articles) * 100
        # El \r regresa el cursor al inicio de la línea. Los espacios al final limpian caracteres sobrantes de la línea anterior.
        sys.stdout.write(
            f"\r[>] Progreso: {porcentaje:.1f}% ({idx}/{total_articles}) | Extrayendo DOI: {art['doi']}"
            + " " * 15
        )
        sys.stdout.flush()

        logger.info(f"Procesando DOI: {art['doi']}")

        oa_data = unpaywall.get_article_data(art["doi"])
        is_oa = oa_data["is_oa"] if oa_data else False
        pdf_links = oa_data["pdf_links"] if oa_data else []

        abstract = art["abstract"]
        abstract_src = "API/Text" if abstract else ""
        pdf_text = None
        pdf_interpretation = ""

        if is_oa and pdf_links:
            pdf_text = await downloader.download_and_extract_text(pdf_links)

        if not pdf_text:
            if not abstract:
                abstract = s2_client.get_abstract(art["doi"])
                if abstract:
                    abstract_src = "API/Text"

            if not abstract:
                screenshot_bytes = await downloader.screenshot_preview(art["doi"])
                if screenshot_bytes:
                    together_ocr = TogetherOCRClient(api_key=api_key)
                    abstract = together_ocr.procesar_captura_bytes(
                        screenshot_bytes,
                        "Extrae todo el texto visible. Identifica título, autor y el texto completo del abstract.",
                    )
                    if abstract:
                        abstract_src = "OCR"

            if not abstract:
                abstract_src = "Missing"

        if pdf_text:
            pdf_interpretation = llm.interpret_pdf(art["title"], pdf_text)

        # Actualizar Reporte
        report.add_paper(
            doi=art["doi"],
            title=art["title"],
            pdf_found=bool(pdf_text),
            abstract_source=abstract_src,
            llm_interpreted=bool(pdf_interpretation),
        )

        paper_dict = {
            "doi": art["doi"],
            "issn": art["issn"],
            "journal_name": art["journal_name"],
            "issue_str": selected_label,
            "date": art["date"],
            "title": art["title"],
            "authors": art["authors"],
            "abstract": abstract if abstract else "No disponible",
            "pdf_interpretation": pdf_interpretation,
        }

        # Guardar en DB centralizada y CSV en memoria
        db.upsert_paper(paper_dict)
        csv_data.append(paper_dict)

        time.sleep(2)

    # Salto de línea final para no sobreescribir el contador al terminar
    print("\n\n[✔] ¡Descarga y análisis completados exitosamente!")

    # Generar Reportes y CSV
    if csv_data:
        csv_filename = (
            f"Journal_{issn}_{selected_label if selected_label else 'latest'}.csv"
        )
        try:
            with open(csv_filename, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=list(csv_data[0].keys()))
                writer.writeheader()
                writer.writerows(csv_data)
        except Exception as e:
            logger.error(f"Error guardando CSV: {e}")

    report.generate_log_report("scraping_report.log")
    logger.info("Pipeline completado satisfactoriamente.")


# ==============================================================================
# CLI EJECUCIÓN (Automatizada)
# ==============================================================================
if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Scraper académico de revistas (Silencioso)"
    )
    parser.add_argument(
        "--issn", help="ISSN de la revista", nargs="?", default="0002-9602"
    )
    parser.add_argument("--latest", action="store_true", help="Recupera última edición")
    parser.add_argument("--year", help="Recuperar de un año específico (ej. 2023)")
    parser.add_argument(
        "--email", default="diegoblaster3@gmail.com", help="Email para APIs"
    )
    parser.add_argument(
        "--api-key",
        default="tgp_v1__mSZyaseeGD0mWw4-k8WL_QbHV3a562AgtZ_0-dArWQ",
        help="API Key",
    )

    args = parser.parse_args()
    asyncio.run(
        process_journal_pipeline(
            args.issn, args.email, args.api_key, args.latest, args.year
        )
    )
