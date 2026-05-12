"""
One-time seeder for licensed_producers + lp_contacts tables.

Strategy:
1. Pull the canonical LP list from ocs_catalog.supplier (the universe of
   real LPs you might do business with via OCS).
2. Parse the user's MasterList_LPs.xlsx for contacts + agreement terms.
3. Fuzzy-match master list LPs to OCS suppliers; populate matches.
4. For OCS LPs without master-list match: create empty rows so the user
   can see them.
5. For master-list LPs without OCS match: create rows flagged as
   "not in current OCS catalog" — they may be defunct or specialty.

Idempotent: re-running merges based on `name`. Existing rows are NOT
overwritten (we only fill in NULL fields), so the user's hand-edits
are preserved across re-seeds.

Usage:
    from jobs.seed_lps import seed_lps_from_master_list
    result = seed_lps_from_master_list(conn, master_list_path=None)
    # If master_list_path=None, defaults to bundled file in jobs/seed_data/
"""
from __future__ import annotations

import logging
import re
import sqlite3
from pathlib import Path
from typing import Optional

import pandas as pd

log = logging.getLogger(__name__)

DEFAULT_MASTER_PATH = Path(__file__).parent / "seed_data" / "MasterList_LPs.xlsx"

# Sheets in the master list that aren't LPs themselves
NON_LP_SHEETS = {
    "Summary", "Promos", "Monthly Tracker", "Sheet15",
    "LP Template", "LP Template (2)", "LTO Template",
}

# LP-specific sheets — but the LTO sheets are siblings of LPs, not LPs themselves
LTO_SHEET_PATTERN = re.compile(r"[-_\s]?(LTO|MonthlyListing)$", re.IGNORECASE)


def _norm_name(s: str) -> str:
    """Normalize an LP name for fuzzy matching: lowercase, strip punctuation,
    drop common suffixes like 'Inc.', 'Ltd', 'Cannabis Group'."""
    if not isinstance(s, str):
        return ""
    s = s.lower().strip()
    # Drop common corporate suffixes that vary between sources
    drops = [
        " cannabis group", " cannabis co.", " cannabis", " group inc",
        " inc.", " inc", " ltd.", " ltd", " corp.", " corp",
        " limited", " holdings", " brands", " co.", " ulc",
    ]
    for d in drops:
        if s.endswith(d):
            s = s[: -len(d)]
    # Replace punctuation with spaces, collapse whitespace
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    return s


def _is_lto_sheet(name: str) -> bool:
    return bool(LTO_SHEET_PATTERN.search(name))


def _parse_lp_sheet(xl: pd.ExcelFile, sheet_name: str) -> dict:
    """Parse an LP detail sheet from the master list. Sheets follow loose
    convention: column 1 has labels ('Name', 'Email', 'Number', 'Terms',
    'Contact', etc.) and column 2 has values. Multiple contacts appear in
    repeating Name/Email/Number blocks.

    Sheets often have non-contact sections too (Billing, Brands, Notes,
    Cadence, etc.) which can have their own 'Email' or 'Phone' fields —
    we must NOT apply those to the current contact. We track whether
    we're "in" the contact section vs. elsewhere.

    Returns:
        {
            'contacts': [{'name': ..., 'email': ..., 'phone': ..., 'notes': ...}, ...],
            'terms_summary': str | None,
            'notes': str | None,
        }
    """
    try:
        df = pd.read_excel(xl, sheet_name=sheet_name, header=None)
    except Exception:
        return {"contacts": [], "terms_summary": None, "notes": None}

    contacts: list[dict] = []
    current_contact: Optional[dict] = None
    terms_summary: Optional[str] = None
    free_notes: list[str] = []
    in_contact_section = False  # True only inside a Contact block

    # Labels that signal "this row belongs to a non-contact section,
    # commit any current contact and stop applying contact-field updates"
    SECTION_BREAKERS = {
        "billing", "brands", "notes", "cadence", "address", "company",
        "attn", "terms",  # Terms ends the contact section in most sheets
    }

    def _commit_contact():
        nonlocal current_contact, in_contact_section
        if current_contact and any(current_contact.values()):
            contacts.append(current_contact)
        current_contact = None
        in_contact_section = False

    for idx in range(len(df)):
        row = df.iloc[idx]
        label = str(row.iloc[1]).strip() if len(row) > 1 and pd.notna(row.iloc[1]) else ""
        value = str(row.iloc[2]).strip() if len(row) > 2 and pd.notna(row.iloc[2]) else ""
        label_lower = label.lower()

        # Detect a non-contact section break. This commits the current contact
        # and pulls us out of contact-section mode. (The "Terms" row goes to
        # terms_summary, but we still want to commit any pending contact.)
        if label_lower in SECTION_BREAKERS:
            _commit_contact()
            if label_lower == "terms" and value:
                terms_summary = value
            elif label_lower == "notes" and value:
                free_notes.append(value)
            continue

        if label_lower == "contact":
            _commit_contact()
            current_contact = {"name": None, "email": None, "phone": None, "notes": None}
            in_contact_section = True
            continue

        # Only apply contact-field updates when in the contact section
        if not in_contact_section:
            # Auto-enter contact section if we see "Name" without explicit "Contact" header
            # (some sheets like Avicanna omit the marker between contacts)
            if label_lower == "name" and value:
                _commit_contact()  # commit anything pending
                current_contact = {"name": None, "email": None, "phone": None, "notes": None}
                in_contact_section = True
            else:
                continue

        # We're in the contact section — apply field updates
        if label_lower == "name" and value:
            # New "Name" row inside contact section means new contact
            if current_contact and current_contact.get("name"):
                _commit_contact()
                current_contact = {"name": None, "email": None, "phone": None, "notes": None}
                in_contact_section = True
            if current_contact is None:
                current_contact = {"name": None, "email": None, "phone": None, "notes": None}
            current_contact["name"] = value
        elif label_lower == "email" and value:
            if current_contact is None:
                current_contact = {"name": None, "email": None, "phone": None, "notes": None}
            current_contact["email"] = value
        elif label_lower in ("number", "phone") and value:
            if current_contact is None:
                current_contact = {"name": None, "email": None, "phone": None, "notes": None}
            current_contact["phone"] = value
        elif value and not label and current_contact is not None:
            # Stray notes attached to current contact (e.g. "Dog's name is Dolce")
            existing = current_contact.get("notes")
            current_contact["notes"] = (existing + " | " + value) if existing else value

    _commit_contact()

    return {
        "contacts": contacts,
        "terms_summary": terms_summary,
        "notes": " | ".join(free_notes) if free_notes else None,
    }


def _parse_summary_statuses(xl: pd.ExcelFile) -> dict[str, dict]:
    """Parse the Summary sheet for per-LP status booleans.

    Summary sheet layout (header row 1, data rows 2+):
        Sent | Agreement | LP Name | Data | Agreement | Report | Invoice |
        Terms | LTO | Offer | Applicable SKUs/Brands | Report | Invoice | Terms

    Mapping:
        col 1: agreement_sent
        col 2: agreement_signed
        col 3: lp name
        col 4: data_sent
        col 5: terms_summary
        col 6: report_sent
        col 7: invoice_sent
        col 8: payment_terms
        col 9: lto_active
        col 10: lto_offer
        col 11: lto_applicable_skus
        col 12: lto_report_sent
        col 13: lto_invoice_sent
        col 14: lto_payment_terms
    """
    try:
        df = pd.read_excel(xl, sheet_name="Summary", header=None)
    except Exception:
        return {}

    statuses: dict[str, dict] = {}

    def _bool(v) -> int:
        if pd.isna(v):
            return 0
        s = str(v).strip().lower()
        if s in ("true", "yes", "1", "y", "auto"):
            return 1
        return 0

    for idx in range(2, len(df)):
        row = df.iloc[idx]
        lp_name = row.iloc[3] if len(row) > 3 else None
        if pd.isna(lp_name) or not str(lp_name).strip():
            continue
        name = str(lp_name).strip()
        statuses[name] = {
            "agreement_sent": _bool(row.iloc[1] if len(row) > 1 else None),
            "agreement_signed": _bool(row.iloc[2] if len(row) > 2 else None),
            "data_sent": _bool(row.iloc[4] if len(row) > 4 else None),
            "terms_summary": (str(row.iloc[5]).strip() if len(row) > 5 and pd.notna(row.iloc[5])
                              else None),
            "report_sent": _bool(row.iloc[6] if len(row) > 6 else None),
            "invoice_sent": _bool(row.iloc[7] if len(row) > 7 else None),
            "payment_terms": (str(row.iloc[8]).strip() if len(row) > 8 and pd.notna(row.iloc[8])
                              else None),
            "lto_active": _bool(row.iloc[9] if len(row) > 9 else None),
            "lto_offer": (str(row.iloc[10]).strip() if len(row) > 10 and pd.notna(row.iloc[10])
                          else None),
            "lto_applicable_skus": (str(row.iloc[11]).strip() if len(row) > 11 and pd.notna(row.iloc[11])
                                    else None),
            "lto_report_sent": _bool(row.iloc[12] if len(row) > 12 else None),
            "lto_invoice_sent": _bool(row.iloc[13] if len(row) > 13 else None),
            "lto_payment_terms": (str(row.iloc[14]).strip() if len(row) > 14 and pd.notna(row.iloc[14])
                                  else None),
        }
        # "-" is used in the sheet to mean N/A or empty
        for k, v in list(statuses[name].items()):
            if v == "-":
                statuses[name][k] = None
    return statuses


def _fuzzy_match(target: str, candidates: list[str]) -> Optional[str]:
    """Find best matching candidate by normalized substring or token overlap."""
    target_n = _norm_name(target)
    if not target_n:
        return None
    # Exact normalized match
    for c in candidates:
        if _norm_name(c) == target_n:
            return c
    # Substring (target is contained in candidate or vice versa)
    for c in candidates:
        cn = _norm_name(c)
        if cn and (target_n in cn or cn in target_n):
            # Avoid spurious 1-char matches
            if min(len(target_n), len(cn)) >= 3:
                return c
    return None


def seed_lps_from_master_list(
    conn: sqlite3.Connection,
    master_list_path: Optional[Path] = None,
) -> dict:
    """Seed licensed_producers + lp_contacts from OCS catalog and master list.

    Idempotent: existing LP rows are not overwritten unless their fields are
    NULL. Existing contacts are never duplicated (we check name+email).
    """
    if master_list_path is None:
        master_list_path = DEFAULT_MASTER_PATH
    if not master_list_path.exists():
        raise FileNotFoundError(f"Master list not found: {master_list_path}")

    cur = conn.cursor()

    # 1. Pull distinct suppliers from OCS catalog
    cur.execute("""
        SELECT DISTINCT supplier
        FROM ocs_catalog
        WHERE supplier IS NOT NULL AND supplier != ''
        ORDER BY supplier
    """)
    ocs_suppliers = [r[0] for r in cur.fetchall()]
    log.info("OCS catalog has %d distinct suppliers", len(ocs_suppliers))

    # 2. Parse the master list
    xl = pd.ExcelFile(master_list_path)
    summary_statuses = _parse_summary_statuses(xl)
    log.info("Master list Summary sheet has status for %d LPs", len(summary_statuses))

    # 3. Build the per-LP detail by walking each non-template, non-LTO sheet
    master_lps: dict[str, dict] = {}  # name (as in sheet) -> parsed details
    for sheet_name in xl.sheet_names:
        if sheet_name in NON_LP_SHEETS:
            continue
        if _is_lto_sheet(sheet_name):
            continue
        # The sheet name is the LP name (e.g. "Auxly", "Avicanna")
        # Some sheets have weird names like "CanadaIslandGarden" — keep as-is
        details = _parse_lp_sheet(xl, sheet_name)
        if details["contacts"] or details["terms_summary"]:
            master_lps[sheet_name] = details

    log.info("Master list has detail sheets for %d LPs", len(master_lps))

    # 4. Build the union of LP names from both sources
    # For each OCS supplier, find best match in master list
    matched: dict[str, str] = {}  # ocs_name -> master_sheet_name
    used_sheets: set = set()
    for ocs_name in ocs_suppliers:
        # Try summary tab first (canonical names like "Auxly", "Avicanna")
        candidates = list(summary_statuses.keys()) + list(master_lps.keys())
        match = _fuzzy_match(ocs_name, candidates)
        if match and match not in used_sheets:
            matched[ocs_name] = match
            used_sheets.add(match)

    unmatched_master = [s for s in master_lps if s not in used_sheets and s not in summary_statuses]
    # Also include summary-only LPs not matched to OCS
    unmatched_summary = [s for s in summary_statuses if s not in used_sheets]

    inserts = 0
    updates = 0
    contacts_added = 0

    def _resolve_canonical_name(ocs_name: Optional[str], master_name: Optional[str]) -> str:
        # Prefer the master-list name (cleaner) if available
        return master_name or ocs_name or ""

    def _upsert_lp(canonical_name: str, ocs_name: Optional[str],
                   master_name: Optional[str], details: dict,
                   summary_status: dict) -> Optional[int]:
        """Insert or merge an LP row. Returns lp_id."""
        nonlocal inserts, updates

        # Check if an LP with this name already exists
        cur.execute("SELECT id FROM licensed_producers WHERE name = ?", (canonical_name,))
        row = cur.fetchone()

        if row is None:
            # Insert
            cur.execute("""
                INSERT INTO licensed_producers (
                    name, name_in_ocs, name_in_master_list,
                    agreement_sent, agreement_signed, data_sent, report_sent, invoice_sent,
                    terms_summary, payment_terms,
                    lto_active, lto_offer, lto_applicable_skus,
                    lto_report_sent, lto_invoice_sent, lto_payment_terms,
                    is_active, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                canonical_name, ocs_name, master_name,
                summary_status.get("agreement_sent", 0),
                summary_status.get("agreement_signed", 0),
                summary_status.get("data_sent", 0),
                summary_status.get("report_sent", 0),
                summary_status.get("invoice_sent", 0),
                summary_status.get("terms_summary") or details.get("terms_summary"),
                summary_status.get("payment_terms"),
                summary_status.get("lto_active", 0),
                summary_status.get("lto_offer"),
                summary_status.get("lto_applicable_skus"),
                summary_status.get("lto_report_sent", 0),
                summary_status.get("lto_invoice_sent", 0),
                summary_status.get("lto_payment_terms"),
                1,  # is_active
                details.get("notes"),
            ))
            inserts += 1
            return cur.lastrowid
        else:
            lp_id = row[0]
            # Only fill NULL fields (preserve user edits)
            cur.execute("""
                UPDATE licensed_producers SET
                    name_in_ocs = COALESCE(name_in_ocs, ?),
                    name_in_master_list = COALESCE(name_in_master_list, ?),
                    terms_summary = COALESCE(terms_summary, ?),
                    payment_terms = COALESCE(payment_terms, ?),
                    notes = COALESCE(notes, ?),
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (
                ocs_name, master_name,
                summary_status.get("terms_summary") or details.get("terms_summary"),
                summary_status.get("payment_terms"),
                details.get("notes"),
                lp_id,
            ))
            updates += 1
            return lp_id

    def _add_contacts(lp_id: int, contacts: list[dict]):
        """Add contacts, deduping on (name, email)."""
        nonlocal contacts_added
        for i, ct in enumerate(contacts):
            name = ct.get("name")
            email = ct.get("email")
            if not (name or email):
                continue
            # Dedupe: skip if a contact with same name+email exists for this LP
            cur.execute("""
                SELECT id FROM lp_contacts
                WHERE lp_id = ? AND
                      COALESCE(name,'') = COALESCE(?,'') AND
                      COALESCE(email,'') = COALESCE(?,'')
            """, (lp_id, name, email))
            if cur.fetchone():
                continue
            cur.execute("""
                INSERT INTO lp_contacts (lp_id, name, email, phone, notes, is_primary, sort_order)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (lp_id, name, email, ct.get("phone"), ct.get("notes"),
                  1 if i == 0 else 0, i))
            contacts_added += 1

    # 5. Process each OCS supplier (matched to master or empty)
    for ocs_name in ocs_suppliers:
        master_match = matched.get(ocs_name)
        details = master_lps.get(master_match, {"contacts": [], "terms_summary": None, "notes": None})
        # Get summary status — match against summary keys directly first, then fuzzy
        status = summary_statuses.get(master_match) if master_match else None
        if not status and master_match:
            for k, v in summary_statuses.items():
                if _norm_name(k) == _norm_name(master_match):
                    status = v
                    break
        status = status or {}
        canonical = _resolve_canonical_name(ocs_name, master_match)
        lp_id = _upsert_lp(canonical, ocs_name, master_match, details, status)
        if lp_id and details.get("contacts"):
            _add_contacts(lp_id, details["contacts"])

    # 6. Process master-list-only LPs (not in OCS catalog)
    leftover_masters = unmatched_master + unmatched_summary
    seen = set()
    for sheet_name in leftover_masters:
        if sheet_name in seen:
            continue
        seen.add(sheet_name)
        details = master_lps.get(sheet_name, {"contacts": [], "terms_summary": None, "notes": None})
        status = summary_statuses.get(sheet_name, {})
        notes = details.get("notes") or ""
        notes_marker = "Not currently in OCS catalog"
        details["notes"] = (notes + " | " + notes_marker) if notes else notes_marker
        lp_id = _upsert_lp(sheet_name, None, sheet_name, details, status)
        if lp_id and details.get("contacts"):
            _add_contacts(lp_id, details["contacts"])

    conn.commit()

    # Final counts
    cur.execute("SELECT COUNT(*) FROM licensed_producers")
    total_lps = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM lp_contacts")
    total_contacts = cur.fetchone()[0]

    return {
        "ocs_suppliers": len(ocs_suppliers),
        "master_list_lps": len(master_lps) + len([k for k in summary_statuses if k not in master_lps]),
        "matched": len(matched),
        "ocs_only": len(ocs_suppliers) - len(matched),
        "master_only": len(leftover_masters),
        "rows_inserted": inserts,
        "rows_updated": updates,
        "contacts_added": contacts_added,
        "total_lps": total_lps,
        "total_contacts": total_contacts,
    }
