#!/usr/bin/env python3
"""
Agente Buscador de Departamentos - CDMX
========================================
Busca en Inmuebles24, Vivanuncios y Lamudi.
Filtra por alcaldía y rango de precio.
Guarda resultados nuevos en Google Sheets (sin duplicados).

Uso:
  export GOOGLE_CREDENTIALS_JSON='{ ... }'
  python scraper.py
"""

import os
import re
import time
import random
import hashlib
import json
import logging
from datetime import datetime

import requests
from bs4 import BeautifulSoup
import gspread
from google.oauth2.service_account import Credentials

# ─── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ─── Configuración ────────────────────────────────────────────────────────────

PRICE_MIN = 5_000   # MXN
PRICE_MAX = 8_000   # MXN

ALCALDIAS = {
    "gustavo-a-madero": "Gustavo A. Madero",
    "miguel-hidalgo":   "Miguel Hidalgo",
    "azcapotzalco":     "Azcapotzalco",
}

SHEET_NAME = "Departamentos CDMX"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "es-MX,es;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

SHEET_HEADERS = [
    "Fecha", "Fuente", "Alcaldía", "Título",
    "Precio", "Recámaras", "Ubicación", "URL", "ID",
]

# ─── Helpers ──────────────────────────────────────────────────────────────────

def get_soup(url: str, params: dict = None) -> "BeautifulSoup | None":
    """GET con delay aleatorio para no sobrecargar los servidores."""
    try:
        time.sleep(random.uniform(2.5, 5.0))
        resp = requests.get(url, headers=HEADERS, params=params, timeout=15)
        resp.raise_for_status()
        return BeautifulSoup(resp.text, "lxml")
    except requests.RequestException as e:
        log.warning(f"  Error al acceder {url}: {e}")
        return None


def extract_price(text: str) -> "int | None":
    """Extrae precio numérico de una cadena. Ej: '$6,500/mes' → 6500."""
    if not text:
        return None
    digits = re.sub(r"[^\d]", "", text)
    if digits:
        val = int(digits)
        # Descartar valores fuera de rango razonable (evitar confundir con precios de venta)
        if 1_000 <= val <= 500_000:
            return val
    return None


def listing_id(listing: dict) -> str:
    """ID único basado en URL o título+precio para deduplicación."""
    key = listing.get("url") or f"{listing['titulo']}_{listing['precio']}"
    return hashlib.md5(key.encode()).hexdigest()[:10]


def make_listing(fuente, alcaldia_key, titulo, precio_raw,
                 recamaras, ubicacion, url) -> "dict | None":
    """Crea un dict de listing y valida el rango de precio."""
    precio_num = extract_price(precio_raw) if precio_raw else None

    # Filtrar por precio si se pudo extraer el número
    if precio_num is not None and not (PRICE_MIN <= precio_num <= PRICE_MAX):
        return None

    return {
        "fuente":    fuente,
        "alcaldia":  ALCALDIAS[alcaldia_key],
        "titulo":    (titulo or "Sin título").strip()[:120],
        "precio":    f"${precio_num:,}" if precio_num else (precio_raw or "N/D"),
        "recamaras": str(recamaras) if recamaras else "N/D",
        "ubicacion": (ubicacion or ALCALDIAS[alcaldia_key]).strip()[:100],
        "url":       (url or "N/D").strip(),
        "fecha":     datetime.now().strftime("%Y-%m-%d %H:%M"),
    }

# ─── Scrapers ─────────────────────────────────────────────────────────────────

def scrape_inmuebles24() -> list:
    """
    Scraper para inmuebles24.com
    Usa atributos data-qa para localizar elementos.
    Si retorna 0 resultados, abre la página en el navegador,
    inspecciona el HTML e actualiza los selectores aquí.
    """
    results = []

    for alcaldia_key in ALCALDIAS:
        url = (
            f"https://www.inmuebles24.com/departamentos-en-renta-en-"
            f"{alcaldia_key}-capital-federal.html"
        )
        soup = get_soup(url, params={
            "precio_desde": PRICE_MIN,
            "precio_hasta": PRICE_MAX,
        })
        if not soup:
            continue

        # Inmuebles24 usa data-qa para identificar tarjetas de listing
        cards = soup.find_all("div", attrs={"data-qa": "posting PROPERTY"})

        # Fallback: clases comunes si cambiaron los data-qa
        if not cards:
            cards = soup.find_all(
                "div",
                class_=re.compile(r"postingCard|listing-card|PropertyCard", re.I)
            )

        log.info(f"  Inmuebles24 / {alcaldia_key}: {len(cards)} tarjetas")

        for card in cards:
            try:
                title_el    = card.find(attrs={"data-qa": "POSTING_CARD_TITLE"})
                price_el    = card.find(attrs={"data-qa": "POSTING_CARD_PRICE"})
                location_el = card.find(attrs={"data-qa": "POSTING_CARD_LOCATION"})
                features_el = card.find(attrs={"data-qa": "POSTING_CARD_FEATURES"})
                link_el     = (
                    card.find("a", attrs={"data-qa": "POSTING_CARD_TITLE"})
                    or card.find("a")
                )

                titulo    = title_el.get_text(strip=True)    if title_el    else None
                precio    = price_el.get_text(strip=True)    if price_el    else None
                ubicacion = location_el.get_text(strip=True) if location_el else None

                recamaras = "N/D"
                if features_el:
                    feat = features_el.get_text()
                    m = re.search(r"(\d+)\s*(rec[áa]mara|cuarto|dorm)", feat, re.I)
                    if m:
                        recamaras = m.group(1)

                href = link_el.get("href", "") if link_el else ""
                full_url = (
                    f"https://www.inmuebles24.com{href}"
                    if href.startswith("/") else href
                )

                lst = make_listing(
                    "Inmuebles24", alcaldia_key,
                    titulo, precio, recamaras, ubicacion, full_url
                )
                if lst:
                    results.append(lst)

            except Exception as e:
                log.debug(f"  Error parseando tarjeta de Inmuebles24: {e}")

    return results


def scrape_vivanuncios() -> list:
    """
    Scraper para vivanuncios.com.mx (plataforma OLX).

    ADVERTENCIA: Vivanuncios usa React para renderizar contenido.
    Si el scraper retorna 0 resultados, el contenido se carga vía JS
    y necesitarás usar Playwright (ver README para instrucciones).
    """
    results = []

    slugs = {
        "gustavo-a-madero": "gustavo-a-madero",
        "miguel-hidalgo":   "miguel-hidalgo",
        "azcapotzalco":     "azcapotzalco",
    }

    for alcaldia_key, slug in slugs.items():
        # URL de búsqueda de Vivanuncios — puede necesitar ajustes
        url = (
            f"https://www.vivanuncios.com.mx/s-renta-de-departamentos/"
            f"ciudad-de-mexico/{slug}/v1c1097l3095041p1"
        )
        soup = get_soup(url)
        if not soup:
            continue

        # Vivanuncios usa <article> para las tarjetas de listing
        cards = soup.find_all("article", class_=re.compile(r"item|listing", re.I))
        if not cards:
            cards = soup.find_all("li", class_=re.compile(r"item|listing", re.I))

        log.info(f"  Vivanuncios / {alcaldia_key}: {len(cards)} tarjetas")

        for card in cards:
            try:
                title_el    = card.find("h2") or card.find(class_=re.compile(r"title", re.I))
                price_el    = card.find(class_=re.compile(r"price", re.I))
                location_el = card.find(class_=re.compile(r"location|address", re.I))
                link_el     = card.find("a")

                titulo    = title_el.get_text(strip=True)    if title_el    else None
                precio    = price_el.get_text(strip=True)    if price_el    else None
                ubicacion = location_el.get_text(strip=True) if location_el else None
                href      = link_el.get("href", "")          if link_el     else ""
                full_url  = (
                    href if href.startswith("http")
                    else f"https://www.vivanuncios.com.mx{href}"
                )

                lst = make_listing(
                    "Vivanuncios", alcaldia_key,
                    titulo, precio, None, ubicacion, full_url
                )
                if lst:
                    results.append(lst)

            except Exception as e:
                log.debug(f"  Error parseando tarjeta de Vivanuncios: {e}")

    return results


def scrape_lamudi() -> list:
    """
    Scraper para lamudi.com.mx
    Más estable que los otros, suele funcionar con requests directo.
    """
    results = []

    slugs = {
        "gustavo-a-madero": "gustavo-a-madero",
        "miguel-hidalgo":   "miguel-hidalgo",
        "azcapotzalco":     "azcapotzalco",
    }

    for alcaldia_key, slug in slugs.items():
        url = (
            f"https://www.lamudi.com.mx/ciudad-de-mexico/{slug}/"
            f"departamento/for-rent/"
        )
        soup = get_soup(url, params={
            "pricemax": PRICE_MAX,
            "pricemin": PRICE_MIN,
        })
        if not soup:
            continue

        cards = soup.find_all(
            "div",
            class_=re.compile(r"ListingCell|listing-card|property-item", re.I)
        )
        if not cards:
            cards = soup.find_all(
                "article",
                class_=re.compile(r"listing|property", re.I)
            )

        log.info(f"  Lamudi / {alcaldia_key}: {len(cards)} tarjetas")

        for card in cards:
            try:
                title_el    = card.find(class_=re.compile(r"title", re.I))
                price_el    = card.find(class_=re.compile(r"price|Price", re.I))
                location_el = card.find(class_=re.compile(r"address|location", re.I))
                link_el     = card.find_parent("a") or card.find("a")

                titulo    = title_el.get_text(strip=True)    if title_el    else None
                precio    = price_el.get_text(strip=True)    if price_el    else None
                ubicacion = location_el.get_text(strip=True) if location_el else None
                href      = link_el.get("href", "")          if link_el     else ""
                full_url  = (
                    href if href.startswith("http")
                    else f"https://www.lamudi.com.mx{href}"
                )

                lst = make_listing(
                    "Lamudi", alcaldia_key,
                    titulo, precio, None, ubicacion, full_url
                )
                if lst:
                    results.append(lst)

            except Exception as e:
                log.debug(f"  Error parseando tarjeta de Lamudi: {e}")

    return results

# ─── Google Sheets ─────────────────────────────────────────────────────────────

def get_or_create_sheet() -> gspread.Worksheet:
    """Conecta con Google Sheets. Crea y formatea la hoja si no existe."""
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS_JSON"])
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    client = gspread.authorize(creds)

    try:
        spreadsheet = client.open(SHEET_NAME)
        log.info(f"  Hoja existente encontrada: {SHEET_NAME}")
    except gspread.SpreadsheetNotFound:
        spreadsheet = client.create(SHEET_NAME)
        log.info(f"  Hoja nueva creada: {SHEET_NAME}")

    sheet = spreadsheet.sheet1

    # Si la hoja está vacía, agregar encabezados con formato
    if not sheet.get_all_values():
        sheet.append_row(SHEET_HEADERS)
        sheet.format("A1:I1", {
            "backgroundColor": {"red": 0.18, "green": 0.55, "blue": 0.34},
            "textFormat": {
                "bold": True,
                "foregroundColor": {"red": 1.0, "green": 1.0, "blue": 1.0},
                "fontSize": 11,
            },
        })
        sheet.freeze(rows=1)
        # Ajustar ancho de columnas (URL ancha, ID estrecha)
        body = {
            "requests": [
                {"updateDimensionProperties": {
                    "range": {"sheetId": 0, "dimension": "COLUMNS",
                              "startIndex": 7, "endIndex": 8},
                    "properties": {"pixelSize": 300},
                    "fields": "pixelSize",
                }},
                {"updateDimensionProperties": {
                    "range": {"sheetId": 0, "dimension": "COLUMNS",
                              "startIndex": 8, "endIndex": 9},
                    "properties": {"pixelSize": 90},
                    "fields": "pixelSize",
                }},
            ]
        }
        spreadsheet.batch_update(body)
        log.info("  Encabezados y formato aplicados.")

    # Compartir con el dueño del agente (se ejecuta siempre por si acaso)
    try:
        spreadsheet.share(
            'carlosfco.aguilar18@gmail.com',
            perm_type='user',
            role='writer',
            notify=False,
        )
        log.info("  Hoja compartida con carlosfco.aguilar18@gmail.com")
    except Exception as e:
        log.warning(f"  No se pudo compartir la hoja: {e}")

    return sheet


def save_listings(sheet: gspread.Worksheet, listings: list) -> int:
    """Guarda solo los listings nuevos (deduplicación por ID). Retorna cuántos se agregaron."""
    existing_data = sheet.get_all_values()
    # La columna ID es la #9 (índice 8)
    existing_ids = {row[8] for row in existing_data[1:] if len(row) > 8}

    new_rows = []
    for listing in listings:
        lid = listing_id(listing)
        if lid not in existing_ids:
            new_rows.append([
                listing["fecha"],
                listing["fuente"],
                listing["alcaldia"],
                listing["titulo"],
                listing["precio"],
                listing["recamaras"],
                listing["ubicacion"],
                listing["url"],
                lid,
            ])
            existing_ids.add(lid)  # Evitar duplicados dentro del mismo lote

    if new_rows:
        sheet.append_rows(new_rows, value_input_option="USER_ENTERED")
        log.info(f"  ✅ {len(new_rows)} nuevos anuncios guardados en Sheets.")
    else:
        log.info("  Sin anuncios nuevos en esta ejecución.")

    return len(new_rows)

# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    log.info("=" * 60)
    log.info("Agente Buscador de Departamentos — CDMX")
    log.info(f"Alcaldías: {', '.join(ALCALDIAS.values())}")
    log.info(f"Precio: ${PRICE_MIN:,} – ${PRICE_MAX:,} MXN")
    log.info("=" * 60)

    all_listings = []

    log.info("Consultando Inmuebles24...")
    all_listings.extend(scrape_inmuebles24())

    log.info("Consultando Vivanuncios...")
    all_listings.extend(scrape_vivanuncios())

    log.info("Consultando Lamudi...")
    all_listings.extend(scrape_lamudi())

    log.info(f"Total encontrado: {len(all_listings)} anuncios")

    if not all_listings:
        log.warning(
            "No se encontraron anuncios. "
            "Posibles causas: los sitios bloquearon el scraper o cambiaron sus selectores HTML. "
            "Revisa el archivo scraper.py y actualiza los selectores según lo que veas en el navegador."
        )
        return

    log.info("Conectando a Google Sheets...")
    sheet = get_or_create_sheet()
    added = save_listings(sheet, all_listings)

    log.info("=" * 60)
    log.info(f"Listo. Nuevos anuncios guardados: {added}")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
