"""
SQLite schema for the manual-import phase.

Identical structure to the Postgres schema — only the syntax differs
(TEXT PRIMARY KEY, no JSONB, no NUMERIC precision, etc). When we
graduate to Postgres + Cova API, the Python queries don't change
because we use parameterized SQL and ANSI-compliant syntax.
"""

DDL = """
CREATE TABLE IF NOT EXISTS locations (
    id               TEXT PRIMARY KEY,
    cova_location_id TEXT UNIQUE NOT NULL,
    name             TEXT NOT NULL,
    city             TEXT,
    region           TEXT,
    is_active        INTEGER NOT NULL DEFAULT 1,
    created_at       TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS products (
    sku                  TEXT PRIMARY KEY,
    cova_catalog_item_id TEXT,
    ocs_variant_number   TEXT,
    name                 TEXT NOT NULL,
    brand                TEXT,
    category             TEXT,                -- leaf level, e.g. "Dried Flower"
    subcategory          TEXT,
    category_path        TEXT,                -- full path: "Cannabis > Flower > Dried Flower"
    top_level            TEXT,                -- "Cannabis" | "Accessories" | "Other"
    size                 TEXT,
    unit_of_measure      TEXT,
    thc                  REAL,
    cbd                  REAL,
    raw                  TEXT NOT NULL DEFAULT '{}',
    first_seen_at        TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_updated_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_products_category ON products (category);
CREATE INDEX IF NOT EXISTS ix_products_brand ON products (brand);
CREATE INDEX IF NOT EXISTS ix_products_ocs ON products (ocs_variant_number);
CREATE INDEX IF NOT EXISTS ix_products_top ON products (top_level);

CREATE TABLE IF NOT EXISTS ocs_catalog (
    ocs_variant_number   TEXT PRIMARY KEY,   -- e.g. 100074_7x0.5g___
    ocs_item_number      TEXT,                -- e.g. 100074
    gtin                 TEXT,
    product_name         TEXT NOT NULL,
    brand                TEXT,
    supplier             TEXT,
    category             TEXT,
    subcategory          TEXT,
    size                 TEXT,
    stock_status         TEXT,                -- 'YES' or 'NO'
    unit_price           REAL,                -- wholesale price per EACH unit
    pack_size            INTEGER,             -- eaches per master case
    thc_min              REAL,
    thc_max              REAL,
    cbd_min              REAL,
    cbd_max              REAL,
    as_of                TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_ocs_brand ON ocs_catalog (brand);
CREATE INDEX IF NOT EXISTS ix_ocs_category ON ocs_catalog (category);
CREATE INDEX IF NOT EXISTS ix_ocs_stock ON ocs_catalog (stock_status);

CREATE TABLE IF NOT EXISTS inventory_snapshots (
    sku                  TEXT NOT NULL,
    location_id          TEXT NOT NULL,
    on_hand              INTEGER NOT NULL,
    reserved             INTEGER NOT NULL DEFAULT 0,
    -- First/Last Received Date come straight from the Cova Inventory On Hand
    -- export. These are per-(sku, location) and update on each snapshot — they
    -- represent Cova's view of when the SKU was first/last received at this store.
    -- Far more reliable than deriving from imported invoice history.
    first_received_date  TEXT,
    last_received_date   TEXT,
    days_since_last_sold INTEGER,  -- also from Cova export, useful for promo decisions
    as_of                TEXT NOT NULL,
    PRIMARY KEY (sku, location_id, as_of)
);
CREATE INDEX IF NOT EXISTS ix_inv_loc_asof ON inventory_snapshots (location_id, as_of DESC);
CREATE INDEX IF NOT EXISTS ix_inv_sku_asof ON inventory_snapshots (sku, as_of DESC);

CREATE TABLE IF NOT EXISTS prices (
    sku           TEXT NOT NULL,
    location_id   TEXT NOT NULL,
    regular_price REAL NOT NULL,
    sale_price    REAL,
    currency      TEXT NOT NULL DEFAULT 'CAD',
    as_of         TEXT NOT NULL,
    PRIMARY KEY (sku, location_id)
);

CREATE TABLE IF NOT EXISTS sales_daily (
    sku           TEXT NOT NULL,
    location_id   TEXT NOT NULL,
    sale_date     TEXT NOT NULL,
    units_sold    INTEGER NOT NULL,
    gross_revenue REAL NOT NULL,
    PRIMARY KEY (sku, location_id, sale_date)
);
CREATE INDEX IF NOT EXISTS ix_sales_sku_date ON sales_daily (sku, sale_date DESC);
CREATE INDEX IF NOT EXISTS ix_sales_loc_date ON sales_daily (location_id, sale_date DESC);

-- Line-level transaction detail from Cova Itemized Sales export.
-- One row per line item per transaction. Enables analyses that aggregations
-- can't support: per-cashier discount patterns, basket composition, peak-hour
-- volume, customer-level repeat purchases, etc.
--
-- We keep BOTH this table and sales_daily because:
--   1. The reorder engine queries sales_daily heavily (fast SKU-level reads)
--   2. Most dashboard tabs only need aggregates
--   3. Line-level table is large (~2-3M rows for 2 years × 8 stores)
-- The importer populates both during the same pass over the CSV.
CREATE TABLE IF NOT EXISTS sale_lines (
    invoice_no       TEXT NOT NULL,
    line_no          INTEGER NOT NULL,         -- ordering within transaction
    sale_date        TEXT NOT NULL,
    sale_datetime    TEXT,                     -- full timestamp if available
    location_id      TEXT NOT NULL,
    sku              TEXT NOT NULL,
    units            REAL NOT NULL,            -- can be fractional for weighted items
    regular_price    REAL,                     -- per-unit list price
    sold_price       REAL,                     -- per-unit price actually charged
    discount_amount  REAL NOT NULL DEFAULT 0,  -- total $ off on this line
    subtotal         REAL,                     -- units × sold_price
    cashier          TEXT,                     -- "Tendered By" field
    created_by       TEXT,                     -- "Created By" — Dutchie Online vs in-store
    is_online        INTEGER NOT NULL DEFAULT 0,
    customer_name    TEXT,                     -- may be null for anonymous
    PRIMARY KEY (invoice_no, line_no, sku)
);
CREATE INDEX IF NOT EXISTS ix_lines_date ON sale_lines (sale_date);
CREATE INDEX IF NOT EXISTS ix_lines_loc_date ON sale_lines (location_id, sale_date);
CREATE INDEX IF NOT EXISTS ix_lines_sku ON sale_lines (sku);
CREATE INDEX IF NOT EXISTS ix_lines_cashier ON sale_lines (cashier, sale_date);
CREATE INDEX IF NOT EXISTS ix_lines_invoice ON sale_lines (invoice_no);

-- Cova Discounts report — one row per discount applied. Has reason codes
-- and authorizing employee that the Itemized Sales export doesn't include.
-- Joins back to sale_lines by (invoice_no, sku) when both exist.
--
-- Discount Type: 'Loyalty', 'Manager Override', etc. (Cova-defined enum)
-- Discount Reason: human-readable label like 'ON - Bud Club 5% OFF',
-- 'ALL - Customer Resolution', etc.
CREATE TABLE IF NOT EXISTS discount_lines (
    invoice_no       TEXT NOT NULL,
    line_no          INTEGER NOT NULL,
    sale_date        TEXT NOT NULL,
    sale_datetime    TEXT,
    location_id      TEXT NOT NULL,
    sku              TEXT,
    product_name     TEXT,
    quantity         REAL,
    discount_type    TEXT,                     -- 'Manual Line Discount', 'Promotion', etc.
    discount_reason  TEXT,                     -- 'ON - Bud Club 5% OFF', 'ALL - Customer Resolution'
    discount_amount  REAL NOT NULL DEFAULT 0,  -- positive = $ given away
    cashier          TEXT,
    customer_name    TEXT,
    PRIMARY KEY (invoice_no, line_no, sku)
);
CREATE INDEX IF NOT EXISTS ix_disc_date ON discount_lines (sale_date);
CREATE INDEX IF NOT EXISTS ix_disc_loc_date ON discount_lines (location_id, sale_date);
CREATE INDEX IF NOT EXISTS ix_disc_reason ON discount_lines (discount_reason, sale_date);
CREATE INDEX IF NOT EXISTS ix_disc_cashier ON discount_lines (cashier, sale_date);

-- Competitor pricing — one row per (competitor, variant, tier, date).
-- price_tier allows Market/Elite/etc to coexist. Same SKU can have multiple
-- rows if Canna Cabana exposes both tiers.
CREATE TABLE IF NOT EXISTS competitor_prices (
    competitor_name    TEXT NOT NULL,         -- "Canna Cabana Cundles"
    sb_competes_with   TEXT NOT NULL,         -- which SB store this targets ("S2")
    collection         TEXT,                  -- category handle from scraper
    collection_label   TEXT,                  -- human-readable category
    product_id         TEXT,                  -- competitor's product ID (stringified)
    product_handle     TEXT,
    product_title      TEXT NOT NULL,
    vendor             TEXT,                  -- = brand
    product_type       TEXT,                  -- = category
    variant_id         TEXT NOT NULL,
    variant_sku        TEXT,                  -- competitor's internal SKU
    variant_title      TEXT,
    variant_size       TEXT,
    price              REAL,                  -- displayed price
    compare_at_price   REAL,                  -- "was" price for sales
    price_tier         TEXT NOT NULL DEFAULT 'market',   -- market | elite | other
    available          INTEGER,               -- 0/1
    tags               TEXT,
    source             TEXT NOT NULL,         -- "cannacabana" | "hibuddy" | "manual" etc
    collected_at       TEXT NOT NULL,         -- ISO-8601 UTC from scrape
    loaded_at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (competitor_name, variant_id, price_tier, collected_at)
);
CREATE INDEX IF NOT EXISTS ix_comp_prices_competitor ON competitor_prices (competitor_name, collected_at DESC);
CREATE INDEX IF NOT EXISTS ix_comp_prices_sb_store ON competitor_prices (sb_competes_with, collected_at DESC);
CREATE INDEX IF NOT EXISTS ix_comp_prices_type ON competitor_prices (product_type, collected_at DESC);
CREATE INDEX IF NOT EXISTS ix_comp_prices_vendor ON competitor_prices (vendor, collected_at DESC);

-- Manager registry. Just names for now — graduates to real users with auth later.
-- The is_admin flag gates destructive ops (managing other managers, anchor overrides).
CREATE TABLE IF NOT EXISTS managers (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL UNIQUE,
    location_id  TEXT,
    is_admin     INTEGER NOT NULL DEFAULT 0,
    is_active    INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- One rating per (sku, location, manager). Integer 0-10. Latest overwrites.
CREATE TABLE IF NOT EXISTS product_ratings (
    sku          TEXT NOT NULL,
    location_id  TEXT NOT NULL,
    author_name  TEXT NOT NULL,
    rating       INTEGER NOT NULL,
    notes        TEXT,
    updated_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (sku, location_id, author_name)
);
CREATE INDEX IF NOT EXISTS ix_ratings_sku ON product_ratings (sku);
CREATE INDEX IF NOT EXISTS ix_ratings_loc ON product_ratings (location_id, sku);

-- Comments: unlimited history per (sku, location). Ordered by created_at.
-- location_id is nullable for chain-wide comments.
CREATE TABLE IF NOT EXISTS product_comments (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    sku          TEXT NOT NULL,
    location_id  TEXT,
    author_name  TEXT NOT NULL,
    body         TEXT NOT NULL,
    created_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_comments_sku ON product_comments (sku, created_at DESC);

-- Anchor overrides: manually force a SKU into or out of Top-50 anchor status.
-- mode=include means treat as anchor even if not in trailing-revenue Top 50.
-- mode=exclude means exclude from anchor list even if it would otherwise qualify.
CREATE TABLE IF NOT EXISTS anchor_overrides (
    sku          TEXT NOT NULL,
    location_id  TEXT NOT NULL,
    mode         TEXT NOT NULL CHECK (mode IN ('include', 'exclude')),
    reason       TEXT,
    author_name  TEXT,
    created_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (sku, location_id)
);

-- User-supplied notes explaining why a SKU is stocked out at a given store.
-- Optional context for the team: "OCS delayed", "supplier issue", "intentional sell-down", etc.
-- Auto-cleared if the SKU comes back in stock (handled in API layer).
CREATE TABLE IF NOT EXISTS stockout_notes (
    sku          TEXT NOT NULL,
    location_id  TEXT NOT NULL,
    note         TEXT,
    author_name  TEXT,
    updated_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (sku, location_id)
);

-- ============================================================================
-- App settings — engine tuning parameters editable from the Admin UI
-- ============================================================================
-- This table holds the live values for engine knobs (reorder ceilings, velocity
-- floor, pack-size threshold, etc.). The reorder engine and other consumers
-- read from here at the start of each computation rather than from module
-- constants. Defaults are set on first init via `_seed_default_settings`.
--
-- Design notes:
--   - One row per setting. Key is dotted-namespace (e.g. 'reorder.hero_ceiling_days')
--   - `default_value` is the hardcoded baseline. UI shows it alongside `value`
--     so users can reset.
--   - `min_value` / `max_value` are validation bounds. Engine rejects writes
--     outside this range.
--   - `value_type` is 'int' | 'float' | 'percent' | 'days' — drives UI input
--     formatting and validation.
--   - `description` is shown as the field tooltip.
--   - `section` groups settings in the UI (e.g. 'Reorder Engine').
CREATE TABLE IF NOT EXISTS app_settings (
    key            TEXT PRIMARY KEY,
    value          REAL NOT NULL,
    default_value  REAL NOT NULL,
    min_value      REAL,
    max_value      REAL,
    value_type     TEXT NOT NULL DEFAULT 'float',  -- 'int' | 'float' | 'percent' | 'days'
    section        TEXT NOT NULL DEFAULT 'general',
    label          TEXT NOT NULL,
    description    TEXT,
    sort_order     INTEGER NOT NULL DEFAULT 0,
    updated_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Audit trail: every settings change logged with before/after values + actor
CREATE TABLE IF NOT EXISTS app_settings_history (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    key          TEXT NOT NULL,
    old_value    REAL,
    new_value    REAL NOT NULL,
    changed_by   TEXT,
    changed_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    note         TEXT
);
CREATE INDEX IF NOT EXISTS ix_settings_history_key ON app_settings_history (key, changed_at DESC);

-- ============================================================================
-- Market Intelligence — OCS regional/municipal benchmarks
-- ============================================================================
-- OCS provides per-store reports comparing your sales to other stores in your
-- municipality. Files exported every 48hr, dropped into
-- imports/market_intelligence/{YYYY-MM-DD}/{StoreName}/.
--
-- Two source files per store, joined on SKU during import:
--   1. "Average Sales Units" — cumulative units over the period
--   2. "Sales Velocity" — average daily units (with sales_days column)
--
-- We persist per-import so historical comparisons remain possible.
CREATE TABLE IF NOT EXISTS market_intelligence_imports (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    location_id     TEXT NOT NULL,           -- which store this data is FOR
    period_start    TEXT,                    -- 'YYYY-MM-DD' covered by export
    period_end      TEXT,
    period_days     INTEGER,                 -- end - start + 1
    sku_count       INTEGER NOT NULL DEFAULT 0,
    has_municipality INTEGER NOT NULL DEFAULT 1,  -- 0 for Innisfil-style cases
    imported_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    source_files    TEXT,                    -- JSON list of filenames
    notes           TEXT
);
CREATE INDEX IF NOT EXISTS ix_mi_imports_loc ON market_intelligence_imports (location_id, imported_at DESC);

CREATE TABLE IF NOT EXISTS market_intelligence_data (
    import_id            INTEGER NOT NULL,
    sku                  TEXT NOT NULL,             -- matches ocs_catalog.ocs_variant_number
    item_name            TEXT,
    brand                TEXT,
    supplier             TEXT,
    subcategory          TEXT,
    size                 TEXT,
    your_units           REAL,                       -- "Your Store(s)" cumulative units
    municipality_units   REAL,                       -- "Your Municipality" cumulative units (per peer store avg)
    your_velocity        REAL,                       -- units/day at YOUR store
    municipality_velocity REAL,                      -- units/day at peer stores avg
    sales_days           INTEGER,                    -- days the SKU sold (anywhere)
    PRIMARY KEY (import_id, sku),
    FOREIGN KEY (import_id) REFERENCES market_intelligence_imports(id)
);
CREATE INDEX IF NOT EXISTS ix_mi_data_sku ON market_intelligence_data (sku);
CREATE INDEX IF NOT EXISTS ix_mi_data_muni ON market_intelligence_data (import_id, municipality_units DESC);

-- Gap suggestion review state — tracks which "consider adding" SKUs the user
-- has snoozed/dismissed/added so they don't keep haunting the Reorder Report.
-- Keyed by (location_id, sku): per-store decisions, since "we should carry X"
-- might be true at Bradford but not at Innisfil.
CREATE TABLE IF NOT EXISTS gap_suggestions_status (
    location_id      TEXT NOT NULL,
    sku              TEXT NOT NULL,                -- ocs_variant_number
    status           TEXT NOT NULL,                -- 'snoozed' | 'dismissed' | 'added'
    snoozed_until    TEXT,                         -- ISO date; only meaningful if status='snoozed'
    note             TEXT,
    author_name      TEXT,
    updated_at       TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (location_id, sku)
);
CREATE INDEX IF NOT EXISTS ix_gap_status_snooze ON gap_suggestions_status (location_id, status, snoozed_until);

-- ============================================================================
-- Authentication & user management
-- ============================================================================
-- Day-1 design:
--   - Email + bcrypt password
--   - Two roles: 'admin' (can manage users) and 'regular' (can use dashboard)
--   - 30-day sessions stored as signed cookies; rows kept here for revocation
--   - First admin bootstrapped via create_admin.py CLI command
--   - No self-signup — admins create accounts
--   - Force password change flag for temp passwords set by admins

CREATE TABLE IF NOT EXISTS users (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    email               TEXT NOT NULL UNIQUE COLLATE NOCASE,
    name                TEXT,
    password_hash       TEXT NOT NULL,           -- bcrypt hash, never the raw password
    role                TEXT NOT NULL DEFAULT 'regular',  -- 'admin' | 'regular'
    must_change_password INTEGER NOT NULL DEFAULT 0,      -- 1 = force change on next login
    is_active           INTEGER NOT NULL DEFAULT 1,
    created_at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_by          TEXT,                    -- email of admin who created them
    last_login_at       TEXT
);

-- Session table: each login creates a row. Cookie carries the session_token.
-- Deleting a row instantly logs that session out (revocation).
CREATE TABLE IF NOT EXISTS user_sessions (
    session_token       TEXT PRIMARY KEY,        -- random 32-byte hex, lives in the cookie
    user_id             INTEGER NOT NULL,
    created_at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at          TEXT NOT NULL,
    last_seen_at        TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    user_agent          TEXT,                    -- for the "logged in on" display
    FOREIGN KEY (user_id) REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS ix_sessions_user ON user_sessions (user_id);
CREATE INDEX IF NOT EXISTS ix_sessions_expires ON user_sessions (expires_at);

-- ============================================================================
-- Email scraper configuration & log
-- ============================================================================
-- The scraper polls an IMAP inbox for Cova auto-exports and drops attachments
-- into the relevant imports/ folder. Foundation supports IMAP w/ app-password;
-- OAuth2 to come later. Configuration is per-account (you might have one
-- inbox for Cova, another for OCS, etc).

CREATE TABLE IF NOT EXISTS email_scraper_accounts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    label           TEXT NOT NULL,           -- 'Cova exports', 'OCS reports', etc.
    provider        TEXT NOT NULL DEFAULT 'imap',  -- 'imap' for now
    host            TEXT NOT NULL,           -- 'imap.gmail.com' etc.
    port            INTEGER NOT NULL DEFAULT 993,
    username        TEXT NOT NULL,           -- usually the email address
    password_enc    TEXT NOT NULL,           -- app password, encrypted at rest
    folder          TEXT NOT NULL DEFAULT 'INBOX',
    is_active       INTEGER NOT NULL DEFAULT 1,
    poll_interval_min INTEGER NOT NULL DEFAULT 5,
    last_polled_at  TEXT,
    last_error      TEXT,
    created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Log of every email processed (or skipped). Lets you debug "where's my file?"
CREATE TABLE IF NOT EXISTS email_scraper_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id      INTEGER NOT NULL,
    message_uid     TEXT NOT NULL,           -- IMAP UID for dedupe
    received_at     TEXT,                    -- when email was received
    processed_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    sender          TEXT,
    subject         TEXT,
    attachment_count INTEGER NOT NULL DEFAULT 0,
    action          TEXT NOT NULL,           -- 'imported' | 'skipped' | 'error'
    detail          TEXT,                    -- summary for UI display
    FOREIGN KEY (account_id) REFERENCES email_scraper_accounts(id)
);
CREATE INDEX IF NOT EXISTS ix_email_log_account ON email_scraper_log (account_id, processed_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS ix_email_log_uid ON email_scraper_log (account_id, message_uid);

-- ============================================================================
-- OCS Order Fill template — the per-order-cycle catalog OCS sends out
-- ============================================================================
-- The Order Fill is the single source of truth for what OCS will deliver
-- in the upcoming order window. It includes:
--   - Flow Through tier per SKU (NO=Click-to-Buy, YES+Standard, YES+Expedited)
--   - Estimated delivery date per tier
--   - Back In Stock flag (recently restored)
--   - New Arrival flag
--   - Available quantity at OCS, max order qty per store
--   - Current OCS price (catches price changes)
-- We import the most recent Order Fill and use it for visibility in the
-- Reorder tab. Older Order Fills are archived but only the most recent
-- drives badges & lead time calculations.
CREATE TABLE IF NOT EXISTS order_fill_runs (
    id                       SERIAL PRIMARY KEY,
    source_file              TEXT NOT NULL,
    generated_at             TEXT,            -- parsed from filename
    -- Computed lead times (delivery date − generated_at), in days
    click_to_buy_lead_days   INTEGER,
    flow_thru_expedited_lead_days INTEGER,
    flow_thru_standard_lead_days  INTEGER,
    -- Computed delivery dates (most common per tier)
    click_to_buy_delivery    TEXT,
    flow_thru_expedited_delivery TEXT,
    flow_thru_standard_delivery  TEXT,
    sku_count                INTEGER NOT NULL DEFAULT 0,
    imported_at              TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (source_file)
);

CREATE TABLE IF NOT EXISTS order_fill_skus (
    run_id                   INTEGER NOT NULL,
    ocs_variant_number       TEXT NOT NULL,
    -- Flow Through info
    flow_thru                INTEGER NOT NULL DEFAULT 0,  -- 0=NO (Click-to-Buy), 1=YES (Flow Through)
    delivery_tier            TEXT,                         -- NULL for CTB; 'Standard' or 'Expedited' for FT
    estimated_delivery_date  TEXT,                         -- ISO date
    -- Status flags
    back_in_stock            INTEGER NOT NULL DEFAULT 0,
    new_arrival              INTEGER NOT NULL DEFAULT 0,
    favourite                INTEGER NOT NULL DEFAULT 0,
    -- Pricing & qty
    item_price               REAL,
    unit_price               REAL,
    pack_size                INTEGER,
    max_qty                  INTEGER,
    available_quantity       INTEGER,
    price_change             TEXT,                         -- 'INCREASE' / 'DECREASE' / NULL
    price_change_pct         REAL,
    -- Product info (denormalized for convenience; main source is products table)
    brand                    TEXT,
    item_name                TEXT,
    sub_category             TEXT,
    PRIMARY KEY (run_id, ocs_variant_number),
    FOREIGN KEY (run_id) REFERENCES order_fill_runs(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_ofs_variant ON order_fill_skus (ocs_variant_number);
CREATE INDEX IF NOT EXISTS ix_ofs_back_in_stock ON order_fill_skus (back_in_stock) WHERE back_in_stock = 1;
CREATE INDEX IF NOT EXISTS ix_ofs_flow_thru ON order_fill_skus (flow_thru, delivery_tier);

CREATE TABLE IF NOT EXISTS import_runs (
    id                SERIAL PRIMARY KEY,
    file_name         TEXT NOT NULL,
    file_type         TEXT NOT NULL,
    location_id       TEXT,
    started_at        TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at       TEXT,
    status            TEXT NOT NULL DEFAULT 'running',
    records_processed INTEGER NOT NULL DEFAULT 0,
    error_message     TEXT
);

-- Brand partners we have any kind of commercial relationship with
-- (data revenue deals, LTOs, vendor-funded promotions, etc.).
-- The "brand" name should match what appears in products.brand for joins.
CREATE TABLE IF NOT EXISTS brand_partners (
    id              SERIAL PRIMARY KEY,
    brand_name      TEXT NOT NULL UNIQUE,
    contact_name    TEXT,
    contact_email   TEXT,
    notes           TEXT,
    is_active       INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_brand_partners_active ON brand_partners (is_active, brand_name);

-- Data revenue deals: brand pays us a percentage of something for sales data.
-- Multiple deals can exist for the same brand if terms changed over time;
-- we use the date range to determine which one applies for a given period.
-- sku_filter is a comma-separated list of SKUs (Cova SKU or OCS variant).
-- Empty/NULL sku_filter means deal applies to all SKUs from this brand.
CREATE TABLE IF NOT EXISTS data_revenue_deals (
    id              SERIAL PRIMARY KEY,
    brand_id        INTEGER NOT NULL,
    start_date      TEXT NOT NULL,
    end_date        TEXT,
    percentage      REAL NOT NULL,
    basis           TEXT NOT NULL CHECK (basis IN ('retail_sales', 'wholesale_cost', 'gross_profit', 'units_sold')),
    sku_filter      TEXT,
    notes           TEXT,
    created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (brand_id) REFERENCES brand_partners(id)
);
CREATE INDEX IF NOT EXISTS ix_data_deals_brand ON data_revenue_deals (brand_id, start_date DESC);

-- Limited-Time Offers: brand-funded promotions, volume rebates, wholesale
-- discounts, or feature flags. Multiple SKUs per LTO via lto_skus.
CREATE TABLE IF NOT EXISTS ltos (
    id                  SERIAL PRIMARY KEY,
    name                TEXT NOT NULL,
    brand_id            INTEGER,
    start_date          TEXT NOT NULL,
    end_date            TEXT NOT NULL,
    lto_type            TEXT NOT NULL CHECK (lto_type IN
                            ('wholesale_discount', 'volume_rebate', 'promo_credit', 'feature_flag')),
    discount_per_unit   REAL,
    rebate_threshold    INTEGER,
    rebate_percentage   REAL,
    notes               TEXT,
    is_active           INTEGER NOT NULL DEFAULT 1,
    created_at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (brand_id) REFERENCES brand_partners(id)
);
CREATE INDEX IF NOT EXISTS ix_ltos_dates ON ltos (start_date, end_date);
CREATE INDEX IF NOT EXISTS ix_ltos_brand ON ltos (brand_id);

-- Junction table linking LTOs to applicable SKUs.
-- sku is the Cova SKU (matches products.sku).
-- If an LTO has zero rows here, treat it as applying to ALL SKUs of the brand.
CREATE TABLE IF NOT EXISTS lto_skus (
    lto_id          INTEGER NOT NULL,
    sku             TEXT NOT NULL,
    PRIMARY KEY (lto_id, sku),
    FOREIGN KEY (lto_id) REFERENCES ltos(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_lto_skus_sku ON lto_skus (sku);


-- ============================================================================
-- Licensed Producers (LPs) — the upstream cannabis suppliers
-- ============================================================================
-- Each LP can produce multiple brands. Data revenue agreements are typically
-- signed at the LP level (e.g. an Auxly agreement covers Lepp + Dykstra + ...).
-- We track the agreement lifecycle status here so the team can see who's at
-- which stage (sent / signed / data shared / report sent / invoice sent).
--
-- Pre-seeded from OCS catalog (supplier column) so we have the universe of
-- potential partners, even those without an active agreement.
CREATE TABLE IF NOT EXISTS licensed_producers (
    id                       SERIAL PRIMARY KEY,
    name                     TEXT NOT NULL UNIQUE,
    name_in_ocs              TEXT,                  -- exact "Supplier Name" from OCS catalog
    name_in_master_list      TEXT,                  -- as it appears in user's master list (for traceability)

    -- Agreement lifecycle (Boolean status flags from user's Summary sheet)
    agreement_sent           INTEGER NOT NULL DEFAULT 0,
    agreement_signed         INTEGER NOT NULL DEFAULT 0,
    data_sent                INTEGER NOT NULL DEFAULT 0,
    report_sent              INTEGER NOT NULL DEFAULT 0,
    invoice_sent             INTEGER NOT NULL DEFAULT 0,

    -- Deal terms (free text since formats vary: "6% on COGS", "7% of Gross Sales", etc.)
    terms_summary            TEXT,
    payment_terms            TEXT,                  -- "Net 30", "Net 60", etc.

    -- LTO tracking (separate lifecycle from the agreement)
    lto_active               INTEGER NOT NULL DEFAULT 0,
    lto_offer                TEXT,                  -- description of current LTO offer
    lto_applicable_skus      TEXT,                  -- comma-separated or "See sheet"
    lto_report_sent          INTEGER NOT NULL DEFAULT 0,
    lto_invoice_sent         INTEGER NOT NULL DEFAULT 0,
    lto_payment_terms        TEXT,

    -- Operational
    is_active                INTEGER NOT NULL DEFAULT 1,
    notes                    TEXT,
    created_at               TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at               TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_lps_active ON licensed_producers (is_active, name);
CREATE INDEX IF NOT EXISTS ix_lps_signed ON licensed_producers (agreement_signed);

-- Multiple contacts per LP. Some LPs in the master list have 2-4 reps.
CREATE TABLE IF NOT EXISTS lp_contacts (
    id              SERIAL PRIMARY KEY,
    lp_id           INTEGER NOT NULL,
    name            TEXT,
    email           TEXT,
    phone           TEXT,
    role            TEXT,            -- optional: "Sales Rep", "Account Manager"
    notes           TEXT,            -- e.g. "Dog's name is Dolce, Happy"
    is_primary      INTEGER NOT NULL DEFAULT 0,
    sort_order      INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (lp_id) REFERENCES licensed_producers(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_lp_contacts_lp ON lp_contacts (lp_id, sort_order);


-- OCS delivery invoices (SO and CI prefixes both supported).
-- One physical delivery may produce multiple invoices on the same date.
CREATE TABLE IF NOT EXISTS invoices (
    invoice_no       TEXT PRIMARY KEY,
    location_id      TEXT,
    invoice_date     TEXT NOT NULL,
    customer_account TEXT,
    fiscal_period    TEXT,
    subtotal         REAL,
    total_with_tax   REAL,
    line_count       INTEGER,
    units_total      INTEGER,
    source_filename  TEXT,
    imported_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_invoices_loc_date ON invoices (location_id, invoice_date DESC);
CREATE INDEX IF NOT EXISTS ix_invoices_date ON invoices (invoice_date DESC);

CREATE TABLE IF NOT EXISTS invoice_lines (
    invoice_no       TEXT NOT NULL,
    ocs_variant      TEXT NOT NULL,
    description      TEXT,
    units_delivered  INTEGER NOT NULL,
    unit_price       REAL,
    line_total       REAL,
    PRIMARY KEY (invoice_no, ocs_variant)
);
CREATE INDEX IF NOT EXISTS ix_invlines_variant ON invoice_lines (ocs_variant);
"""

# SQLite doesn't have SERIAL — fix that
DDL = DDL.replace("SERIAL PRIMARY KEY", "INTEGER PRIMARY KEY AUTOINCREMENT")


def init_schema(conn) -> None:
    """Create tables if they don't exist. Idempotent."""
    cur = conn.cursor()
    cur.executescript(DDL)
    conn.commit()

    # Migrations — add columns that may not exist in older databases.
    # SQLite doesn't support IF NOT EXISTS on ADD COLUMN, so we check first.
    _ensure_column(conn, "products", "category_path", "TEXT")
    _ensure_column(conn, "products", "top_level", "TEXT")
    _ensure_column(conn, "products", "ocs_variant_number", "TEXT")
    # LP (Licensed Producer) — populated from collective buysheets, used by
    # the data-revenue resolver to apply LP-level direct deals (e.g., Organigram
    # has a direct deal → all SHRED, Edison, etc. SKUs excluded from collectives)
    _ensure_column(conn, "products", "lp", "TEXT")

    # brand_partners type/direct flags — distinguishes brand vs LP partners
    # and marks ones that are direct deals (override collectives entirely)
    _ensure_column(conn, "brand_partners", "partner_type", "TEXT NOT NULL DEFAULT 'brand'")
    _ensure_column(conn, "brand_partners", "is_direct_deal", "INTEGER NOT NULL DEFAULT 0")

    # Inventory snapshots: capture First/Last Received Dates and Days Since Last
    # Sold straight from the Cova export (these columns appear in every IOH
    # export but were previously discarded). Used by the Promotions tab and
    # available for display elsewhere.
    _ensure_column(conn, "inventory_snapshots", "first_received_date", "TEXT")
    _ensure_column(conn, "inventory_snapshots", "last_received_date", "TEXT")
    _ensure_column(conn, "inventory_snapshots", "days_since_last_sold", "INTEGER")

    # LTOs can be tied to an LP (licensed_producers) instead of a brand_partner.
    # This is how the Data Partners tab creates LTOs for OCS catalog products.
    _ensure_column(conn, "ltos", "lp_id", "INTEGER")
    _ensure_column(conn, "ltos", "rate_basis", "TEXT")  # 'retail_sales' | 'wholesale_cost' | 'gross_profit'
    _ensure_column(conn, "ltos", "rate_percentage", "REAL")  # The data revenue % for this LTO

    cur = conn.cursor()
    cur.execute("CREATE INDEX IF NOT EXISTS ix_products_lp ON products (lp)")
    cur.execute("CREATE INDEX IF NOT EXISTS ix_brand_partners_direct ON brand_partners (is_direct_deal, partner_type)")
    cur.execute("CREATE INDEX IF NOT EXISTS ix_ltos_lp ON ltos (lp_id)")
    conn.commit()

    # Seed default app_settings on first run. Idempotent — never overwrites
    # user-modified values, only fills in rows that don't exist yet.
    _seed_default_settings(conn)

    # ONE-TIME MIGRATION: pack_size_min_fraction was originally stored as a
    # 0-1 fraction (0.5 = 50%) but the UI displays it as a percent and the
    # confusion was real. Convert any value ≤ 1.0 to its percent equivalent
    # so users see "50" instead of "0.5". Safe to run repeatedly — if the
    # value is already 50 or higher, this no-ops.
    cur = conn.cursor()
    cur.execute("""
        UPDATE app_settings
        SET value = value * 100,
            default_value = CASE WHEN default_value <= 1.0 THEN default_value * 100 ELSE default_value END,
            min_value = CASE WHEN min_value <= 1.0 THEN min_value * 100 ELSE min_value END,
            max_value = CASE WHEN max_value <= 1.0 THEN max_value * 100 ELSE max_value END
        WHERE key = 'reorder.pack_size_min_fraction' AND value <= 1.0
    """)
    conn.commit()


# ----------------------------------------------------------------------------
# Default settings: the canonical list of tunable engine parameters.
# Each entry: (key, default, min, max, type, section, label, description, sort)
# ----------------------------------------------------------------------------
_DEFAULT_SETTINGS = [
    # Reorder Engine — Coverage
    ("reorder.order_cycle_days", 7, 1, 30, "days", "Reorder Engine — Coverage",
     "Order cycle (days)",
     "How often you submit OCS orders. Drives the trigger threshold (we reorder when days_supply ≤ order_cycle + lead_time).",
     10),
    ("reorder.lead_time_days", 4, 1, 30, "days", "Reorder Engine — Coverage",
     "Lead time (days)",
     "Days from placing an OCS order to product arriving in store. Used to compute the reorder trigger.",
     20),
    ("reorder.hero_ceiling_days", 7, 3, 30, "days", "Reorder Engine — Coverage",
     "Hero ceiling (days)",
     "Days of supply target for Hero SKUs. Lower = tighter, more risk of brief stockouts. Higher = more inventory held. Mix-aware multipliers do NOT inflate this.",
     30),
    ("reorder.regular_ceiling_days", 10, 3, 30, "days", "Reorder Engine — Coverage",
     "Regular ceiling (days)",
     "Days of supply target for non-Hero SKUs. Mix-aware multipliers can adjust this up or down by category.",
     40),

    # Reorder Engine — Filtering
    ("reorder.min_velocity", 0.0, 0.0, 5.0, "float", "Reorder Engine — Filtering",
     "Velocity floor (units/day)",
     "Minimum daily velocity to be reorderable. Set to 0.0 to rely entirely on the trigger + pack-size filter (recommended). Heroes always skip this floor.",
     10),
    ("reorder.pack_size_min_fraction", 50, 0, 100, "percent", "Reorder Engine — Filtering",
     "Pack-size threshold (%)",
     "Minimum % of a case the math must want before we order one. Below this, the SKU is skipped. 50% means we won't order 1 unit of a 4-pack to satisfy 1 unit of demand.",
     20),
    ("reorder.overstock_days", 50, 14, 365, "days", "Reorder Engine — Filtering",
     "Overstock threshold (days)",
     "SKUs with more than this many days of supply are flagged as overstocked.",
     30),

    # Hero Classification
    ("hero.top_n_per_format", 5, 1, 20, "int", "Hero Classification",
     "Top N per format",
     "How many SKUs per (category, size) bucket get Hero status. Lower = more selective, higher = more Heroes.",
     10),
    ("hero.lookback_days", 90, 14, 365, "days", "Hero Classification",
     "Lookback window (days)",
     "Trailing days of sales used to rank Heroes by revenue.",
     20),

    # Promotions Cascade
    ("promo.tier_3mo_pct", 5, 0, 50, "percent", "Promotions Cascade",
     "3-month tier discount (%)",
     "Discount % suggested for products with 3 months of shelf age (90-119 days since last received).",
     10),
    ("promo.tier_4mo_pct", 10, 0, 50, "percent", "Promotions Cascade",
     "4-month tier discount (%)",
     "Discount % suggested for products with 4 months of shelf age (120-149 days).",
     20),
    ("promo.tier_5mo_pct", 15, 0, 50, "percent", "Promotions Cascade",
     "5-month tier discount (%)",
     "Discount % suggested for products with 5 months of shelf age (150-179 days).",
     30),
    ("promo.tier_6mo_pct", 20, 0, 50, "percent", "Promotions Cascade",
     "6+ month tier discount (%)",
     "Discount % suggested for products with 6+ months of shelf age (180+ days).",
     40),
    ("promo.max_velocity", 0.3, 0.0, 5.0, "float", "Promotions Cascade",
     "Promo velocity ceiling (units/day)",
     "SKUs above this velocity aren't candidates for promos — they're moving fine on their own.",
     50),

    # Dead Stock
    ("deadstock.window_days", 30, 7, 365, "days", "Dead Stock / Overstock",
     "Dead-stock window (days)",
     "How many trailing days define 'dead'. Longer window = stricter (more days of zero sales required).",
     10),
]


def _seed_default_settings(conn) -> None:
    """Insert default settings for any keys that don't exist yet.
    Never overwrites user-modified values."""
    cur = conn.cursor()
    for key, default, min_v, max_v, vtype, section, label, desc, sort in _DEFAULT_SETTINGS:
        cur.execute("""
            INSERT INTO app_settings
                (key, value, default_value, min_value, max_value,
                 value_type, section, label, description, sort_order)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (key) DO NOTHING
        """, (key, default, default, min_v, max_v, vtype, section, label, desc, sort))
    conn.commit()


def _ensure_column(conn, table: str, column: str, ddl_type: str) -> None:
    """Add a column if it doesn't exist. Safe to call repeatedly."""
    cur = conn.cursor()
    cur.execute(f"PRAGMA table_info({table})")
    existing = {row[1] for row in cur.fetchall()}
    if column not in existing:
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}")
