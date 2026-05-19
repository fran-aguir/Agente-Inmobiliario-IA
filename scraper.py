#!/usr/bin/env python3
"""
Agente Buscador de Departamentos - CDMX
========================================
Usa Playwright para manejar sitios con JavaScript.
Fuentes: Vivanuncios, Lamudi, icasas.mx
Guarda resultados nuevos en Google Sheets (sin duplicados).
"""

import os
import re
import time
import hashlib
import json
import logging
from datetime import datetime

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
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

PRICE_MIN   = 5_000
PRICE_MAX   = 8_000
SHEET_ID    = "1hXmBLPT9oip5MM-JYFUo4yl4IpaCtukCfB9SeqpHNwo"
OWNER_EMAIL = "carlosfco.aguilar18@gmail.com"

ALCALDIAS = {
    "gustavo-a-madero": "Gustavo A. Madero",
    "miguel-hidalgo":   "Miguel Hidalgo",
    "azcapotzalco":     "Azcapotzalco",
}

SHEET_HEADERS = [
    "Fecha", "Fuente", "Alcaldía", "Título",
    "Precio", "Recámaras", "Ubicación", "URL", "ID",
]

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


def listing_id(url: str, titulo: str, precio: str) -> str:
    key = url if url and url != "N/D" else f"{titulo}_{precio}"
    return hashlib.md5(key.encode()).hexdigest()[:10]


def price_in_range(text: str) -> bool:
    """Retorna True si el precio está en rango o no se pudo determinar."""
    num = extract_price(text)
    if num is None:
        return True  # Sin precio → incluir y que el usuario decida
    return PRICE_MIN <= num <= PRICE_MAX


def fmt_price(text: str) -> str:
    num = extract_price(text)
    return f"${num:,}" if num else (text or "N/D")

# ─── Playwright helpers ───────────────────────────────────────────────────────

def new_page(playwright):
    """Crea un browser + página con headers realistas."""
    browser = playwright.chromium.launch(
        headless=True,
        args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
    )
    ctx = browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        locale="es-MX",
        viewport={"width": 1280, "height": 800},
        extra_http_headers={
            "Accept-Language": "es-MX,es;q=0.9",
            "DNT": "1",
        },
    )
    return browser, ctx, ctx.new_page()


def safe_text(page, selector: str) -> str:
    try:
        el = page.query_selector(selector)
        return el.inner_text().strip() if el else ""
    except Exception:
        return ""


def safe_attr(el, attr: str) -> str:
    try:
        return el.get_attribute(attr) or ""
    except Exception:
        return ""

# ─── Scraper: Vivanuncios ─────────────────────────────────────────────────────

def scrape_vivanuncios(pw) -> list:
    results = []

    url_map = {
        "gustavo-a-madero": "https://www.vivanuncios.com.mx/s-departamentos-en-renta/gustavo-a-madero/v1c1300l10271p1",
        "miguel-hidalgo":   "https://www.vivanuncios.com.mx/s-departamentos-en-renta/miguel-hidalgo/v1c1300l10276p1",
        "azcapotzalco":     "https://www.vivanuncios.com.mx/s-departamentos-en-renta/azcapotzalco/v1c1300l10266p1",
    }

    browser, ctx, page = new_page(pw)
    try:
        for alcaldia_key, url in url_map.items():
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                # Esperar a que carguen los listings
                try:
                    page.wait_for_selector(
                        "article[data-aut-id='itemBox'], li.item, [class*='listing']",
                        timeout=15_000
                    )
                except PlaywrightTimeout:
                    pass
                time.sleep(2)

                cards = page.query_selector_all("article[data-aut-id='itemBox']")
                if not cards:
                    cards = page.query_selector_all("li[class*='item']")
                if not cards:
                    cards = page.query_selector_all("[class*='listingCard'], [class*='ListingCard']")

                log.info(f"  Vivanuncios / {alcaldia_key}: {len(cards)} tarjetas")

                for card in cards:
                    try:
                        title_el    = card.query_selector("[data-aut-id='itemTitle'], h2, [class*='title']")
                        price_el    = card.query_selector("[data-aut-id='itemPrice'], [class*='price']")
                        location_el = card.query_selector("[data-aut-id='item-location'], [class*='location'], [class*='address']")
                        link_el     = card.query_selector("a")

                        titulo    = title_el.inner_text().strip()    if title_el    else "Sin título"
                        precio    = price_el.inner_text().strip()    if price_el    else ""
                        ubicacion = location_el.inner_text().strip() if location_el else ALCALDIAS[alcaldia_key]
                        href      = safe_attr(link_el, "href")
                        full_url  = href if href.startswith("http") else f"https://www.vivanuncios.com.mx{href}"

                        if not price_in_range(precio):
                            continue

                        results.append({
                            "fecha":     datetime.now().strftime("%Y-%m-%d %H:%M"),
                            "fuente":    "Vivanuncios",
                            "alcaldia":  ALCALDIAS[alcaldia_key],
                            "titulo":    titulo[:120],
                            "precio":    fmt_price(precio),
                            "recamaras": "N/D",
                            "ubicacion": ubicacion[:100],
                            "url":       full_url,
                        })
                    except Exception as e:
                        log.debug(f"  Error tarjeta Vivanuncios: {e}")

            except Exception as e:
                log.warning(f"  Vivanuncios / {alcaldia_key} falló: {e}")
    finally:
        browser.close()

    return results

# ─── Scraper: Lamudi ──────────────────────────────────────────────────────────

def scrape_lamudi(pw) -> list:
    results = []

    url_map = {
        "gustavo-a-madero": f"https://www.lamudi.com.mx/distrito-federal/gustavo-a-madero/departamento/for-rent/price:{PRICE_MIN}-{PRICE_MAX}/",
        "miguel-hidalgo":   f"https://www.lamudi.com.mx/distrito-federal/miguel-hidalgo/departamento/for-rent/price:{PRICE_MIN}-{PRICE_MAX}/",
        "azcapotzalco":     f"https://www.lamudi.com.mx/distrito-federal/azcapotzalco/departamento/for-rent/price:{PRICE_MIN}-{PRICE_MAX}/",
    }

    browser, ctx, page = new_page(pw)
    try:
        for alcaldia_key, url in url_map.items():
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                try:
                    page.wait_for_selector(
                        "[class*='ListingCell'], [class*='listing-card'], [class*='js-listing']",
                        timeout=15_000
                    )
                except PlaywrightTimeout:
                    pass
                time.sleep(2)

                # Selectores de Lamudi (varios fallbacks)
                cards = page.query_selector_all("[class*='ListingCell-content']")
                if not cards:
                    cards = page.query_selector_all("[class*='listing-card']")
                if not cards:
                    cards = page.query_selector_all("article")

                log.info(f"  Lamudi / {alcaldia_key}: {len(cards)} tarjetas")

                for card in cards:
                    try:
                        title_el    = card.query_selector("[class*='title'], h2, h3")
                        price_el    = card.query_selector("[class*='price'], [class*='Price']")
                        location_el = card.query_selector("[class*='location'], [class*='address'], [class*='Location']")
                        link_el     = card.query_selector("a")

                        titulo    = title_el.inner_text().strip()    if title_el    else "Sin título"
                        precio    = price_el.inner_text().strip()    if price_el    else ""
                        ubicacion = location_el.inner_text().strip() if location_el else ALCALDIAS[alcaldia_key]
                        href      = safe_attr(link_el, "href")
                        full_url  = href if href.startswith("http") else f"https://www.lamudi.com.mx{href}"

                        if not price_in_range(precio):
                            continue

                        results.append({
                            "fecha":     datetime.now().strftime("%Y-%m-%d %H:%M"),
                            "fuente":    "Lamudi",
                            "alcaldia":  ALCALDIAS[alcaldia_key],
                            "titulo":    titulo[:120],
                            "precio":    fmt_price(precio),
                            "recamaras": "N/D",
                            "ubicacion": ubicacion[:100],
                            "url":       full_url,
                        })
                    except Exception as e:
                        log.debug(f"  Error tarjeta Lamudi: {e}")

            except Exception as e:
                log.warning(f"  Lamudi / {alcaldia_key} falló: {e}")
    finally:
        browser.close()

    return results

# ─── Scraper: icasas.mx ───────────────────────────────────────────────────────

def scrape_icasas(pw) -> list:
    """
    icasas.mx agrega anuncios de múltiples fuentes.
    Suele ser más estable que los sitios individuales.
    """
    results = []

    url_map = {
        "gustavo-a-madero": f"https://www.icasas.mx/renta/departamento/distrito-federal/gustavo-a-madero/?preciomax={PRICE_MAX}&preciomin={PRICE_MIN}",
        "miguel-hidalgo":   f"https://www.icasas.mx/renta/departamento/distrito-federal/miguel-hidalgo/?preciomax={PRICE_MAX}&preciomin={PRICE_MIN}",
        "azcapotzalco":     f"https://www.icasas.mx/renta/departamento/distrito-federal/azcapotzalco/?preciomax={PRICE_MAX}&preciomin={PRICE_MIN}",
    }

    browser, ctx, page = new_page(pw)
    try:
        for alcaldia_key, url in url_map.items():
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                try:
                    page.wait_for_selector(
                        "[class*='property'], [class*='listing'], article, .card",
                        timeout=15_000
                    )
                except PlaywrightTimeout:
                    pass
                time.sleep(2)

                cards = page.query_selector_all("[class*='PropertyCard'], [class*='property-item']")
                if not cards:
                    cards = page.query_selector_all("article")
                if not cards:
                    cards = page.query_selector_all("[class*='listing']")

                log.info(f"  iCasas / {alcaldia_key}: {len(cards)} tarjetas")

                for card in cards:
                    try:
                        title_el    = card.query_selector("h2, h3, [class*='title']")
                        price_el    = card.query_selector("[class*='price'], [class*='Price']")
                        location_el = card.query_selector("[class*='location'], [class*='address']")
                        link_el     = card.query_selector("a")

                        titulo    = title_el.inner_text().strip()    if title_el    else "Sin título"
                        precio    = price_el.inner_text().strip()    if price_el    else ""
                        ubicacion = location_el.inner_text().strip() if location_el else ALCALDIAS[alcaldia_key]
                        href      = safe_attr(link_el, "href")
                        full_url  = href if href.startswith("http") else f"https://www.icasas.mx{href}"

                        if not price_in_range(precio):
                            continue

                        results.append({
                            "fecha":     datetime.now().strftime("%Y-%m-%d %H:%M"),
                            "fuente":    "iCasas",
                            "alcaldia":  ALCALDIAS[alcaldia_key],
                            "titulo":    titulo[:120],
                            "precio":    fmt_price(precio),
                            "recamaras": "N/D",
                            "ubicacion": ubicacion[:100],
                            "url":       full_url,
                        })
                    except Exception as e:
                        log.debug(f"  Error tarjeta iCasas: {e}")

            except Exception as e:
                log.warning(f"  iCasas / {alcaldia_key} falló: {e}")
    finally:
        browser.close()

    return results

# ─── Google Sheets ────────────────────────────────────────────────────────────

def get_sheet() -> gspread.Worksheet:
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS_JSON"])
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    client = gspread.authorize(creds)

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
        log.info("  Encabezados creados.")

    return sheet


def save_listings(sheet: gspread.Worksheet, listings: list) -> int:
    existing_data = sheet.get_all_values()
    existing_ids = {row[8] for row in existing_data[1:] if len(row) > 8}

    new_rows = []
    for lst in listings:
        lid = listing_id(lst["url"], lst["titulo"], lst["precio"])
        if lid not in existing_ids:
            new_rows.append([
                lst["fecha"], lst["fuente"], lst["alcaldia"],
                lst["titulo"], lst["precio"], lst["recamaras"],
                lst["ubicacion"], lst["url"], lid,
            ])
            existing_ids.add(lid)

    if new_rows:
        sheet.append_rows(new_rows, value_input_option="USER_ENTERED")
        log.info(f"  ✅ {len(new_rows)} nuevos anuncios guardados.")
    else:
        log.info("  Sin anuncios nuevos.")

    return len(new_rows)

# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    log.info("=" * 60)
    log.info("Agente Buscador de Departamentos — CDMX")
    log.info(f"Alcaldías: {', '.join(ALCALDIAS.values())}")
    log.info(f"Precio: ${PRICE_MIN:,} – ${PRICE_MAX:,} MXN")
    log.info("=" * 60)

    all_listings = []

    with sync_playwright() as pw:
        log.info("Consultando Vivanuncios...")
        all_listings.extend(scrape_vivanuncios(pw))

        log.info("Consultando Lamudi...")
        all_listings.extend(scrape_lamudi(pw))

        log.info("Consultando iCasas...")
        all_listings.extend(scrape_icasas(pw))

    log.info(f"Total encontrado: {len(all_listings)} anuncios dentro del rango de precio")

    log.info("Conectando a Google Sheets...")
    sheet = get_sheet()
    added = save_listings(sheet, all_listings)

    log.info("=" * 60)
    log.info(f"Listo. Nuevos anuncios guardados: {added}")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
