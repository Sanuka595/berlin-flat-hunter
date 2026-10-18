"""Auto-Apply module for WBM (Wohnungsbaugesellschaft Berlin-Mitte mbH).

Parses Powermail form on WBM apartment detail page and submits inquiry.
"""

from __future__ import annotations

import html
import logging
import re
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urljoin

import httpx

SUCCESS_MARKERS = [
    "vielen dank",
    "ihre anfrage wurde versendet",
    "wurde erfolgreich versendet",
    "anfrage versendet",
    "erfolgreich übermittelt",
    "erfolgreich uebermittelt",
    "wir haben ihre anfrage erhalten",
]


def extract_powermail_form(html_content: str, base_url: str) -> Optional[Dict[str, Any]]:
    """Extract form action and hidden/default fields from WBM Powermail form."""
    form_match = re.search(
        r'<form[^>]*class="[^"]*powermail_form[^"]*"[^>]*action="([^"]+)"[^>]*>(.*?)</form>',
        html_content,
        re.DOTALL | re.IGNORECASE,
    )
    if not form_match:
        # Fallback if class attribute is arranged differently
        form_match = re.search(
            r'<form[^>]*action="([^"]+)"[^>]*class="[^"]*powermail_form[^"]*"[^>]*>(.*?)</form>',
            html_content,
            re.DOTALL | re.IGNORECASE,
        )
    if not form_match:
        return None

    raw_action = html.unescape(form_match.group(1))
    form_html = form_match.group(2)
    action_url = urljoin(base_url, raw_action)

    fields: Dict[str, Any] = {}

    # Extract hidden and text inputs
    input_matches = re.findall(
        r'<input([^>]+)/?>',
        form_html,
        re.DOTALL | re.IGNORECASE,
    )
    for tag in input_matches:
        name_m = re.search(r'name="([^"]+)"', tag)
        val_m = re.search(r'value="([^"]*)"', tag)
        type_m = re.search(r'type="([^"]+)"', tag)
        if not name_m:
            continue
        name = html.unescape(name_m.group(1))
        val = html.unescape(val_m.group(1)) if val_m else ""
        inp_type = type_m.group(1).lower() if type_m else "text"

        if inp_type in ("hidden", "text", "date"):
            fields[name] = val

    return {
        "action": action_url,
        "fields": fields,
        "form_html": form_html,
    }


def prepare_wbm_form_data(
    extracted_form: Dict[str, Any],
    applicant: Dict[str, Any],
) -> Dict[str, Any]:
    """Combine extracted hidden fields with applicant profile details."""
    data: Dict[str, Any] = dict(extracted_form.get("fields", {}))

    # Clean honeypot field
    for k in list(data.keys()):
        if "__hp" in k:
            data[k] = ""

    # Applicant details mapping
    anrede = applicant.get("anrede") or "Frau"
    name = applicant.get("name") or applicant.get("last_name") or ""
    vorname = applicant.get("vorname") or applicant.get("first_name") or ""
    strasse = applicant.get("strasse") or applicant.get("street") or ""
    plz = str(applicant.get("plz") or applicant.get("zip_code") or "")
    ort = applicant.get("ort") or applicant.get("city") or "Berlin"
    email = applicant.get("email") or applicant.get("e_mail") or ""
    telefon = str(applicant.get("telefon") or applicant.get("phone") or "")

    data["tx_powermail_pi1[field][anrede]"] = anrede
    data["tx_powermail_pi1[field][name]"] = name
    data["tx_powermail_pi1[field][vorname]"] = vorname
    data["tx_powermail_pi1[field][strasse]"] = strasse
    data["tx_powermail_pi1[field][plz]"] = plz
    data["tx_powermail_pi1[field][ort]"] = ort
    data["tx_powermail_pi1[field][e_mail]"] = email
    data["tx_powermail_pi1[field][telefon]"] = telefon

    # WBS fields
    has_wbs = applicant.get("has_wbs", True)
    data["tx_powermail_pi1[field][wbsvorhanden]"] = "1" if has_wbs else "0"
    if has_wbs:
        if applicant.get("wbs_valid_until"):
            data["tx_powermail_pi1[field][wbsgueltigbis]"] = str(applicant["wbs_valid_until"])
        if applicant.get("wbs_rooms"):
            data["tx_powermail_pi1[field][wbszimmeranzahl]"] = str(applicant["wbs_rooms"])
        if applicant.get("wbs_income_level"):
            data["tx_powermail_pi1[field][einkommensgrenzenacheinkommensbescheinigung9]"] = str(
                applicant["wbs_income_level"]
            )
        if applicant.get("wbs_special_need"):
            data["tx_powermail_pi1[field][wbsmitbesonderemwohnbedarf][]"] = "1"

    # Mandatory GDPR consent checkbox
    data["tx_powermail_pi1[field][datenschutzhinweis][]"] = "1"

    return data


def verify_wbm_response(resp: httpx.Response) -> Tuple[bool, str]:
    """Check if the submission was successful."""
    # Check redirect to success or confirmation page
    if resp.status_code in (301, 302, 303, 307):
        target = resp.headers.get("location", "")
        if "erfolg" in target.lower() or "danke" in target.lower():
            return True, f"Redirect to success page: {target}"
        return True, f"Redirected to {target}"

    if resp.status_code != 200:
        return False, f"HTTP error {resp.status_code}"

    text = resp.text.lower()
    for marker in SUCCESS_MARKERS:
        if marker in text:
            return True, f"Found success marker '{marker}' in response"

    if "powermail_field_error" in text:
        return False, "Validation error in Powermail response"

    return False, "Response did not contain known success marker"


async def apply_wbm(
    client: httpx.AsyncClient,
    flat_url: str,
    applicant: Dict[str, Any],
    dry_run: bool = False,
) -> Tuple[bool, str, Dict[str, Any]]:
    """Execute Auto-Apply for WBM apartment."""
    if not flat_url:
        return False, "No flat URL provided", {}

    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:132.0) Gecko/20100101 Firefox/132.0",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7",
    }

    try:
        page_resp = await client.get(flat_url, headers=headers, timeout=15.0, follow_redirects=True)
    except Exception as exc:
        return False, f"Failed to fetch detail page: {exc}", {}

    if page_resp.status_code != 200:
        return False, f"Detail page returned HTTP {page_resp.status_code}", {}

    extracted = extract_powermail_form(page_resp.text, str(page_resp.url))
    if not extracted:
        return False, "Powermail contact form not found on WBM page", {}

    submit_url = extracted["action"]
    form_data = prepare_wbm_form_data(extracted, applicant)

    # Safe summary for logging (mask email/phone)
    safe_data = {
        "action": submit_url,
        "name": f"{applicant.get('vorname', '')} {applicant.get('name', '')}".strip(),
        "email": applicant.get("email", ""),
        "phone": applicant.get("telefon", ""),
        "wbs": form_data.get("tx_powermail_pi1[field][wbsvorhanden]"),
    }

    if dry_run:
        logging.info("[DRY RUN] Auto-Apply prepared for %s to %s", flat_url, submit_url)
        return True, "DRY_RUN_OK", form_data

    # Real POST submission
    headers["Referer"] = flat_url
    headers["Origin"] = "https://www.wbm.de"
    try:
        post_resp = await client.post(
            submit_url,
            data=form_data,
            headers=headers,
            timeout=20.0,
            follow_redirects=True,
        )
    except Exception as exc:
        return False, f"POST submission failed: {exc}", form_data

    success, reason = verify_wbm_response(post_resp)
    return success, reason, form_data
