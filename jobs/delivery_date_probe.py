"""
Delivery-date watcher — detect when a store's REAL OCS order form (the one that
carries committed delivery dates) is available, and confirm whether fetching it
with exportOrderTemplate=false actually yields populated dates.

WHY: the connector's normal pull uses `exportOrderTemplate=true` — the always-
available BLANK template, which never carries committed delivery dates. The
committed dates only exist in a store's real order form, which appears during
that store's order window (~7pm the night before its order day until ~12:30pm
the next day). Stores are staggered across the week, so we don't know offhand
when any given store is in-window. This probe runs on a schedule, fetches each
mapped store's order form with BOTH exportOrderTemplate=false (real form) and
=true (template), and records whether each carries a populated
'Estimated Delivery Date' column.

When any store reports real-form dates, the hypothesis is confirmed and we can
wire fetch_order_fill to use exportOrderTemplate=false in-window + add the
anti-clobber import logic. Until then this only READS the portal — it never
imports or writes to the DB (so it can't clobber connector data).

Outputs (under logs/):
  - delivery_date_probe.json   latest full result (overwritten each run)
  - delivery_date_probe.log    one summary line appended per run
  - DELIVERY_DATES_FOUND.flag  written the first time real dates are seen

Run on the DROPLET (creds only decrypt under the prod SECRET_KEY there) via
deploy/run_delivery_probe.sh on a cron a couple of times a day.
"""
from __future__ import annotations

import io
import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from jobs.ocs_connector import (
    load_account, _make_session, login, _store_tokens, store_mappings,
    select_store, _filename_from_response, _EXCEL_MAGICS,
)

log = logging.getLogger(__name__)

LOG_DIR = Path("logs")
EDD_COL = "Estimated Delivery Date"


def _fetch_order_form(session, account, *, pack_type: int = 1, template: bool):
    """Fetch the OrderExport with an explicit exportOrderTemplate flag.

    Returns (bytes, filename) or None if no real spreadsheet came back (e.g. the
    real form isn't available outside the store's order window — the portal then
    returns HTML/JSON instead of an xlsx)."""
    base = account.base_url.rstrip("/")
    resp = session.get(
        f"{base}/Sales/GenerateOrderExportFile",
        params={"packType": pack_type,
                "exportOrderTemplate": "true" if template else "false"},
        timeout=120, allow_redirects=True,
    )
    if not resp.ok or not resp.content or not resp.content.startswith(_EXCEL_MAGICS):
        return None
    return resp.content, _filename_from_response(resp, "OrderExport.xlsx")


def _date_count(content: bytes):
    """(#non-null Estimated Delivery Date, total rows) for an order xlsx.
    Returns (None, None) if it can't be read, (0, rows) if the column is absent."""
    try:
        df = pd.read_excel(io.BytesIO(content), sheet_name="MasterCatalogue")
    except Exception:
        return None, None
    if EDD_COL not in df.columns:
        return 0, len(df)
    return int(df[EDD_COL].notna().sum()), len(df)


def run_probe(conn) -> dict:
    """Log in once, probe every mapped store's real form + template, summarize."""
    account = load_account(conn)
    if account is None:
        return {"status": "skipped", "reason": "no OCS account configured"}

    session = _make_session()
    login(session, account)
    tokens = _store_tokens(session, account)

    stores: list[dict] = []
    for store_number, location_id in store_mappings(conn):
        rec = {"store_number": store_number, "location_id": location_id}
        token = tokens.get(str(store_number))
        if not token:
            rec.update(available=False, reason="not present on portal")
            stores.append(rec)
            continue
        select_store(session, account, token)
        real = _fetch_order_form(session, account, template=False)
        tmpl = _fetch_order_form(session, account, template=True)
        real_dates, real_rows = _date_count(real[0]) if real else (None, None)
        tmpl_dates, tmpl_rows = _date_count(tmpl[0]) if tmpl else (None, None)
        rec.update(
            real_form_available=bool(real),
            real_form_dates=real_dates, real_form_rows=real_rows,
            real_form_file=real[1] if real else None,
            template_available=bool(tmpl),
            template_dates=tmpl_dates, template_rows=tmpl_rows,
            # The signal we're after: the real form is back AND it has dates.
            in_window_with_dates=bool(real) and (real_dates or 0) > 0,
        )
        stores.append(rec)

    hits = [s["location_id"] for s in stores if s.get("in_window_with_dates")]
    return {"status": "ok", "stores": stores, "in_window_with_dates": hits}


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    db_path = os.environ.get("TERROIR_DB", "terroir.db")
    conn = sqlite3.connect(db_path, timeout=60)
    try:
        result = run_probe(conn)
    except Exception as e:  # noqa: BLE001 — record then carry on; cron-friendly
        result = {"status": "error", "error": str(e)}
    finally:
        conn.close()

    result["ts"] = datetime.now(timezone.utc).isoformat()
    LOG_DIR.mkdir(exist_ok=True)
    (LOG_DIR / "delivery_date_probe.json").write_text(json.dumps(result, indent=2))

    hits = result.get("in_window_with_dates") or []
    with open(LOG_DIR / "delivery_date_probe.log", "a") as f:
        f.write(f"{result['ts']} status={result.get('status')} "
                f"hits={hits} err={result.get('error', '')}\n")

    # Persistent, easy-to-spot marker the first time we actually see dates.
    if hits:
        (LOG_DIR / "DELIVERY_DATES_FOUND.flag").write_text(
            f"{result['ts']}\nStores with real-form delivery dates: {hits}\n"
            f"See logs/delivery_date_probe.json for the full comparison.\n")

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
