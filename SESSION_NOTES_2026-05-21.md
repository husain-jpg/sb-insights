# Session Notes — 2026-05-21

## CRITICAL FINDING — GTIN re-key won't fix the orphan problem

When OCS refreshes their catalog, they don't just renumber existing products —
they **re-list products as entirely new entries** with new variant numbers **AND**
new GTINs. Cova mirrors this with new Catalog SKUs tagged `*New*`. All identifiers
move together.

**Result:** a GTIN-based join would resolve only **20 of 6,608 orphans (0.3%)**.

### Evidence

- 3,122 products where Cova `Vendor SKU` matches OCS variant exactly.
- 3,097 (99.2%) have **IDENTICAL** UPC == GTIN.
- Conclusion: UPC and GTIN **are the same field** — but the orphans' UPCs are
  themselves dead (point at delisted listings).

### Tracing the 5 known orphans

- **H600VNKR** (OG Cola FREE 355ml) → old listing fully gone; exists in Cova as
  new SKU `5UX4V6N3` with a new GTIN.
- Same pattern for **86Q8E5DP**, **ZK0FTJJ8**, **6M8HG1YF** (each has a `*New*`
  successor in Cova under a new SKU + new GTIN).

The real problem is **product re-identity, not join-key instability.**

### Fix options being evaluated

- **A)** Manual re-mapping (weekly UI task for manager).
- **B)** Automated successor detection by brand + name + size.
- **C)** Accept the gap; treat re-listed products as new (loses velocity history).
- **D)** Hybrid: keep existing join + add `predecessor_sku` field +
  semi-automated successor detection with manager review.

**Recommendation:** Option D — but it's 2–3 sessions of work and **not blocking
cloud migration.**
