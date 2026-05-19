#!/usr/bin/env python3
"""
Agente Buscador de Departamentos - CDMX
========================================
Busca en Inmuebles24, Vivanuncios y Lamudi.
Filtra por alcaldía y rango de precio.
Guarda resultados nuevos en Google Sheets (sin duplicados).
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

PRICE_MIN = 5_000
PRICE_MAX = 8_000

ALCALDIAS = {
    "gustavo-a-madero": "Gustavo A. Madero",
    "miguel-hidalgo":   "Miguel Hidalgo",
    "azcapotzalco":     "Azcapotzalco",
}

SHEET_NAME  = "Departamentos CDMX"
SHEET_ID    = "1hXmBLPT9oip5MM-JYFUo4yl4IpaCtukCfB9SeqpHNwo"
OWNER_EMAIL = "carlosfco.aguilar18@gmail.com"

SHEET_HEADERS = [
    "Fecha", "Fuente", "Alcaldía", "Título",
    "Precio", "Recámaras", "Ubicación", "URL", "ID",
]

# ─── Sesión HTTP con headers realistas ────────────────────────────────────────

def make_session() -> requests.Session:
    """Crea una sesión con headers que imitan un navegador real."""
    s = requests.Session()
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "es-MX,es;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept-Encoding": "gzip, deflate, br",
        "DNT": "1",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Cache-Control": "max-age=0",
    })
    return s

SESSION = make_session()

def get_soup(url: str, params: dict = None, referer: str = None) -> "BeautifulSoup | None":
    try:
        time.sleep(random.uniform(3.0, 6.0))
        headers = {}
        if referer:
            headers["Referer"] = referer
        resp = SESSION.get(url, params=params, timeout=20, headers=headers)
        resp.raise_for_status()
        return BeautifulSoup(resp.text, "lxml")
    except requests.RequestException as e:
        log.warning(f"  Error al acceder {url}: {e}")
        return None

# ─── Helpers ──────────────────────────────────────────────────────────────────

def extract_price(text: str) -> "int | None":
    if not text:
        return None
    digits = re.sub(r"[^\d]", "", text)
    if digits:
        val = int(digits)
        if 1_000 <= val <= 500_000:
            return val
    return None


def listing_id(listing: dict) -> str:
    key = listing.get("url") or f"{listing['titulo']}_{listing['precio']}"
    return hashlib.md5(key.encode()).hexdigest()[:10]


def make_listing(fuente, alcaldia_key, titulo, precio_raw,
                 recamaras, ubicacion, url) -> "dict | None":
    precio_num = extract_price(precio_raw) if precio_raw else None
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

# ─── Scraper: Inmuebles24 ─────────────────────────────────────────────────────

def scrape_inmuebles24() -> list:
    """
    URLs correctas: /departamentos-en-renta-en-{alcaldia}.html
    Sin 'capital-federal' en la URL.
    """
    results = []

    # URLs verificadas — formato correcto sin 'capital-federal'
    urls = {
        "gustavo-a-madero": "https://www.inmuebles24.com/departamentos-en-renta-en-gustavo-a-madero.html",
        "miguel-hidalgo":   "https://www.inmuebles24.com/departamentos-en-renta-en-miguel-hidalgo.html",
        "azcapotzalco":     "https://www.inmuebles24.com/departamentos-en-renta-en-azcapotzalco.html",
    }

    for alcaldia_key, url in urls.items():
        # Primero visitar la home para tener cookie de sesión (evita 403)
        get_soup("https://www.inmuebles24.com/", referer=None)
        time.sleep(random.uniform(1.5, 3.0))

        soup = get_soup(url, referer="https://www.inmuebles24.com/")
        if not soup:
            continue

        cards = soup.find_all("div", attrs={"data-qa": "posting PROPERTY"})
        if not cards:
            cards = soup.find_all("div", class_=re.compile(r"postingCard|PostingCard", re.I))

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
                log.debug(f"  Error parseando tarjeta: {e}")

    return results

# ─── Scraper: Vivanuncios ─────────────────────────────────────────────────────

def scrape_vivanuncios() -> list:
    """
    URLs correctas con código de localidad verificado:
    - Azcapotzalco: l10266
    - Miguel Hidalgo: l10276
    - Gustavo A. Madero: l10271
    Formato: /s-departamentos-en-renta/{alcaldia}/v1c1300l{codigo}p1
    """
    results = []

    url_map = {
        "gustavo-a-madero": "https://www.vivanuncios.com.mx/s-departamentos-en-renta/gustavo-a-madero/v1c1300l10271p1",
        "miguel-hidalgo":   "https://www.vivanuncios.com.mx/s-departamentos-en-renta/miguel-hidalgo/v1c1300l10276p1",
        "azcapotzalco":     "https://www.vivanuncios.com.mx/s-departamentos-en-renta/azcapotzalco/v1c1300l10266p1",
    }

    for alcaldia_key, url in url_map.items():
        soup = get_soup(url, referer="https://www.vivanuncios.com.mx/")
        if not soup:
            continue

        # Vivanuncios usa <article> con data-aut-id
        cards = soup.find_all("article", attrs={"data-aut-id": "itemBox"})
        if not cards:
            cards = soup.find_all("article")
        if not cards:
            cards = soup.find_all("li", class_=re.compile(r"item|listing|ad", re.I))

        log.info(f"  Vivanuncios / {alcaldia_key}: {len(cards)} tarjetas")

        for card in cards:
            try:
                title_el = (
                    card.find(attrs={"data-aut-id": "itemTitle"})
                    or card.find("h2")
                    or card.find(class_=re.compile(r"title", re.I))
                )
                price_el = (
                    card.find(attrs={"data-aut-id": "itemPrice"})
                    or card.find(class_=re.compile(r"price", re.I))
                )
                location_el = (
                    card.find(attrs={"data-aut-id": "item-location"})
                    or card.find(class_=re.compile(r"location|address", re.I))
                )
                link_el = card.find("a")

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
                log.debug(f"  Error parseando tarjeta Vivanuncios: {e}")

    return results

# ─── Scraper: Lamudi ──────────────────────────────────────────────────────────

def scrape_lamudi() -> list:
    """
    URLs correctas con 'distrito-federal' (no 'ciudad-de-mexico').
    Con filtro de precio directo en la URL.
    """
    results = []

    url_map = {
        "gustavo-a-madero": (
            f"https://www.lamudi.com.mx/distrito-federal/gustavo-a-madero/"
            f"departamento/for-rent/price:{PRICE_MIN}-{PRICE_MAX}/"
        ),
        "miguel-hidalgo": (
            f"https://www.lamudi.com.mx/distrito-federal/miguel-hidalgo/"
            f"departamento/for-rent/price:{PRICE_MIN}-{PRICE_MAX}/"
        ),
        "azcapotzalco": (
            f"https://www.lamudi.com.mx/distrito-federal/azcapotzalco/"
            f"departamento/for-rent/price:{PRICE_MIN}-{PRICE_MAX}/"
        ),
    }

    for alcaldia_key, url in url_map.items():
        soup = get_soup(url, referer="https://www.lamudi.com.mx/")
        if not soup:
            continue

        cards = soup.find_all(
            "div",
            class_=re.compile(r"ListingCell|listing-card|property-item|js-listing-item", re.I)
        )
        if not cards:
            cards = soup.find_all("article", class_=re.compile(r"listing|property", re.I))

        log.info(f"  Lamudi / {alcaldia_key}: {len(cards)} tarjetas")

        for card in cards:
            try:
                title_el    = card.find(class_=re.compile(r"title", re.I))
                price_el    = card.find(class_=re.compile(r"price|Price", re.I))
                location_el = card.find(class_=re.compile(r"address|location|Location", re.I))
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
                log.debug(f"  Error parseando tarjeta Lamudi: {e}")

    return results

# ─── Google Sheets ─────────────────────────────────────────────────────────────

def get_or_create_sheet() -> gspread.Worksheet:
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS_JSON"])
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    client = gspread.authorize(creds)

    # Abrir por ID directo — la hoja ya existe en el Drive del usuario
    spreadsheet = client.open_by_key(SHEET_ID)
    log.info(f"  Hoja encontrada: {spreadsheet.title}")

    sheet = spreadsheet.sheet1

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
            OWNER_EMAIL,
            perm_type='user',
            role='writer',
            notify=False,
        )
        log.info(f"  Hoja compartida con {OWNER_EMAIL}")
    except Exception as e:
        log.warning(f"  No se pudo compartir la hoja: {e}")

    return sheet


def save_listings(sheet: gspread.Worksheet, listings: list) -> int:
    existing_data = sheet.get_all_values()
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
            existing_ids.add(lid)

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

    log.info(f"Total encontrado antes de filtrar: {len(all_listings)} anuncios")

    if not all_listings:
        log.warning(
            "No se encontraron anuncios. "
            "Conectando de todas formas con Sheets para verificar credenciales..."
        )

    log.info("Conectando a Google Sheets...")
    sheet = get_or_create_sheet()

    if all_listings:
        added = save_listings(sheet, all_listings)
    else:
        added = 0
        log.info("  Hoja accesible. Sin anuncios nuevos para guardar.")

    log.info("=" * 60)
    log.info(f"Listo. Nuevos anuncios guardados: {added}")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
