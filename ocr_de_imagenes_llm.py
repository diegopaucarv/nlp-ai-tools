import base64
import json
from pathlib import Path
from typing import List

import pandas as pd
from pydantic import BaseModel, Field
from together import Together

client = Together(
    api_key="', timeout=300
)


class Articulo(BaseModel):
    titulo: str = Field(description="Título del texto o columna de opinión")
    autor: str = Field(description="Nombre del autor o columnista")
    contenido: str = Field(description="Texto íntegro o transcripción del contenido")


class ListaArticulos(BaseModel):
    articulos: List[Articulo]


def encode_image(image_path: Path) -> str:
    """Convierte una imagen local a cadena base64."""
    with open(image_path, "rb") as img_file:
        return base64.b64encode(img_file.read()).decode("utf-8")


def procesar_imagenes(carpeta_imagenes: str, prompt: str) -> pd.DataFrame:
    ruta = Path(carpeta_imagenes)
    archivos_jpg = list(ruta.glob("*.jpg")) + list(ruta.glob("*.jpeg"))

    # Si no hay imágenes, retornamos un DataFrame vacío de inmediato
    if not archivos_jpg:
        print("No se encontraron imágenes .jpg en la ruta especificada.")
        return pd.DataFrame()

    # CORRECCIÓN 1: Inicializar la lista FUERA del bucle
    filas = []

    for img_path in archivos_jpg:
        print(f"\n--- Procesando: {img_path.name} ---")
        base64_img = encode_image(img_path)

        try:
            response = client.chat.completions.create(
                model="Prism-ML/Ternary-Bonsai-27B",  # Asegúrate de que este modelo soporta Vision + JSON mode
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
                    "schema": ListaArticulos.model_json_schema(),
                },
            )

            resultado = json.loads(response.choices[0].message.content)
            articulos = resultado.get("articulos", [])

            for art in articulos:
                art["archivo"] = img_path.name
                filas.append(art)

        except Exception as e:
            print(f"[-] Error procesando la imagen {img_path.name}: {e}")

    # CORRECCIÓN 2: Convertir la lista de diccionarios en un DataFrame antes de retornar
    return pd.DataFrame(filas)


if __name__ == "__main__":
    CARPETA = "./"
    INSTRUCCION = (
        "Extrae todo el texto visible de las columnas de opinión de esta imagen. "
        "Separa cada columna identificando su título, su autor y transcribe el texto completo."
    )

    df_resultado = procesar_imagenes(CARPETA, INSTRUCCION)
    if not df_resultado.empty:
        # Visualización previa
        print("\n--- Vista previa del DataFrame ---")
        print(df_resultado.head())

        # Exportación
        df_resultado.to_csv("articulos_extraidos.csv", index=False, encoding="utf-8")
        print("\nArchivo guardado exitosamente como 'articulos_extraidos.csv'")
    else:
        print("\nNo se extrajeron datos para guardar.")
