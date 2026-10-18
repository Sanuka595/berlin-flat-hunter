#!/usr/bin/env python3
"""Berlin Social Housing Flat Monitor for Telegram (Primary Landeseigene Sources & Aggregator)."""

import argparse
import asyncio
import html
import json
import logging
import os
import random
import re
import signal
import sqlite3
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import httpx
from dotenv import load_dotenv

import monitor_dispatch
from profile_config import (
    SENIOR_STOP_WORDS,
    cover_letter,
    dedup_keys,
    extract_available_from,
    extract_wbs_label,
    is_senior_housing,
    is_student_housing,
    load_profiles,
    matches_profile,
    required_fetchers,
)

# Load configuration from .env
load_dotenv()

CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "90"))
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "flats.db")
WEBHOOK_RETRY_DELAYS = [5, 15, 30]

# Active background tasks reference to avoid GC
background_tasks: set[asyncio.Task] = set()

USER_AGENTS = [
    "Mozilla/5.0 (X11; Linux x86_64; rv:132.0) Gecko/20100101 Firefox/132.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


def get_random_headers() -> Dict[str, str]:
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7",
    }


# ---------------------------------------------------------------------------
# Database Layer (SQLite)
# ---------------------------------------------------------------------------

def init_db(db_path: str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS profile_seen (
            profile_id TEXT NOT NULL,
            dedup_key TEXT NOT NULL,
            source TEXT,
            object_id TEXT,
            url TEXT,
            price REAL,
            title TEXT,
            notified_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (profile_id, dedup_key)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_profile_seen_url ON profile_seen(url)")
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Parsing Helpers
# ---------------------------------------------------------------------------

def parse_german_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).replace("€", "").replace("m²", "").strip()
    s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def _parse_api_number(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).replace("€", "").replace("m²", "").strip()
    if "," in text:
        return parse_german_float(text)
    try:
        return float(text)
    except (TypeError, ValueError):
        return parse_german_float(text)


def escape_html(text: str) -> str:
    return html.escape(str(text or ""))


# ---------------------------------------------------------------------------
# Telegram Notifier
# ---------------------------------------------------------------------------

async def send_raw_telegram_message(
    client: httpx.AsyncClient,
    text: str,
    chat_id: str,
    bot_token: str,
) -> bool:
    if not bot_token or not chat_id:
        return False
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    try:
        resp = await client.post(url, json=payload, timeout=12.0)
        resp.raise_for_status()
        return True
    except Exception as exc:
        logging.error("Ошибка отправки сообщения в Telegram: %s", exc)
        return False


async def send_telegram_alert(
    client: httpx.AsyncClient,
    flat: Dict[str, Any],
    chat_id: Optional[str] = None,
    bot_token: Optional[str] = None,
    profile: Optional[Any] = None,
) -> bool:
    if not bot_token or not chat_id:
        logging.warning("Telegram Bot Token или Chat ID не заданы для отправки.")
        return False

    source = escape_html(flat.get("source", "Landeseigene"))
    title = escape_html(flat.get("title", "Wohnung in Berlin"))
    address = escape_html(flat.get("address", "Berlin"))
    district = escape_html(flat.get("district", "Berlin"))
    rooms = flat.get("rooms", "—")
    area = flat.get("area", "—")
    warmmiete = flat.get("warmmiete")
    kaltmiete = flat.get("kaltmiete")
    wbs_req = flat.get("wbs_required")
    url = flat.get("url", "https://inberlinwohnen.de")

    wbs_status = "🟢 <b>WBS erforderlich</b>" if wbs_req is True else (
        "⚪ <b>Kein WBS nötig / Frei</b>" if wbs_req is False else "ℹ️ <b>WBS: Nicht angegeben</b>"
    )

    prices = []
    if warmmiete:
        prices.append(f"Warmmiete: <b>{warmmiete:.2f} €</b>")
    if kaltmiete:
        prices.append(f"Kaltmiete: <b>{kaltmiete:.2f} €</b>")
    price_str = " | ".join(prices) if prices else "Preis auf Anfrage"

    copy_text = cover_letter(flat, profile) if profile else ""

    headline = f"🏠 <b>Новая квартира ({source})</b>\n"
    if profile is not None:
        headline += f"👤 <b>{escape_html(getattr(profile, 'display_name', ''))}</b>\n"

    message_html = (
        headline
        + f"<b>{title}</b>\n\n"
        + f"📍 <b>Адрес:</b> {address} ({district})\n"
        + f"💶 <b>Стоимость:</b> {price_str}\n"
        + f"📐 <b>Параметры:</b> {rooms} комн. | {area} м²\n"
        + f"📑 <b>Статус:</b> {wbs_status}\n\n"
        + f"📝 <b>Шаблон быстрого отклика:</b>\n"
        + f"<code>{escape_html(copy_text)}</code>\n"
    )

    tg_api_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message_html,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
        "reply_markup": {
            "inline_keyboard": [
                [
                    {"text": "🔗 Zum Angebot (Открыть объявление)", "url": url}
                ]
            ]
        },
    }

    try:
        resp = await client.post(tg_api_url, json=payload, timeout=12.0)
        resp.raise_for_status()
        logging.info("Telegram алерт успешно отправлен для ID: %s (%s)", flat.get("flat_id"), source)
        return True
    except Exception as e:
        logging.error("Ошибка при отправке в Telegram для %s: %s", flat.get("flat_id"), e)
        return False


# ---------------------------------------------------------------------------
# Webhook Notifier
# ---------------------------------------------------------------------------

async def send_webhook_alert_with_retries(
    flat: Dict[str, Any],
    webhook_url: str,
    payload: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
    label: Optional[str] = None,
    retry_delays: Optional[List[int]] = None,
) -> bool:
    delays = WEBHOOK_RETRY_DELAYS if retry_delays is None else retry_delays
    body = payload if payload is not None else monitor_dispatch.build_profile_webhook_payload(flat, None)
    req_headers = headers if headers is not None else {"Content-Type": "application/json"}
    target = label or "вебхука"

    async with httpx.AsyncClient(timeout=20.0) as client:
        for attempt, delay in enumerate(delays, 1):
            try:
                resp = await client.post(webhook_url, json=body, headers=req_headers)
                if resp.status_code in (200, 201, 202, 204):
                    logging.info("Вебхук %s успешно доставлен: %s (HTTP %d, попытка %d/%d)",
                                 target, flat.get("url"), resp.status_code, attempt, len(delays))
                    return True
                logging.warning("Вебхук вернул HTTP %d (попытка %d/%d) для %s: %s",
                                resp.status_code, attempt, len(delays), flat.get("url"), resp.text[:200])
            except Exception as exc:
                logging.warning("Ошибка соединения с вебхуком (попытка %d/%d) для %s: %s",
                                attempt, len(delays), flat.get("url"), exc)

            if attempt < len(delays):
                await asyncio.sleep(delay)

    logging.error("Не удалось доставить вебхук %s для %s после %d попыток.", target, flat.get("url"), len(delays))
    return False


# ---------------------------------------------------------------------------
# Scrapers
# ---------------------------------------------------------------------------

async def fetch_wbm(client: httpx.AsyncClient) -> List[Dict[str, Any]]:
    """Primary source: WBM Wohnungsbaugesellschaft Berlin-Mitte."""
    flats = []
    url = "https://www.wbm.de/wohnungen-berlin/angebote/"
    try:
        headers = get_random_headers()
        resp = await client.get(url, headers=headers, timeout=15.0, follow_redirects=True)
        if resp.status_code != 200:
            logging.warning("WBM HTTP %d", resp.status_code)
            return flats

        items = re.findall(
            r"(<div class=\"row openimmo-search-list-item\".*?)(?=<div class=\"row openimmo-search-list-item\"|<footer|<!--TYPO3SEARCH_end-->)",
            resp.text,
            re.DOTALL
        )
        for it in items:
            try:
                m_uid = re.search(r"data-uid=\"([^\"]+)\"", it)
                m_id = re.search(r"data-id=\"([^\"]+)\"", it)
                raw_id = m_uid.group(1) if m_uid else (m_id.group(1) if m_id else str(hash(it)))
                flat_id = f"wbm_{raw_id}"

                m_title = re.search(r"<h2 class=\"imageTitle\">(.*?)</h2>", it, re.DOTALL)
                title = html.unescape(m_title.group(1)).strip() if m_title else "WBM Wohnung"

                m_district = re.search(r"<div class=\"area\">(.*?)</div>", it, re.DOTALL)
                district = html.unescape(m_district.group(1)).strip() if m_district else "Berlin"

                m_addr = re.search(r"<div class=\"address\">(.*?)</div>", it, re.DOTALL)
                address = html.unescape(m_addr.group(1)).strip() if m_addr else district

                m_rent = re.search(r"<div class=\"main-property-value main-property-rent\">([^<]+)</div>", it)
                m_size = re.search(r"<div class=\"main-property-value main-property-size\">([^<]+)</div>", it)
                m_rooms = re.search(r"<div class=\"main-property-value main-property-rooms\">([^<]+)</div>", it)

                warmmiete = parse_german_float(m_rent.group(1)) if m_rent else None
                area = parse_german_float(m_size.group(1)) if m_size else None
                rooms = parse_german_float(m_rooms.group(1)) if m_rooms else None

                m_link = re.search(r"href=\"([^\"]*details/[^\"]*)\"", it)
                rel_link = m_link.group(1) if m_link else ""
                prop_url = f"https://www.wbm.de{rel_link}" if rel_link.startswith("/") else rel_link or url

                wbs = True if "wbs" in it.lower() else (False if "ohne wbs" in it.lower() else None)

                flats.append({
                    "flat_id": flat_id,
                    "object_id": raw_id,
                    "source": "WBM",
                    "title": title,
                    "address": address,
                    "district": district,
                    "rooms": rooms,
                    "area": area,
                    "kaltmiete": None,
                    "warmmiete": warmmiete,
                    "wbs_required": wbs,
                    "raw_text": it,
                    "url": prop_url,
                })
            except Exception as e:
                logging.debug("Ошибка при обработке элемента WBM: %s", e)
    except Exception as e:
        logging.warning("Сбой при опросе WBM: %s", e)
    return flats


async def fetch_gewobag(client: httpx.AsyncClient) -> List[Dict[str, Any]]:
    """Primary source: GEWOBAG."""
    flats = []
    url = "https://www.gewobag.de/fuer-mietinteressentinnen/mietangebote/?gesamtflaeche_bis=&gesamtflaeche_von=&gesamtmiete_bis=&gesamtmiete_von=&objekttyp%5B%5D=wohnung&sort-by=&zimmer_bis=&zimmer_von="
    try:
        headers = get_random_headers()
        resp = await client.get(url, headers=headers, timeout=15.0, follow_redirects=True)
        if resp.status_code != 200:
            logging.warning("GEWOBAG HTTP %d", resp.status_code)
            return flats

        articles = re.findall(
            r"(<article[^>]*id=\"post-(\d+)\"[^>]*>.*?</article>)",
            resp.text,
            re.DOTALL | re.IGNORECASE
        )
        for full_art, post_id in articles:
            try:
                flat_id = f"gewobag_{post_id}"

                m_title = re.search(r"<h3 class=\"angebot-title\">(.*?)</h3>", full_art, re.DOTALL)
                title = html.unescape(m_title.group(1)).strip() if m_title else "GEWOBAG Wohnung"

                m_addr = re.search(r"<address>(.*?)</address>", full_art, re.DOTALL)
                address = html.unescape(m_addr.group(1)).strip() if m_addr else "Berlin"

                m_reg = re.search(r"<tr class=\"angebot-region\">.*?<td>(.*?)</td>", full_art, re.DOTALL)
                district = html.unescape(m_reg.group(1)).strip() if m_reg else "Berlin"

                m_area_rooms = re.search(r"<tr class=\"angebot-area\">.*?<td>(.*?)</td>", full_art, re.DOTALL)
                area_str = m_area_rooms.group(1) if m_area_rooms else ""
                
                m_r = re.search(r"([\d\.,]+)\s*Zimmer", area_str, re.IGNORECASE)
                m_a = re.search(r"([\d\.,]+)\s*m²", area_str)
                rooms = parse_german_float(m_r.group(1)) if m_r else None
                area = parse_german_float(m_a.group(1)) if m_a else None

                m_rent = re.search(r"<tr class=\"angebot-kosten\">.*?<td>(.*?)</td>", full_art, re.DOTALL)
                rent_str = m_rent.group(1) if m_rent else ""
                m_p = re.search(r"([\d\.,]+)\s*(?:&euro;|€)", rent_str)
                warmmiete = parse_german_float(m_p.group(1)) if m_p else None

                m_link = re.search(r"<a href=\"([^\"]+)\" class=\"read-more-link\">", full_art)
                prop_url = m_link.group(1) if m_link else url

                wbs = True if "wbs" in full_art.lower() else False

                flats.append({
                    "flat_id": flat_id,
                    "object_id": post_id,
                    "source": "GEWOBAG",
                    "title": title,
                    "address": address,
                    "district": district,
                    "rooms": rooms,
                    "area": area,
                    "kaltmiete": None,
                    "warmmiete": warmmiete,
                    "wbs_required": wbs,
                    "raw_text": full_art,
                    "url": prop_url,
                })
            except Exception as e:
                logging.debug("Ошибка при обработке элемента GEWOBAG: %s", e)
    except Exception as e:
        logging.warning("Сбой при опросе GEWOBAG: %s", e)
    return flats


async def fetch_gesobau(client: httpx.AsyncClient) -> List[Dict[str, Any]]:
    """Primary source: GESOBAU."""
    flats = []
    url = "https://www.gesobau.de/mieten/wohnungssuche/"
    try:
        headers = get_random_headers()
        resp = await client.get(url, headers=headers, timeout=15.0, follow_redirects=True)
        if resp.status_code != 200:
            logging.warning("GESOBAU HTTP %d", resp.status_code)
            return flats

        for m in re.finditer(r"<a href=\"([^\"]*detailseite[^\"]*)\">([^<]+)</a>", resp.text):
            try:
                detail_link = m.group(1)
                title = html.unescape(m.group(2)).strip()
                start = max(0, m.start() - 300)
                end = min(len(resp.text), m.end() + 500)
                snippet = resp.text[start:end]

                uid_match = re.search(r"/detailseite/([^/]+)/", detail_link)
                raw_id = uid_match.group(1) if uid_match else str(hash(detail_link))
                flat_id = f"gesobau_{raw_id}"

                m_r = re.search(r"([\d\.,]+)\s*Zimmer", snippet, re.IGNORECASE)
                m_a = re.search(r"([\d\.,]+)\s*m²", snippet)
                m_p = re.search(r"([\d\.,]+)\s*€", snippet)

                rooms = parse_german_float(m_r.group(1)) if m_r else None
                area = parse_german_float(m_a.group(1)) if m_a else None
                warmmiete = parse_german_float(m_p.group(1)) if m_p else None

                prop_url = f"https://www.gesobau.de{detail_link}" if detail_link.startswith("/") else detail_link
                wbs = True if "wbs" in snippet.lower() else False

                flats.append({
                    "flat_id": flat_id,
                    "object_id": raw_id,
                    "source": "GESOBAU",
                    "title": title,
                    "address": "Berlin",
                    "district": "Berlin",
                    "rooms": rooms,
                    "area": area,
                    "kaltmiete": None,
                    "warmmiete": warmmiete,
                    "wbs_required": wbs,
                    "raw_text": snippet,
                    "url": prop_url,
                })
            except Exception as e:
                logging.debug("Ошибка при обработке элемента GESOBAU: %s", e)
    except Exception as e:
        logging.warning("Сбой при опросе GESOBAU: %s", e)
    return flats


async def fetch_howoge(client: httpx.AsyncClient) -> List[Dict[str, Any]]:
    """Primary source: HOWOGE JSON API."""
    flats = []
    url = "https://www.howoge.de/?type=424&tx_howoge_json_list[action]=loadProperties"
    try:
        headers = get_random_headers()
        resp = await client.get(url, headers=headers, timeout=15.0, follow_redirects=True)
        if resp.status_code != 200:
            logging.warning("HOWOGE API HTTP %d", resp.status_code)
            return flats

        data = resp.json()
        for item in data.get("immoobjects", []):
            try:
                uid = str(item.get("uid"))
                flat_id = f"howoge_{uid}"
                title = item.get("title") or "HOWOGE Wohnung"
                rooms = parse_german_float(item.get("rooms"))
                area = parse_german_float(item.get("area"))
                warmmiete = parse_german_float(item.get("rent"))
                district = item.get("district", "Berlin")
                features = item.get("features", [])
                notice = item.get("notice", "")
                
                wbs_raw = str(item.get("wbs", "")).lower()
                wbs_required = True if "ja" in wbs_raw or "erforderlich" in wbs_raw else (False if "nein" in wbs_raw else None)

                rel_link = item.get("link", "")
                full_url = f"https://www.howoge.de{rel_link}" if rel_link.startswith("/") else rel_link or "https://www.howoge.de"

                raw_combined = f"{title} {district} {notice} {' '.join(features) if isinstance(features, list) else ''}"

                flats.append({
                    "flat_id": flat_id,
                    "object_id": uid,
                    "source": "HOWOGE",
                    "title": title,
                    "address": title,
                    "district": district,
                    "rooms": rooms,
                    "area": area,
                    "kaltmiete": None,
                    "warmmiete": warmmiete,
                    "wbs_required": wbs_required,
                    "raw_text": raw_combined,
                    "url": full_url,
                })
            except Exception as e:
                logging.debug("Ошибка при обработке элемента HOWOGE: %s", e)
    except Exception as e:
        logging.warning("Сбой при опросе HOWOGE: %s", e)
    return flats


async def fetch_degewo(client: httpx.AsyncClient) -> List[Dict[str, Any]]:
    """Primary source: degewo immosuche API."""
    flats = []
    url = "https://immosuche.degewo.de/de/search.json"
    params = {"property_type_id": 1, "categories[]": 1}
    try:
        headers = get_random_headers()
        headers["Accept"] = "application/json, text/plain, */*"
        resp = await client.get(url, headers=headers, params=params, timeout=10.0, follow_redirects=True)
        if resp.status_code == 200:
            data = resp.json()
            for item in data.get("immos", []):
                try:
                    raw_id = str(item.get("id"))
                    flat_id = f"degewo_{raw_id}"
                    title = item.get("headline") or "degewo Wohnung"
                    rooms = parse_german_float(item.get("number_of_rooms"))
                    area = parse_german_float(item.get("living_space"))
                    warmmiete = parse_german_float(item.get("total_rent"))
                    kaltmiete = parse_german_float(item.get("cold_rent"))
                    address = item.get("full_address") or "Berlin"
                    slug = item.get("slug", "")
                    prop_url = f"https://immosuche.degewo.de/de/properties/{slug}" if slug else f"https://immosuche.degewo.de/de/properties/{raw_id}"
                    
                    raw_combined = f"{title} {address} {json.dumps(item, ensure_ascii=False)}"

                    flats.append({
                        "flat_id": flat_id,
                        "object_id": raw_id,
                        "source": "degewo",
                        "title": title,
                        "address": address,
                        "district": "Berlin",
                        "rooms": rooms,
                        "area": area,
                        "kaltmiete": kaltmiete,
                        "warmmiete": warmmiete,
                        "wbs_required": item.get("wbs_required"),
                        "raw_text": raw_combined,
                        "url": prop_url,
                    })
                except Exception as e:
                    logging.debug("Ошибка при парсинге degewo: %s", e)
    except Exception as e:
        logging.debug("degewo API недоступен или таймаут: %s", e)
    return flats


async def fetch_inberlinwohnen(client: httpx.AsyncClient) -> List[Dict[str, Any]]:
    """Aggregator: inBerlinWohnen (all 6 Landeseigene companies)."""
    flats = []
    url = "https://www.inberlinwohnen.de/wohnungsfinder/"
    try:
        headers = get_random_headers()
        resp = await client.get(url, headers=headers, timeout=15.0, follow_redirects=True)
        if resp.status_code != 200:
            logging.warning("inBerlinWohnen HTTP %d", resp.status_code)
            return flats

        snapshots = re.findall(r'wire:snapshot="([^"]+)"', resp.text)
        for snap in snapshots:
            try:
                data = json.loads(html.unescape(snap))
                item_data = data.get("data", {}).get("item")
                if not isinstance(item_data, list) or not item_data or not isinstance(item_data[0], dict):
                    continue
                item = item_data[0]
                flat_id = f"ibw_{item.get('id')}"
                title = item.get("title") or "Wohnung inberlinwohnen"
                rooms = parse_german_float(item.get("rooms"))
                area = parse_german_float(item.get("area"))
                kaltmiete = parse_german_float(item.get("rentNet"))
                warmmiete = parse_german_float(item.get("rentGross"))
                
                addr_info = item.get("address", [{}])[0] if isinstance(item.get("address"), list) and item.get("address") else {}
                street = addr_info.get("street", "")
                number = addr_info.get("number", "")
                zip_code = addr_info.get("zipCode", "")
                district = addr_info.get("district", "Berlin")
                address = f"{street} {number}, {zip_code} {district}".strip(", ")
                
                company_info = item.get("company", [{}])[0] if isinstance(item.get("company"), list) and item.get("company") else {}
                company_name = company_info.get("name", "inBerlinWohnen").strip()

                wbs_required = None
                details = item.get("details", [])
                details_str = json.dumps(details, ensure_ascii=False).lower()
                if "wbs" in details_str:
                    if "erforderlich" in details_str:
                        wbs_required = True
                    elif "nicht erforderlich" in details_str:
                        wbs_required = False

                link = item.get("deeplink") or url
                raw_combined = f"{title} {address} {details_str} {json.dumps(item.get('attributes', []), ensure_ascii=False)}"

                flats.append({
                    "flat_id": flat_id,
                    "object_id": item.get("objectId", ""),
                    "source": company_name or "inBerlinWohnen",
                    "title": title,
                    "address": address or "Berlin",
                    "district": district,
                    "rooms": rooms,
                    "area": area,
                    "kaltmiete": kaltmiete,
                    "warmmiete": warmmiete,
                    "wbs_required": wbs_required,
                    "raw_text": raw_combined,
                    "url": link,
                })
            except Exception as e:
                logging.debug("Ошибка при обработке элемента inBerlinWohnen: %s", e)
    except Exception as e:
        logging.warning("Сбой при опросе inBerlinWohnen: %s", e)
    return flats


async def fetch_stadtundland(client: httpx.AsyncClient) -> List[Dict[str, Any]]:
    """Primary source: STADT UND LAND listing API."""
    flats = []
    url = "https://d2396ha8oiavw0.cloudfront.net/sul-main/immoSearch"
    headers = get_random_headers()
    headers["Accept"] = "application/json"
    headers["Content-Type"] = "application/json"
    headers["Origin"] = "https://stadtundland.de"
    headers["Referer"] = "https://stadtundland.de/wohnungssuche"
    offset = 0
    limit = 50
    try:
        while offset < 400:
            resp = await client.post(
                url,
                headers=headers,
                json={"limit": limit, "offset": offset},
                timeout=15.0,
            )
            if resp.status_code != 200:
                logging.warning("Stadt und Land HTTP %d", resp.status_code)
                break
            payload = resp.json()
            items = payload.get("data") or []
            total = int(payload.get("count") or 0)
            for item in items:
                try:
                    details = item.get("details") or {}
                    if str(details.get("immoType") or "").lower() != "wohnung":
                        continue
                    immo_number = str(details.get("immoNumber") or "").strip()
                    if not immo_number:
                        continue
                    address_info = item.get("address") or {}
                    street = str(address_info.get("street") or "").strip()
                    number = str(address_info.get("house_number") or "").strip()
                    plz = str(address_info.get("postal_code") or "").strip()
                    district = str(address_info.get("precinct") or "Berlin").strip()
                    address = f"{street} {number}, {plz} Berlin".strip(" ,")
                    costs = item.get("costs") or {}
                    title = item.get("headline") or "Stadt und Land Wohnung"
                    raw_bits = [title, address, district]
                    if details.get("seniorsFriendly") is True:
                        raw_bits.append("seniorenwohnung")
                    prop_url = "https://stadtundland.de/wohnungssuche/" + quote(immo_number, safe="")
                    flats.append({
                        "flat_id": f"stadtundland_{immo_number.replace('/', '_')}",
                        "object_id": immo_number,
                        "source": "Stadt und Land",
                        "title": title,
                        "address": address or "Berlin",
                        "district": district,
                        "rooms": _parse_api_number(details.get("rooms")),
                        "area": _parse_api_number(details.get("livingSpace")),
                        "kaltmiete": _parse_api_number(costs.get("coldRent")),
                        "warmmiete": _parse_api_number(costs.get("warmRent") or costs.get("totalRent")),
                        "wbs_required": None,
                        "raw_text": " ".join(raw_bits),
                        "url": prop_url,
                    })
                except Exception as e:
                    logging.debug("Ошибка при обработке элемента Stadt und Land: %s", e)
            offset += limit
            if not items or offset >= total:
                break
    except Exception as e:
        logging.warning("Сбой при опросе Stadt und Land: %s", e)
    return flats


async def fetch_berlinovo(client: httpx.AsyncClient) -> List[Dict[str, Any]]:
    """Primary source: Berlinovo Drupal search pages."""
    flats = []
    seen_ids = set()
    base = "https://www.berlinovo.de/de/wohnungen/suche"
    try:
        for page in range(0, 20):
            url = base if page == 0 else f"{base}?page={page}"
            headers = get_random_headers()
            resp = await client.get(url, headers=headers, timeout=15.0, follow_redirects=True)
            if resp.status_code != 200:
                logging.warning("Berlinovo HTTP %d", resp.status_code)
                break
            articles = re.findall(
                r'(<article[^>]*node--type-apartment.*?</article>)',
                resp.text,
                re.DOTALL,
            )
            if not articles:
                break
            page_new = 0
            for art in articles:
                try:
                    about = re.search(r'about="([^"]+)"', art)
                    href = about.group(1) if about else ""
                    obj_id = href.rstrip("/").split("/")[-1]
                    if not obj_id or obj_id in seen_ids:
                        continue
                    seen_ids.add(obj_id)
                    page_new += 1
                    title_m = re.search(r'field--name-title.*?<a[^>]*>(.*?)</a>', art, re.DOTALL)
                    title = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", title_m.group(1)))).strip() if title_m else "Berlinovo Wohnung"
                    if any(word in title.lower() for word in ("stellplatz", "garage", "gewerbe")):
                        continue
                    street_m = re.search(r'class="address-line1">([^<]+)</span>', art)
                    plz_m = re.search(r'class="postal-code">([^<]+)</span>', art)
                    street = html.unescape(street_m.group(1)).strip() if street_m else ""
                    plz = html.unescape(plz_m.group(1)).strip() if plz_m else ""
                    address = f"{street}, {plz} Berlin".strip(" ,") if street or plz else "Berlin"
                    rent_m = re.search(r'field--name-field-total-rent.*?field__item">\s*([\d\.,]+)', art, re.DOTALL)
                    rooms_m = re.search(r'field--name-field-rooms.*?field__item">\s*([\d\.,]+)', art, re.DOTALL)
                    avail_m = re.search(r'field--name-field-available-date.*?<time[^>]*>([^<]+)</time>', art, re.DOTALL)
                    wbs_vals = re.findall(r'field--name-field-wbs.*?field__item">\s*([^<]*)', art, re.DOTALL)
                    wbs_clean = [v.strip() for v in wbs_vals if v.strip()]
                    if "1" in wbs_clean:
                        wbs_required = True
                    elif "0" in wbs_clean:
                        wbs_required = False
                    else:
                        wbs_required = None
                    prop_url = f"https://www.berlinovo.de{href}" if href.startswith("/") else href or base
                    flats.append({
                        "flat_id": f"berlinovo_{obj_id}",
                        "object_id": obj_id,
                        "source": "Berlinovo",
                        "title": title,
                        "address": address,
                        "district": "Berlin",
                        "rooms": parse_german_float(rooms_m.group(1)) if rooms_m else None,
                        "area": None,
                        "kaltmiete": None,
                        "warmmiete": parse_german_float(rent_m.group(1)) if rent_m else None,
                        "wbs_required": wbs_required,
                        "available_from": avail_m.group(1) if avail_m else None,
                        "raw_text": f"{title} {address} {art}",
                        "url": prop_url,
                    })
                except Exception as e:
                    logging.debug("Ошибка при обработке элемента Berlinovo: %s", e)
            if page_new == 0:
                break
    except Exception as e:
        logging.warning("Сбой при опросе Berlinovo: %s", e)
    return flats


# ---------------------------------------------------------------------------
# Runner & Lifecycle
# ---------------------------------------------------------------------------

async def run_monitoring_loop(
    http_client: httpx.AsyncClient,
    db_conn: sqlite3.Connection,
    stop_event: asyncio.Event,
    once: bool = False,
    dry_run: bool = False,
) -> Dict[str, Any]:
    stats = {"runs": 0, "notifications": 0, "status": "completed"}
    while not stop_event.is_set():
        stats["runs"] += 1
        try:
            matches = await monitor_dispatch.monitor_profiles(
                sys.modules[__name__],
                http_client,
                db_conn,
                dry_run=dry_run,
            )
            stats["notifications"] += matches
        except Exception as exc:
            logging.error("Сбой в цикле мониторинга: %s", exc, exc_info=True)
            stats["status"] = "error"

        if once:
            break

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=CHECK_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            pass

    return stats


async def main() -> None:
    parser = argparse.ArgumentParser(description="Berlin Flat Hunter Monitor & Auto-Apply")
    parser.add_argument("--once", action="store_true", help="Одиночный прогон с выводом JSON-отчёта и выходом")
    parser.add_argument("--dry-run", action="store_true", help="Тестовый прогон без записи в БД и реальной отправки")
    parser.add_argument("--test-webhook", nargs="?", const="primary", default=None, metavar="PROFILE",
                        help="Отправить тестовое объявление на вебхук профиля")
    parser.add_argument("--url", type=str, default=None, help="Опциональный URL вебхука для теста")
    args = parser.parse_args()

    if args.test_webhook:
        ok = await monitor_dispatch.send_test_webhook_for_profile(args.test_webhook, webhook_url=args.url)
        sys.exit(0 if ok else 1)

    db_conn = init_db()
    stop_event = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda s=sig: (logging.info(f"Получен сигнал {s.name}. Завершение..."), stop_event.set()))
        except NotImplementedError:
            pass

    limits = httpx.Limits(max_keepalive_connections=5, max_connections=10)
    async with httpx.AsyncClient(limits=limits, timeout=20.0, follow_redirects=True) as client:
        if not args.once:
            logging.info("==================================================")
            logging.info("Berlin Flat Hunter запущен (PID %d)", os.getpid())
            logging.info("Интервал проверки: %d секунд", CHECK_INTERVAL_SECONDS)
            logging.info("База данных: %s", DB_PATH)
            logging.info("==================================================")

        stats = await run_monitoring_loop(
            client,
            db_conn,
            stop_event,
            once=args.once,
            dry_run=args.dry_run,
        )

        if background_tasks:
            logging.debug("Ожидание завершения %d фоновых задач...", len(background_tasks))
            await asyncio.gather(*list(background_tasks), return_exceptions=True)

    db_conn.close()

    if args.once:
        print(json.dumps(stats, indent=2, ensure_ascii=False))
        sys.exit(0 if stats.get("status") == "completed" else 1)

    logging.info("Сервис мониторинга корректно остановлен.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
