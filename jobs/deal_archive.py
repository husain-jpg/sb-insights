"""
Append-only archive of collective (data-revenue) deals.

Buysheet imports replace deals with a delete-then-insert. Before the DELETE, the
outgoing rows are copied into ``data_revenue_deals_archive`` so no historical
deal set is ever lost — handy for later rebate / coverage analysis, and for
recovering a superseded month.

Called by the IRCC / Seeker / Canna Collective importers immediately before their
DELETE. Best-effort: archiving must never block or fail an import.
"""
from __future__ import annotations


def archive_deals(conn, where_clause: str, params, source_file: str | None) -> int:
    """Copy the ``data_revenue_deals`` rows matching ``where_clause`` into
    ``data_revenue_deals_archive`` before they're deleted/replaced.

    ``where_clause`` must reference the deals table as alias ``d`` (e.g.
    ``"d.brand_id = ?"``). ``params`` are the placeholders for that clause.
    ``source_file`` is the buysheet that triggered the replacement (stored as
    ``replaced_by_file``). Returns the number of rows archived; never raises.
    """
    try:
        cur = conn.cursor()
        cur.execute(
            f"""
            INSERT INTO data_revenue_deals_archive
                (orig_id, brand_id, partner_name, start_date, end_date, percentage,
                 basis, sku_filter, notes, deal_created_at, replaced_by_file)
            SELECT d.id, d.brand_id, b.brand_name, d.start_date, d.end_date,
                   d.percentage, d.basis, d.sku_filter, d.notes, d.created_at, ?
            FROM data_revenue_deals d
            LEFT JOIN brand_partners b ON b.id = d.brand_id
            WHERE {where_clause}
            """,
            [source_file] + list(params),
        )
        return cur.rowcount or 0
    except Exception:
        # Archiving is best-effort — a failure here (e.g. table missing on a very
        # old DB) must not abort the import that the user actually cares about.
        return 0
