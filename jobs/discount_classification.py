"""Classify discount_lines.discount_reason values as customer-facing or internal.

Cova's "Sales by Location" report only nets certain discount types out of
Gross to arrive at Subtotal. Internal cost adjustments (employee purchases,
LP rep cost-pricing, staff cards) don't reduce reported gross — they're
treated as cost-of-goods adjustments.

This module classifies our discount_lines.discount_reason values to match
Cova's behavior so:
    GROSS SALES = sales_daily.gross_revenue (Subtotal) + customer-facing discounts
    NET SALES   = sales_daily.gross_revenue (Subtotal — already post-discount)
    INTERNAL    = employee/staff/LP rep adjustments (memo only)

Verified against May 9-Jun 5 2026 data: Bradford matches Cova within $5;
chain-wide total within 2%. Per-store match is approximate due to remaining
classification ambiguities (Aged Inventory, Customer Resolution, etc.) that
Cova may treat differently per store config.
"""
from __future__ import annotations

# Substrings (lowercase, case-insensitive match) that mark a discount reason
# as INTERNAL (not customer-facing, not deducted from Cova "Gross Sales").
_INTERNAL_REASON_PATTERNS = (
    "employee",       # Employee Purchase 30% OFF, Employee Weekly Purchase 50% OFF, McNeil Employees, Rock 95 Employees
    "lp rep",         # ON - LP Reps 15% OFF
    " sc ",           # ON - SC 5%, ON - SC 15% (staff cards)
)


def is_internal_discount(reason: str | None) -> bool:
    """True if this discount reason represents an internal cost adjustment
    (employee / LP rep / staff card), not a customer-facing markdown.

    These are EXCLUDED from "Gross Sales" calculation because Cova does
    the same in its "Sales by Location" report.
    """
    if not reason:
        return False
    r = reason.lower()
    return any(p in r for p in _INTERNAL_REASON_PATTERNS)


def classify_discount(reason: str | None) -> str:
    """Return 'internal' or 'customer'. Useful for SQL CASE expressions
    via the resulting bucket name."""
    return "internal" if is_internal_discount(reason) else "customer"


# SQL fragment for use in WHERE / CASE clauses to identify internal discounts.
# Keep in sync with the Python helper above; tested cases:
#   'ON - Employee Weekly Purchase - 50% OFF' -> internal
#   'ALL - Employee Purchase 30% OFF'         -> internal
#   'ON - LP Reps 15% OFF'                    -> internal
#   'ON - McNeil Employees - 10% OFF'         -> internal (substring 'employee')
#   'ON - SC 5%' / 'ON - SC 15%'              -> internal (substring ' sc ')
#   'ON - Bud Club 5% OFF'                    -> customer
#   'ALL - Senior 10% OFF'                    -> customer
#   'ALL - Customer Resolution'               -> customer  (note: see module docstring re: ambiguity)
INTERNAL_DISCOUNT_SQL = (
    "(LOWER(COALESCE(discount_reason,'')) LIKE '%employee%' "
    " OR LOWER(COALESCE(discount_reason,'')) LIKE '%lp rep%' "
    " OR LOWER(COALESCE(discount_reason,'')) LIKE '% sc %')"
)
