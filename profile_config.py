"""Search profiles for the Berlin flat monitor.

Handles profile parsing from profiles.yaml, env-interpolation,
stop-word filters (seniors, students), and WBS category constraints.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

SENIOR_REGEXES = [
    re.compile(r"\b(ab\s*(?:55|60|65|70|75)\s*(?:jahren?|j|jahre)?)\b", re.IGNORECASE),
    re.compile(r"\bsenior(?:en)?(?:wohnung|gerecht)?\b", re.IGNORECASE),
    re.compile(r"\baltersgerecht\b", re.IGNORECASE),
    re.compile(r"\bbetreutes\s*wohnen\b", re.IGNORECASE),
    re.compile(r"(?:^|\s)60\+(?:\s|$|\b)", re.IGNORECASE),
    re.compile(r"\bpflege\b", re.IGNORECASE),
    re.compile(r"\bruhestand\b", re.IGNORECASE),
    re.compile(r"\bwbs\s*(?:mit\s*)?(?:besonderem\s*)?wohnbedarf\s*(?:für\s*ältere|60|65)\b", re.IGNORECASE),
    re.compile(r"\b(rentner|im alter|ruheständler)\b", re.IGNORECASE),
    re.compile(r"\bwbs\s*-?\s*(?:55|60|65|70|75)\b", re.IGNORECASE),
]

STUDENT_REGEXES = [
    re.compile(r"\bstudent(?:en)?(?:wohnung|wohnheim)?\b", re.IGNORECASE),
    re.compile(r"\bstudierende\b", re.IGNORECASE),
    re.compile(r"\bstudenten\s*only\b", re.IGNORECASE),
    re.compile(r"\b(nur|für)\s*immatrikulierte\b", re.IGNORECASE),
    re.compile(r"\bauszubildende\b", re.IGNORECASE),
    re.compile(r"\bazubi\b", re.IGNORECASE),
    re.compile(r"\bwg-zimmer\b", re.IGNORECASE),
    re.compile(r"\bwohngemeinschaft\b", re.IGNORECASE),
    re.compile(r"\bmöbliertes\s*zimmer\b", re.IGNORECASE),
]

SENIOR_STOP_WORDS = [
    "ab 60",
    "ab 65",
    "ab 55",
    "ab 70",
    "ab 75",
    "senior",
    "senioren",
    "seniorenwohnung",
    "seniorengerecht",
    "altersgerecht",
    "betreutes wohnen",
    "wbs 60",
    "wbs60",
    "wbs 65",
    "wbs 55",
    "rentner",
    "im alter",
    "ruheständler",
    "60 jahre",
    "65 jahre",
]

SUPPORTED_FETCHERS = (
    "wbm",
    "gewobag",
    "gesobau",
    "howoge",
    "degewo",
    "stadtundland",
    "berlinovo",
    "inberlinwohnen",
)

AGGREGATOR_COMPANIES = frozenset({
    "wbm",
    "gewobag",
    "gesobau",
    "howoge",
    "degewo",
    "stadtundland",
})

UNSUPPORTED_SOURCES = frozenset({
    "vonovia",
    "deutschewohnen",
    "immobilienscout24",
})

WBS_CATEGORIES = ("100", "140", "160", "180", "220")
_WBS_CAT_RE = re.compile(r"\bwbs\s*-?\s*(100|140|160|180|220)\b", re.IGNORECASE)
_WBS_RANGE_RE = re.compile(
    r"\bwbs\s*(100|140|160|180|220)\s*[-–/]\s*(100|140|160|180|220)\b",
    re.IGNORECASE,
)
_NUR_WBS_RE = re.compile(r"\bnur\s*(?:mit\s*)?wbs\s*-?\s*(100|140|160|180|220)\b", re.IGNORECASE)
_NO_WBS_RE = re.compile(r"\b(?:wbs\s*(?:erforderlich)?\s*:\s*nein|ohne\s*wbs|kein\s*wbs|wbs\s*nicht\s*erforderlich)\b", re.IGNORECASE)

_ENV_FULL = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")
_ENV_SUB = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_PLZ_RE = re.compile(r"\b(\d{5})\b")

_SOURCE_ALIASES = {
    "wbm": "wbm",
    "gewobag": "gewobag",
    "gesobau": "gesobau",
    "howoge": "howoge",
    "degewo": "degewo",
    "stadt und land": "stadtundland",
    "stadtundland": "stadtundland",
    "berlinovo": "berlinovo",
    "vonovia": "vonovia",
    "deutsche wohnen": "deutschewohnen",
    "deutschewohnen": "deutschewohnen",
    "immobilienscout24": "immobilienscout24",
    "immoscout24": "immobilienscout24",
    "immoscout": "immobilienscout24",
    "inberlinwohnen": "inberlinwohnen",
    "in berlin wohnen": "inberlinwohnen",
}

_KNOWN_FIELDS = {
    "id",
    "display_name",
    "enabled",
    "legacy",
    "allow_senior",
    "allow_student",
    "sources",
    "districts_include",
    "districts_exclude",
    "rooms_min",
    "rooms_max",
    "size_min_m2",
    "size_max_m2",
    "warm_rent_max",
    "cold_rent_max",
    "wbs_required_ok",
    "wbs_categories_ok",
    "household_size",
    "jobcenter_limit_warm",
    "exclude_keywords",
    "auto_apply",
    "applicant",
    "telegram_chat_id",
    "telegram_bot_token",
    "webhook_url",
    "webhook_key",
}


def default_profiles_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "profiles.yaml")


def normalize_source(name: str) -> str:
    s = (name or "").strip().lower().replace("&", " und ").replace("ß", "ss")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    if not s:
        return ""
    if s in _SOURCE_ALIASES:
        return _SOURCE_ALIASES[s]
    for key, val in _SOURCE_ALIASES.items():
        if key in s:
            return val
    return s.replace(" ", "")


def normalize_place(value: str) -> str:
    s = (value or "").strip().lower().replace("ß", "ss")
    s = s.replace("-", " ")
    return re.sub(r"\s+", " ", s).strip()


def listing_corpus(flat: Dict[str, Any]) -> str:
    return " ".join([
        str(flat.get("title") or ""),
        str(flat.get("description") or ""),
        str(flat.get("address") or ""),
        str(flat.get("district") or ""),
        str(flat.get("category") or ""),
        str(flat.get("tags") or ""),
        str(flat.get("details") or ""),
        str(flat.get("raw_text") or ""),
        str(flat.get("wbs_label") or ""),
    ]).strip()


def is_senior_housing(flat: Dict[str, Any]) -> bool:
    """Check whether the listing is restricted to seniors/retirees."""
    corpus = listing_corpus(flat)
    corpus_lower = corpus.lower()
    for word in SENIOR_STOP_WORDS:
        if word in corpus_lower:
            return True
    for pat in SENIOR_REGEXES:
        if pat.search(corpus):
            return True
    return False


def is_student_housing(flat: Dict[str, Any]) -> bool:
    """Check whether the listing is student housing / WG room."""
    corpus = listing_corpus(flat)
    for pat in STUDENT_REGEXES:
        if pat.search(corpus):
            return True
    return False


def extract_wbs_categories(flat: Dict[str, Any]) -> List[str]:
    text = listing_corpus(flat)
    if _NO_WBS_RE.search(text):
        return []
    found: List[str] = []
    for cat in _WBS_CAT_RE.findall(text):
        if cat not in found:
            found.append(cat)
    for left, right in _WBS_RANGE_RE.findall(text):
        ia, ib = WBS_CATEGORIES.index(left), WBS_CATEGORIES.index(right)
        if ia > ib:
            ia, ib = ib, ia
        for cat in WBS_CATEGORIES[ia:ib + 1]:
            if cat not in found:
                found.append(cat)
    return found


def extract_wbs_label(flat: Dict[str, Any]) -> Optional[str]:
    explicit = flat.get("wbs_label")
    if explicit:
        return str(explicit)
    text = listing_corpus(flat)
    if _NO_WBS_RE.search(text):
        return "Kein WBS erforderlich"
    cats = extract_wbs_categories(flat)
    besonderer = bool(re.search(r"besonderer\s*wohnbedarf", text, re.IGNORECASE))
    required = flat.get("wbs_required") is True
    if flat.get("wbs_required") is not True:
        if re.search(r"wbs[^\n]{0,48}erforderlich", text, re.IGNORECASE) or cats or besonderer:
            required = True
    if not cats and not besonderer and not required:
        return None
    if cats and besonderer:
        label = f"WBS {'/'.join(cats)} besonderer Wohnbedarf"
    elif cats:
        label = f"WBS {'/'.join(cats)}"
    elif besonderer:
        label = "besonderer Wohnbedarf"
    else:
        label = "WBS"
    if required and "erforderlich" not in label.lower():
        label += " erforderlich"
    return label


def extract_available_from(flat: Dict[str, Any]) -> Optional[str]:
    explicit = flat.get("available_from")
    if explicit:
        return str(explicit)
    text = " ".join([
        str(flat.get("raw_text") or ""),
        str(flat.get("details") or ""),
        str(flat.get("title") or ""),
    ])
    match = re.search(
        r"(?:verfügbar ab|verfuegbar ab|bezugsfrei ab|frei ab)\s*[:\s]*"
        r"(\d{1,2}\.\d{1,2}\.\d{2,4}|sofort|ab sofort)",
        text,
        re.IGNORECASE,
    )
    if not match:
        return None
    return match.group(1)


def listing_requires_wbs(flat: Dict[str, Any]) -> bool:
    text = listing_corpus(flat)
    if _NO_WBS_RE.search(text):
        return False
    if flat.get("wbs_required") is True:
        return True
    if flat.get("wbs_required") is False:
        return False
    if re.search(r"wbs[^\n]{0,48}erforderlich", text, re.IGNORECASE):
        return True
    if extract_wbs_categories(flat) or re.search(r"besonderer\s*wohnbedarf", text, re.IGNORECASE):
        return True
    return False


def _resolve_env(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _resolve_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_env(v) for v in value]
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    full = _ENV_FULL.fullmatch(stripped)
    if full:
        raw = os.getenv(full.group(1), "").strip()
        return raw or None
    return _ENV_SUB.sub(lambda m: os.getenv(m.group(1), ""), value)


def _as_float(value: Any) -> Optional[float]:
    value = _resolve_env(value)
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError(f"ожидалось число, получено {value!r}")
    if isinstance(value, (int, float)):
        return float(value)
    return float(str(value).replace(",", ".").strip())


def _as_int(value: Any) -> Optional[int]:
    number = _as_float(value)
    if number is None:
        return None
    return int(number)


def _as_bool(value: Any) -> Optional[bool]:
    value = _resolve_env(value)
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "ja", "да"}:
        return True
    if text in {"0", "false", "no", "nein", "нет"}:
        return False
    raise ValueError(f"ожидалось true/false, получено {value!r}")


def _as_str_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        resolved = _resolve_env(value)
        if not resolved:
            return []
        return [part.strip() for part in str(resolved).split(",") if part.strip()]
    out = []
    for item in value:
        resolved = _resolve_env(item)
        if resolved is None:
            continue
        text = str(resolved).strip()
        if text:
            out.append(text)
    return out


def _env_name(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    match = _ENV_FULL.fullmatch(value.strip())
    return match.group(1) if match else ""


@dataclass
class Profile:
    id: str
    display_name: str
    enabled: bool = True
    legacy: bool = False
    allow_senior: bool = False
    allow_student: bool = False
    sources: List[str] = field(default_factory=list)
    districts_include: List[str] = field(default_factory=list)
    districts_exclude: List[str] = field(default_factory=list)
    rooms_min: Optional[float] = None
    rooms_max: Optional[float] = None
    size_min_m2: Optional[float] = None
    size_max_m2: Optional[float] = None
    warm_rent_max: Optional[float] = None
    cold_rent_max: Optional[float] = None
    wbs_required_ok: Optional[bool] = None
    wbs_categories_ok: List[str] = field(default_factory=list)
    household_size: Optional[int] = None
    jobcenter_limit_warm: Optional[float] = None
    exclude_keywords: List[str] = field(default_factory=list)
    auto_apply: bool = False
    applicant: Dict[str, Any] = field(default_factory=dict)
    telegram_chat_id: str = ""
    telegram_bot_token: str = ""
    webhook_url: str = ""
    webhook_key: str = ""
    webhook_url_env: str = ""
    webhook_key_env: str = ""
    telegram_chat_id_env: str = ""

    def describe(self) -> str:
        state = "вкл" if self.enabled else "выкл"
        tg = "tg=да" if self.telegram_chat_id else "tg=нет"
        wh = "webhook=да" if self.webhook_url else "webhook=нет"
        apply = "apply=да" if self.auto_apply else "apply=нет"
        limits = (
            f"rooms {self.rooms_min or '—'}..{self.rooms_max or '—'}, "
            f"area {self.size_min_m2 or '—'}..{self.size_max_m2 or '—'}, "
            f"warm<={self.warm_rent_max or '—'}, cold<={self.cold_rent_max or '—'}"
        )
        return f"{self.id} ({state}, {apply}, {tg}, {wh}, {limits})"


def _profile_from_mapping(raw: Dict[str, Any]) -> Profile:
    if not isinstance(raw, dict):
        raise ValueError("блок профиля должен быть словарём")
    unknown = sorted(set(raw) - _KNOWN_FIELDS)
    profile_id = str(raw.get("id") or "").strip()
    if not profile_id:
        raise ValueError("у профиля нет id")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", profile_id):
        raise ValueError(f"недопустимый id профиля: {profile_id}")
    if unknown:
        logging.warning("Профиль %s: неизвестные поля %s", profile_id, ", ".join(unknown))

    sources = [normalize_source(item) for item in _as_str_list(raw.get("sources"))]
    sources = [item for item in sources if item]
    for source in sources:
        if source in UNSUPPORTED_SOURCES:
            logging.warning(
                "Профиль %s: источник %s пока не парсится "
                "(нет в WBM/GEWOBAG/GESOBAU/HOWOGE/degewo/inBerlinWohnen)",
                profile_id,
                source,
            )

    token = str(_resolve_env(raw.get("telegram_bot_token")) or "").strip()
    if not token:
        # Fallback to general token if profile specific token not configured
        token = os.getenv(f"TELEGRAM_BOT_TOKEN_{profile_id.upper()}", "").strip() or os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

    chat_id = str(_resolve_env(raw.get("telegram_chat_id")) or "").strip()
    if not chat_id:
        chat_id = os.getenv(f"TELEGRAM_CHAT_ID_{profile_id.upper()}", "").strip() or os.getenv("TELEGRAM_CHAT_ID", "").strip()

    categories = [str(item) for item in _as_str_list(raw.get("wbs_categories_ok"))]
    raw_applicant = raw.get("applicant") or {}
    resolved_applicant = _resolve_env(raw_applicant) if isinstance(raw_applicant, dict) else {}

    return Profile(
        id=profile_id,
        display_name=str(raw.get("display_name") or profile_id),
        enabled=bool(raw.get("enabled", True)),
        legacy=bool(raw.get("legacy", False)),
        allow_senior=bool(raw.get("allow_senior", False)),
        allow_student=bool(raw.get("allow_student", False)),
        sources=sources,
        districts_include=_as_str_list(raw.get("districts_include")),
        districts_exclude=_as_str_list(raw.get("districts_exclude")),
        rooms_min=_as_float(raw.get("rooms_min")),
        rooms_max=_as_float(raw.get("rooms_max")),
        size_min_m2=_as_float(raw.get("size_min_m2")),
        size_max_m2=_as_float(raw.get("size_max_m2")),
        warm_rent_max=_as_float(raw.get("warm_rent_max")),
        cold_rent_max=_as_float(raw.get("cold_rent_max")),
        wbs_required_ok=_as_bool(raw.get("wbs_required_ok")),
        wbs_categories_ok=categories,
        household_size=_as_int(raw.get("household_size")),
        jobcenter_limit_warm=_as_float(raw.get("jobcenter_limit_warm")),
        exclude_keywords=_as_str_list(raw.get("exclude_keywords")),
        auto_apply=bool(raw.get("auto_apply", False)),
        applicant=resolved_applicant,
        telegram_chat_id=chat_id,
        telegram_bot_token=token,
        webhook_url=str(_resolve_env(raw.get("webhook_url")) or "").strip(),
        webhook_key=str(_resolve_env(raw.get("webhook_key")) or "").strip(),
        webhook_url_env=_env_name(raw.get("webhook_url")),
        webhook_key_env=_env_name(raw.get("webhook_key")),
        telegram_chat_id_env=_env_name(raw.get("telegram_chat_id")),
    )


def load_profiles(path: Optional[str] = None) -> List[Profile]:
    path = path or default_profiles_path()
    if not os.path.exists(path):
        raise RuntimeError(f"Конфигурационный файл {path} не найден")
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError("PyYAML не установлен (pip install pyyaml)") from exc

    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    raw_list = data.get("profiles")
    if not isinstance(raw_list, list):
        raise ValueError(f"{path}: корневой ключ 'profiles' должен содержать список")

    profiles: List[Profile] = []
    seen_ids: set[str] = set()
    for item in raw_list:
        p = _profile_from_mapping(item)
        if p.id in seen_ids:
            raise ValueError(f"Дубликат id профиля: {p.id}")
        seen_ids.add(p.id)
        profiles.append(p)

    return profiles


def required_fetchers(profiles: Sequence[Profile]) -> List[str]:
    needed: set[str] = set()
    for p in profiles:
        if not p.enabled:
            continue
        if not p.sources:
            return list(SUPPORTED_FETCHERS)
        for s in p.sources:
            norm = normalize_source(s)
            if norm in AGGREGATOR_COMPANIES:
                needed.add("inberlinwohnen")
            if norm in SUPPORTED_FETCHERS:
                needed.add(norm)
    order = [f for f in SUPPORTED_FETCHERS if f in needed]
    return order or list(SUPPORTED_FETCHERS)


def dedup_keys(flat: Dict[str, Any]) -> List[str]:
    source = normalize_source(str(flat.get("source") or flat.get("fetcher") or ""))
    object_id = str(flat.get("object_id") or "").strip()
    url = (str(flat.get("url") or "")).strip().rstrip("/")
    keys: List[str] = []
    if object_id:
        keys.append(f"{source}|id|{object_id}")
    if url:
        keys.append(f"{source}|url|{url}")
        keys.append(f"url|{url}")
    if not keys and flat.get("flat_id"):
        keys.append(f"flat|{flat.get('flat_id')}")
    return keys


def _keyword_hit(flat: Dict[str, Any], keywords: Sequence[str]) -> Optional[str]:
    if not keywords:
        return None
    text = listing_corpus(flat).casefold()
    for keyword in keywords:
        needle = keyword.casefold().strip()
        if needle and needle in text:
            return keyword
    return None


def _source_ok(profile: Profile, flat: Dict[str, Any]) -> bool:
    if not profile.sources:
        return True
    wanted = set(profile.sources)
    fetcher = normalize_source(str(flat.get("fetcher") or ""))
    source = normalize_source(str(flat.get("source") or ""))
    return fetcher in wanted or source in wanted


def _place_matches(token: str, flat: Dict[str, Any]) -> bool:
    raw = token.strip()
    if not raw:
        return False
    hay = " ".join([
        str(flat.get("district") or ""),
        str(flat.get("address") or ""),
        str(flat.get("title") or ""),
    ])
    if re.fullmatch(r"\d{5}", raw):
        return raw in set(_PLZ_RE.findall(hay)) or raw in hay
    needle = normalize_place(raw)
    return needle in normalize_place(hay)


def _district_ok(profile: Profile, flat: Dict[str, Any]) -> bool:
    if profile.districts_include and not any(
        _place_matches(token, flat) for token in profile.districts_include
    ):
        return False
    if profile.districts_exclude and any(
        _place_matches(token, flat) for token in profile.districts_exclude
    ):
        return False
    return True


def _wbs_ok(profile: Profile, flat: Dict[str, Any]) -> bool:
    requires_wbs = listing_requires_wbs(flat)
    if profile.wbs_required_ok is False and requires_wbs:
        return False

    allowed = {str(item) for item in profile.wbs_categories_ok}
    if not allowed:
        return True

    text = listing_corpus(flat)
    if _NO_WBS_RE.search(text):
        return True

    cats = set(extract_wbs_categories(flat))
    nur = set(_NUR_WBS_RE.findall(text))
    besonderer = bool(re.search(r"besonderer?m?\s*wohnbedarf", text, re.IGNORECASE))

    # If "nur mit WBS 220"
    if nur and not (nur & allowed):
        return False

    # If besonderer Wohnbedarf required but category 220 not allowed
    if besonderer and "220" not in allowed:
        return False

    # If specific WBS categories are listed and none are allowed
    if cats and not (cats & allowed):
        return False

    return True


def _rent_ok(profile: Profile, flat: Dict[str, Any]) -> bool:
    caps = [cap for cap in (profile.warm_rent_max, profile.jobcenter_limit_warm) if cap is not None]
    warm_cap = min(caps) if caps else None
    cold_cap = profile.cold_rent_max
    if warm_cap is None and cold_cap is None:
        return True

    warm = flat.get("warmmiete")
    cold = flat.get("kaltmiete")
    checked = False

    if warm_cap is not None and warm is not None:
        checked = True
        if warm > warm_cap:
            return False

    if cold_cap is not None and cold is not None:
        checked = True
        if cold > cold_cap:
            return False

    if not checked:
        if warm_cap is not None and cold is not None and cold <= warm_cap:
            return True
        return False

    return True


def matches_profile(
    flat: Dict[str, Any],
    profile: Profile,
    **kwargs: Any,
) -> Tuple[bool, str]:
    """Check if listing matches profile constraints."""
    if not profile.allow_senior and is_senior_housing(flat):
        return False, "senior"
    if not profile.allow_student and is_student_housing(flat):
        return False, "student"
    keyword = _keyword_hit(flat, profile.exclude_keywords)
    if keyword:
        return False, f"keyword:{keyword}"
    if not _source_ok(profile, flat):
        return False, "source"
    if not _district_ok(profile, flat):
        return False, "district"

    rooms = flat.get("rooms")
    if rooms is not None:
        if profile.rooms_min is not None and rooms < profile.rooms_min:
            return False, "rooms"
        if profile.rooms_max is not None and rooms > profile.rooms_max:
            return False, "rooms"

    area = flat.get("area")
    if area is not None:
        if profile.size_min_m2 is not None and area < profile.size_min_m2:
            return False, "size"
        if profile.size_max_m2 is not None and area > profile.size_max_m2:
            return False, "size"

    if not _wbs_ok(profile, flat):
        return False, "wbs"

    if not _rent_ok(profile, flat):
        return False, "rent"

    return True, "ok"


def cover_letter(flat: Dict[str, Any], profile: Profile) -> str:
    address = flat.get("address") or "Berlin"
    obj_id = flat.get("object_id") or flat.get("flat_id") or ""
    obj_bit = f" ({obj_id})" if obj_id else ""
    lines = [
        "Sehr geehrte Damen und Herren,",
        "",
        f"ich interessiere mich für Ihr Wohnungsangebot{obj_bit} in {address}.",
    ]
    if profile.household_size:
        count = int(profile.household_size)
        word = "Person" if count == 1 else "Personen"
        lines.append(f"Unser Haushalt besteht aus {count} {word}.")
    if profile.wbs_required_ok:
        if profile.wbs_categories_ok:
            cats = "/".join(profile.wbs_categories_ok)
            lines.append(f"Einen Wohnberechtigungsschein (WBS {cats}) kann ich vorlegen.")
        else:
            lines.append("Einen gültigen Wohnberechtigungsschein (WBS) kann ich vorlegen.")
    if profile.jobcenter_limit_warm:
        lines.append("Die Kostenübernahme durch das Jobcenter ist möglich.")
    lines.extend([
        "Über eine Einladung zur Besichtigung würde ich mich freuen.",
        "",
        "Mit freundlichen Grüßen",
    ])
    if profile.display_name:
        lines.append(profile.display_name)
    return "\n".join(lines)
