"""Fan-out of flat scrapes across search profiles."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from profile_config import (
    SUPPORTED_FETCHERS,
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
from appliers.wbm import apply_wbm

_PROFILE_CACHE: Dict[str, Any] = {"profiles": None, "mtime": None}


def _schema(conn: sqlite3.Connection) -> None:
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


def is_profile_seen(conn: sqlite3.Connection, profile_id: str, flat: Dict[str, Any]) -> bool:
    keys = dedup_keys(flat)
    if not keys:
        return False
    placeholders = ",".join("?" * len(keys))
    row = conn.execute(
        f"SELECT 1 FROM profile_seen WHERE profile_id = ? AND dedup_key IN ({placeholders})",
        [profile_id, *keys],
    ).fetchone()
    return row is not None


def mark_profile_seen(conn: sqlite3.Connection, profile_id: str, flat: Dict[str, Any]) -> None:
    keys = dedup_keys(flat)
    if not keys:
        return
    price = flat.get("warmmiete") or flat.get("kaltmiete") or 0.0
    title = flat.get("title") or ""
    url = flat.get("url") or ""
    source = flat.get("source") or flat.get("fetcher") or ""
    object_id = str(flat.get("object_id") or "")
    for key in keys:
        conn.execute(
            """
            INSERT OR IGNORE INTO profile_seen
                (profile_id, dedup_key, source, object_id, url, price, title)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (profile_id, key, source, object_id, url, price, title),
        )
    conn.commit()


def build_auth_headers(auth_val: str) -> Dict[str, str]:
    headers = {"Content-Type": "application/json"}
    auth_val = (auth_val or "").strip()
    if not auth_val:
        return headers
    if ":" in auth_val:
        key, value = auth_val.split(":", 1)
        headers[key.strip()] = value.strip()
    elif auth_val.lower().startswith("bearer ") or auth_val.lower().startswith("basic "):
        headers["Authorization"] = auth_val
    else:
        headers["Authorization"] = f"Bearer {auth_val}"
    return headers


def _json_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _json_rooms(value: Any) -> Any:
    number = _json_float(value)
    if number is None:
        return None
    if number.is_integer():
        return int(number)
    return number


def build_profile_webhook_payload(flat: Dict[str, Any], profile: Any, test: bool = False) -> Dict[str, Any]:
    object_id = flat.get("object_id")
    object_id_out = None if object_id is None or str(object_id).strip() == "" else str(object_id)
    payload: Dict[str, Any] = {
        "profile": profile.id,
        "source": flat.get("source") or None,
        "object_id": object_id_out,
        "url": flat.get("url") or None,
        "title": flat.get("title") or None,
        "address": flat.get("address") or None,
        "district": flat.get("district") or None,
        "rooms": _json_rooms(flat.get("rooms")),
        "size_m2": _json_float(flat.get("area")),
        "cold_rent": _json_float(flat.get("kaltmiete")),
        "warm_rent": _json_float(flat.get("warmmiete")),
        "wbs": extract_wbs_label(flat),
        "available_from": extract_available_from(flat),
        "found_at": flat.get("found_at") or datetime.now(timezone.utc).isoformat(),
        "cover_letter": cover_letter(flat, profile),
    }
    if test:
        payload["test"] = True
    return payload


def _mask_headers(headers: Dict[str, str]) -> Dict[str, str]:
    masked = {}
    for key, value in headers.items():
        if key.lower() == "content-type" or not value:
            masked[key] = value
        else:
            masked[key] = value[:4] + "…"
    return masked


def load_profiles_cached(path: Optional[str] = None, force: bool = False) -> List[Any]:
    import profile_config
    cfg_path = path or profile_config.default_profiles_path()
    try:
        current_mtime = os.path.getmtime(cfg_path)
    except OSError:
        current_mtime = 0

    if not force and _PROFILE_CACHE["profiles"] is not None and _PROFILE_CACHE["mtime"] == current_mtime:
        return _PROFILE_CACHE["profiles"]

    profiles = load_profiles(cfg_path)
    _PROFILE_CACHE["profiles"] = profiles
    _PROFILE_CACHE["mtime"] = current_mtime
    logging.debug("Загружены профили из %s: %s", cfg_path, ", ".join(p.describe() for p in profiles))
    return profiles


def _schedule_webhook(fm: Any, coro: Any, flat_id: Any) -> None:
    try:
        task = asyncio.create_task(coro)
        fm.background_tasks.add(task)
        task.add_done_callback(fm.background_tasks.discard)
    except Exception as exc:
        logging.error("Сбой запуска вебхука для %s: %s", flat_id, exc)


async def _notify(fm: Any, client: Any, profile: Any, flat: Dict[str, Any], dry_run: bool = False) -> bool:
    flat_id = flat.get("flat_id")
    price = flat.get("warmmiete") or flat.get("kaltmiete") or 0.0
    title = flat.get("title") or ""
    sent = False

    logging.info(
        "🔥 [%s] Найдена подходящая квартира: [%s] %s | %s комн. | %s м² | %s €",
        profile.id,
        flat.get("source"),
        title,
        flat.get("rooms"),
        flat.get("area"),
        price,
    )

    # Auto-Apply check for WBM
    source_norm = (flat.get("source") or flat.get("fetcher") or "").lower()
    if getattr(profile, "auto_apply", False) and "wbm" in source_norm:
        flat_url = flat.get("url") or ""
        applicant_info = getattr(profile, "applicant", {}) or {}
        apply_ok, apply_msg, form_sent = await apply_wbm(
            client,
            flat_url,
            applicant_info,
            dry_run=dry_run,
        )
        if apply_ok:
            logging.info("✅ Auto-Apply для %s успешно выполнен: %s", profile.id, apply_msg)
            # Send separate Auto-Apply Telegram alert
            if profile.telegram_chat_id:
                try:
                    applied_info = (
                        f"🤖 <b>Auto-Apply отправлен! ({profile.display_name})</b>\n\n"
                        f"🏢 <b>WBM:</b> {flat.get('title')}\n"
                        f"📍 <b>Адрес:</b> {flat.get('address')}\n"
                        f"ℹ️ <b>Статус:</b> {apply_msg}\n"
                        f"🔗 <a href=\"{flat_url}\">Объявление</a>"
                    )
                    await fm.send_raw_telegram_message(
                        client,
                        applied_info,
                        chat_id=profile.telegram_chat_id,
                        bot_token=profile.telegram_bot_token or None,
                    )
                except Exception as exc:
                    logging.error("Ошибка отправки Auto-Apply алерта в Telegram: %s", exc)
        else:
            logging.warning("⚠️ Auto-Apply для %s не удался: %s", profile.id, apply_msg)

    # Telegram notification
    if profile.telegram_chat_id:
        try:
            sent = await fm.send_telegram_alert(
                client,
                flat,
                chat_id=profile.telegram_chat_id,
                bot_token=profile.telegram_bot_token or None,
                profile=profile,
            )
        except Exception as exc:
            logging.error("Сбой отправки Telegram для %s/%s: %s", profile.id, flat_id, exc)
    else:
        logging.warning("Профиль %s: нет telegram_chat_id, уведомление пропущено", profile.id)

    # Webhook dispatch
    if profile.webhook_url:
        payload = build_profile_webhook_payload(flat, profile)
        headers = build_auth_headers(profile.webhook_key)
        _schedule_webhook(
            fm,
            fm.send_webhook_alert_with_retries(
                flat,
                profile.webhook_url,
                payload=payload,
                headers=headers,
                label=f"профиля {profile.id}",
            ),
            flat_id,
        )

    return bool(sent)


async def monitor_profiles(
    fm: Any,
    http_client: Any,
    db_conn: sqlite3.Connection,
    profiles: Optional[List[Any]] = None,
    dry_run: bool = False,
) -> int:
    """Run one pass of monitoring across all enabled profiles."""
    if profiles is None:
        profiles = load_profiles_cached()
    enabled = [profile for profile in profiles if profile.enabled]
    if not enabled:
        logging.warning("Нет включённых профилей в profiles.yaml.")
        return 0

    names = required_fetchers(enabled)
    logging.info("Начало проверки источников: %s", ", ".join(names))

    fetcher_map = {
        "wbm": fm.fetch_wbm,
        "gewobag": fm.fetch_gewobag,
        "gesobau": fm.fetch_gesobau,
        "howoge": fm.fetch_howoge,
        "degewo": fm.fetch_degewo,
        "stadtundland": fm.fetch_stadtundland,
        "berlinovo": fm.fetch_berlinovo,
        "inberlinwohnen": fm.fetch_inberlinwohnen,
    }

    tasks = [fetcher_map[name](http_client) for name in names if name in fetcher_map]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    all_flats: List[Dict[str, Any]] = []
    active_names = [name for name in names if name in fetcher_map]
    for name, result in zip(active_names, results):
        if isinstance(result, list):
            for flat in result:
                flat.setdefault("fetcher", name)
            all_flats.extend(result)
        elif isinstance(result, Exception):
            logging.warning("Источник %s упал: %s", name, result)

    logging.info("Собрано %d объявлений со всех первоисточников", len(all_flats))
    new_matches = 0

    for flat in all_flats:
        flat_id = flat.get("flat_id")
        if not flat_id:
            continue

        unseen = [p for p in enabled if not is_profile_seen(db_conn, p.id, flat)]
        if not unseen:
            continue

        if is_senior_housing(flat) and any(not p.allow_senior for p in unseen):
            logging.debug(
                "🚫 Отклонено (жилье для пожилых): [%s] %s",
                flat.get("source"),
                flat.get("title"),
            )

        if is_student_housing(flat) and any(not p.allow_student for p in unseen):
            logging.debug(
                "🚫 Отклонено (студенческое жилье/WG): [%s] %s",
                flat.get("source"),
                flat.get("title"),
            )

        for profile in unseen:
            try:
                ok, reason = matches_profile(flat, profile)
            except Exception as exc:
                logging.error("Сбой фильтра %s для %s: %s", profile.id, flat_id, exc)
                continue

            if ok:
                try:
                    if await _notify(fm, http_client, profile, flat, dry_run=dry_run):
                        new_matches += 1
                except Exception as exc:
                    logging.error("Сбой уведомления профиля %s для %s: %s", profile.id, flat_id, exc)

            if not dry_run:
                mark_profile_seen(db_conn, profile.id, flat)

    logging.info("Цикл завершен. Новых отправленных уведомлений: %d", new_matches)
    return new_matches


async def send_test_webhook_for_profile(profile_id: str, webhook_url: Optional[str] = None) -> bool:
    import flat_monitor as fm

    try:
        profiles = load_profiles()
    except Exception as exc:
        print(f"❌ Не удалось загрузить profiles.yaml: {exc}")
        return False
    profile = next((item for item in profiles if item.id == profile_id), None)
    if profile is None:
        known = ", ".join(item.id for item in profiles) or "—"
        print(f"❌ Профиль {profile_id!r} не найден. Доступны: {known}")
        return False
    target_url = (webhook_url or profile.webhook_url or "").strip()
    if not target_url:
        env_name = profile.webhook_url_env or f"{profile.id.upper()}_WEBHOOK_URL"
        print(f"❌ Ошибка: вебхук профиля {profile.id} не задан ({env_name} в .env) и не передан --url.")
        return False

    test_flat = {
        "flat_id": f"test_{profile.id}_001",
        "object_id": "test-obj-001",
        "url": "https://immosuche.degewo.de/de/properties/test-sample",
        "source": "degewo",
        "fetcher": "degewo",
        "title": "Test-Wohnung für Webhook Integration",
        "address": "Beispielstraße 1, 12043 Berlin",
        "district": "Neukölln",
        "rooms": 2.0,
        "area": 45.3,
        "kaltmiete": 400.0,
        "warmmiete": 520.0,
        "wbs_required": True,
        "wbs_label": "WBS 100 erforderlich",
        "available_from": "sofort",
        "found_at": datetime.now(timezone.utc).isoformat(),
    }
    payload = build_profile_webhook_payload(test_flat, profile, test=True)
    headers = build_auth_headers(profile.webhook_key)
    print("==================================================")
    print(f"🚀 Тестовое объявление для профиля {profile.id}")
    if not profile.enabled:
        print("ℹ️  Профиль выключен в конфигурации — тест всё равно отправляет одно объявление.")
    print(f"URL: {target_url}")
    print(f"Заголовки: {_mask_headers(headers)}")
    print("Тело запроса (JSON):")
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    print("==================================================")
    success = await fm.send_webhook_alert_with_retries(
        test_flat,
        webhook_url=target_url,
        payload=payload,
        headers=headers,
        label=f"профиля {profile.id}",
    )
    if success:
        print("\n✅ Тестовый вебхук успешно доставлен!")
    else:
        print("\n❌ Не удалось доставить тестовый вебхук после повторов.")
    return success
