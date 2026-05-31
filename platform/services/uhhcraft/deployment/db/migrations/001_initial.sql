-- +goose Up
-- +goose StatementBegin

-- Case-insensitive text for email columns so 'Foo@x.com' and 'foo@x.com' are
-- the same identity — the UNIQUE constraint and all lookups fold case.
CREATE EXTENSION IF NOT EXISTS citext;

-- ─────────────────────────────────────────────────────────────────────────────
-- Users
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE users (
    id                      UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    email                   CITEXT      NOT NULL UNIQUE,
    password_hash           TEXT        NOT NULL,
    role                    TEXT        NOT NULL DEFAULT 'user',  -- 'user' | 'admin'
    email_verified          BOOLEAN     NOT NULL DEFAULT FALSE,
    next_order_discount_pct SMALLINT    NOT NULL DEFAULT 0,       -- 0, 5, or 10
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT users_role_check CHECK (role IN ('user', 'admin')),
    CONSTRAINT users_discount_check CHECK (next_order_discount_pct IN (0, 5, 10))
);

-- ─────────────────────────────────────────────────────────────────────────────
-- Sessions  (scs postgresstore schema)
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE sessions (
    token  TEXT        PRIMARY KEY,
    data   BYTEA       NOT NULL,
    expiry TIMESTAMPTZ NOT NULL
);

CREATE INDEX sessions_expiry_idx ON sessions (expiry);

-- ─────────────────────────────────────────────────────────────────────────────
-- Email verification and password reset tokens
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE email_verification_tokens (
    id         UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id    UUID        NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token      TEXT        NOT NULL UNIQUE,
    expires_at TIMESTAMPTZ NOT NULL,
    used_at    TIMESTAMPTZ
);

CREATE TABLE password_reset_tokens (
    id         UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id    UUID        NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token      TEXT        NOT NULL UNIQUE,
    expires_at TIMESTAMPTZ NOT NULL,
    used_at    TIMESTAMPTZ
);

-- ─────────────────────────────────────────────────────────────────────────────
-- Catalog
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE catalog_categories (
    id         UUID     PRIMARY KEY DEFAULT gen_random_uuid(),
    slug       TEXT     NOT NULL UNIQUE,
    name       TEXT     NOT NULL,
    sort_order SMALLINT NOT NULL DEFAULT 0,
    active     BOOLEAN  NOT NULL DEFAULT TRUE
);

CREATE TABLE catalog_items (
    id               UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    slug             TEXT        NOT NULL UNIQUE,
    category_id      UUID        REFERENCES catalog_categories(id) ON DELETE SET NULL,
    name             TEXT        NOT NULL,
    description      TEXT        NOT NULL,
    product_type     TEXT        NOT NULL,           -- 'sticker' | 'print'
    size_tier        TEXT,                           -- prints: 'small'|'medium'|'large'|'extra-large'
    base_price_usd   NUMERIC(10,2) NOT NULL,
    material_ids     TEXT[]      NOT NULL DEFAULT '{}',
    cut_finish_ids   TEXT[]      NOT NULL DEFAULT '{}',
    model_glb_path   TEXT,                           -- MinIO key, GLB preview
    model_stl_path   TEXT,                           -- MinIO key, STL manufacturing (prints)
    image_png_path   TEXT,                           -- MinIO key, PNG (stickers)
    thumbnail_path   TEXT        NOT NULL,
    active           BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT catalog_items_type_check CHECK (product_type IN ('sticker', 'print'))
);

CREATE INDEX catalog_items_category_idx ON catalog_items (category_id) WHERE active = TRUE;
CREATE INDEX catalog_items_type_idx     ON catalog_items (product_type) WHERE active = TRUE;

-- ─────────────────────────────────────────────────────────────────────────────
-- AI Generations
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE generations (
    id             UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id        UUID        REFERENCES users(id) ON DELETE SET NULL,
    session_id     TEXT,                             -- guest identifier
    product_type   TEXT        NOT NULL,             -- 'sticker' | 'print'
    prompt         TEXT        NOT NULL,
    material_id    TEXT        NOT NULL,
    cut_finish_id  TEXT        NOT NULL,
    status         TEXT        NOT NULL DEFAULT 'pending',
    error_message  TEXT,
    asset_png_path TEXT,                             -- MinIO key
    asset_glb_path TEXT,                             -- MinIO key
    asset_stl_path TEXT,                             -- MinIO key (prints only)
    river_job_id   BIGINT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at   TIMESTAMPTZ,

    CONSTRAINT generations_type_check   CHECK (product_type IN ('sticker', 'print')),
    CONSTRAINT generations_status_check CHECK (status IN ('pending','processing','completed','failed'))
);

-- Keep last 10 per user: enforced in application layer (not DB constraint)
CREATE INDEX generations_user_recent_idx    ON generations (user_id, created_at DESC) WHERE user_id IS NOT NULL;
CREATE INDEX generations_session_recent_idx ON generations (session_id, created_at DESC) WHERE session_id IS NOT NULL;
CREATE INDEX generations_status_idx         ON generations (status) WHERE status IN ('pending','processing');

-- ─────────────────────────────────────────────────────────────────────────────
-- Cart
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE cart_items (
    id              UUID          PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID          REFERENCES users(id) ON DELETE CASCADE,
    session_id      TEXT,
    item_type       TEXT          NOT NULL,           -- 'catalog' | 'generated'
    catalog_item_id UUID          REFERENCES catalog_items(id) ON DELETE SET NULL,
    generation_id   UUID          REFERENCES generations(id) ON DELETE SET NULL,
    material_id     TEXT          NOT NULL,
    cut_finish_id   TEXT          NOT NULL,
    quantity        SMALLINT      NOT NULL DEFAULT 1,
    unit_price_usd  NUMERIC(10,2) NOT NULL,
    locked_until    TIMESTAMPTZ,                      -- 30-min reservation for generated items
    created_at      TIMESTAMPTZ   NOT NULL DEFAULT NOW(),

    CONSTRAINT cart_item_type_check CHECK (item_type IN ('catalog','generated')),
    CONSTRAINT cart_item_source_check CHECK (
        (item_type = 'catalog'   AND catalog_item_id IS NOT NULL) OR
        (item_type = 'generated' AND generation_id   IS NOT NULL)
    ),
    -- Every cart row must belong to either a logged-in user or a guest session;
    -- an orphaned row (both NULL) is unreachable and would never be cleaned up.
    CONSTRAINT cart_item_owner_check CHECK (
        user_id IS NOT NULL OR session_id IS NOT NULL
    ),
    CONSTRAINT cart_item_quantity_check CHECK (quantity > 0)
);

CREATE INDEX cart_items_user_idx    ON cart_items (user_id) WHERE user_id IS NOT NULL;
CREATE INDEX cart_items_session_idx ON cart_items (session_id) WHERE session_id IS NOT NULL;

-- ─────────────────────────────────────────────────────────────────────────────
-- Orders
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE orders (
    id                       UUID          PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id                  UUID          REFERENCES users(id) ON DELETE SET NULL,
    guest_email              CITEXT,
    status                   TEXT          NOT NULL DEFAULT 'pending',
    subtotal_usd             NUMERIC(10,2) NOT NULL,
    shipping_usd             NUMERIC(10,2) NOT NULL DEFAULT 0,
    tax_usd                  NUMERIC(10,2) NOT NULL DEFAULT 0,
    discount_usd             NUMERIC(10,2) NOT NULL DEFAULT 0,
    discount_pct             SMALLINT      NOT NULL DEFAULT 0,
    total_usd                NUMERIC(10,2) NOT NULL,
    shipping_address         JSONB         NOT NULL,
    shipping_method          TEXT,
    tracking_number          TEXT,
    priority                 BOOLEAN       NOT NULL DEFAULT FALSE,
    stripe_payment_intent_id TEXT          UNIQUE,
    stripe_charge_id         TEXT,
    fulfillment_route        TEXT          NOT NULL DEFAULT 'inhouse',
    external_order_id        TEXT,                   -- Printify / Hubs order ID
    age_gate_confirmed       BOOLEAN       NOT NULL DEFAULT FALSE,
    created_at               TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    updated_at               TIMESTAMPTZ   NOT NULL DEFAULT NOW(),

    CONSTRAINT orders_status_check CHECK (
        status IN ('pending','paid','manufacturing','shipped','delivered','cancelled','refunded')
    ),
    CONSTRAINT orders_fulfillment_check CHECK (
        fulfillment_route IN ('inhouse','printify','hubs')
    ),
    CONSTRAINT orders_customer_check CHECK (
        user_id IS NOT NULL OR guest_email IS NOT NULL
    )
);

CREATE INDEX orders_user_idx             ON orders (user_id, created_at DESC) WHERE user_id IS NOT NULL;
-- No separate index on stripe_payment_intent_id: the column's UNIQUE constraint
-- already creates a backing index that the webhook lookup uses.
CREATE INDEX orders_status_priority_idx  ON orders (status, priority DESC, created_at ASC);

CREATE TABLE order_items (
    id                       UUID          PRIMARY KEY DEFAULT gen_random_uuid(),
    order_id                 UUID          NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    item_type                TEXT          NOT NULL,
    catalog_item_id          UUID          REFERENCES catalog_items(id),
    generation_id            UUID          REFERENCES generations(id),
    name                     TEXT          NOT NULL,  -- snapshot at order time
    material_id              TEXT          NOT NULL,
    cut_finish_id            TEXT          NOT NULL,
    quantity                 SMALLINT      NOT NULL DEFAULT 1,
    unit_price_usd           NUMERIC(10,2) NOT NULL,
    manufacturing_asset_path TEXT,                   -- locked MinIO key
    status                   TEXT          NOT NULL DEFAULT 'pending',
    created_at               TIMESTAMPTZ   NOT NULL DEFAULT NOW(),

    CONSTRAINT order_items_type_check CHECK (item_type IN ('catalog','generated')),
    -- A line item's source must match its type, mirroring cart_item_source_check
    -- so the snapshot can never lose its provenance.
    CONSTRAINT order_items_source_check CHECK (
        (item_type = 'catalog'   AND catalog_item_id IS NOT NULL) OR
        (item_type = 'generated' AND generation_id   IS NOT NULL)
    ),
    CONSTRAINT order_items_quantity_check CHECK (quantity > 0)
);

CREATE INDEX order_items_order_idx ON order_items (order_id);

-- ─────────────────────────────────────────────────────────────────────────────
-- Order snapshots — the cart captured at payment-intent creation time
-- ─────────────────────────────────────────────────────────────────────────────
--
-- The checkout flow snapshots the cart when it creates a Stripe PaymentIntent,
-- keyed by the intent id. The webhook (which may fire before, during, or after
-- the browser returns) reads this snapshot to build the order idempotently, so
-- the order can never drift from what the customer saw even if the live cart
-- changed in between. Totals and the shipping address are recomputed at webhook
-- time from the PaymentIntent metadata + these item rows, so only the cart line
-- items are persisted here. payment_intent_id is the natural PK.
CREATE TABLE order_snapshots (
    payment_intent_id TEXT        PRIMARY KEY,
    items             JSONB       NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ─────────────────────────────────────────────────────────────────────────────
-- Content moderation
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE prompt_blocklist (
    id         UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    category   TEXT        NOT NULL,  -- 'copyright'|'nsfw'|'hate'|'political'|'violence'|'other'
    term       TEXT        NOT NULL UNIQUE,
    active     BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX prompt_blocklist_active_idx ON prompt_blocklist (active) WHERE active = TRUE;

-- ─────────────────────────────────────────────────────────────────────────────
-- Showcase gallery
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE showcase_items (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    catalog_item_id UUID        REFERENCES catalog_items(id) ON DELETE CASCADE,
    caption         TEXT,
    active          BOOLEAN     NOT NULL DEFAULT TRUE,
    sort_order      SMALLINT    NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX showcase_items_active_idx ON showcase_items (sort_order) WHERE active = TRUE;

-- ─────────────────────────────────────────────────────────────────────────────
-- Abandoned cart email dedup
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE abandoned_cart_emails (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id      TEXT,
    user_id         UUID        REFERENCES users(id) ON DELETE CASCADE,
    email           CITEXT      NOT NULL,
    sent_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    cart_created_at TIMESTAMPTZ NOT NULL
);

-- Dedup: at most one abandoned-cart email per owner per cart epoch, so a retry
-- or overlapping cron tick can't double-send. user-owned and guest carts are
-- keyed independently (one of user_id / session_id is always set).
CREATE UNIQUE INDEX abandoned_cart_emails_user_dedup
    ON abandoned_cart_emails (user_id, cart_created_at) WHERE user_id IS NOT NULL;
CREATE UNIQUE INDEX abandoned_cart_emails_session_dedup
    ON abandoned_cart_emails (session_id, cart_created_at) WHERE session_id IS NOT NULL;

-- ─────────────────────────────────────────────────────────────────────────────
-- updated_at trigger (applied to users, catalog_items, orders)
-- ─────────────────────────────────────────────────────────────────────────────

CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER users_updated_at
    BEFORE UPDATE ON users
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER catalog_items_updated_at
    BEFORE UPDATE ON catalog_items
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER orders_updated_at
    BEFORE UPDATE ON orders
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- +goose StatementEnd

-- +goose Down
-- +goose StatementBegin

DROP TRIGGER IF EXISTS orders_updated_at       ON orders;
DROP TRIGGER IF EXISTS catalog_items_updated_at ON catalog_items;
DROP TRIGGER IF EXISTS users_updated_at        ON users;
DROP FUNCTION IF EXISTS set_updated_at();

DROP TABLE IF EXISTS abandoned_cart_emails;
DROP TABLE IF EXISTS showcase_items;
DROP TABLE IF EXISTS prompt_blocklist;
DROP TABLE IF EXISTS order_snapshots;
DROP TABLE IF EXISTS order_items;
DROP TABLE IF EXISTS orders;
DROP TABLE IF EXISTS cart_items;
DROP TABLE IF EXISTS generations;
DROP TABLE IF EXISTS catalog_items;
DROP TABLE IF EXISTS catalog_categories;
DROP TABLE IF EXISTS password_reset_tokens;
DROP TABLE IF EXISTS email_verification_tokens;
DROP TABLE IF EXISTS sessions;
DROP TABLE IF EXISTS users;

-- +goose StatementEnd
