-- migrate:up
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE payment_intents (
    id                       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id                text NOT NULL,
    consumer_reference       text NOT NULL,
    provider_reference       text NOT NULL UNIQUE,
    idempotency_key          text NOT NULL,
    request_hash             text NOT NULL,
    provider                 text NOT NULL CHECK (provider IN ('demo', 'mercado_pago')),
    provider_preference_id   text,
    provider_payment_id      text,
    checkout_url             text,
    status                   text NOT NULL DEFAULT 'creating'
                             CHECK (status IN (
                                 'creating', 'pending', 'approved', 'rejected',
                                 'cancelled', 'refunded', 'failed'
                             )),
    amount                   bigint NOT NULL CHECK (amount > 0),
    currency                 char(3) NOT NULL,
    sandbox                  boolean NOT NULL DEFAULT false,
    callback_url             text NOT NULL,
    request_payload          jsonb NOT NULL,
    last_payment_payload     jsonb,
    expires_at               timestamptz NOT NULL,
    created_at               timestamptz NOT NULL DEFAULT now(),
    updated_at               timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, idempotency_key)
);

CREATE UNIQUE INDEX payment_intents_provider_preference_unique
    ON payment_intents (provider, provider_preference_id)
    WHERE provider_preference_id IS NOT NULL;
CREATE UNIQUE INDEX payment_intents_provider_payment_unique
    ON payment_intents (provider, provider_payment_id)
    WHERE provider_payment_id IS NOT NULL;
CREATE INDEX payment_intents_reconciliation_idx
    ON payment_intents (status, updated_at)
    WHERE status IN ('creating', 'pending');

CREATE TABLE payment_events (
    id                  bigserial PRIMARY KEY,
    provider            text NOT NULL CHECK (provider IN ('demo', 'mercado_pago')),
    provider_event_id   text NOT NULL,
    event_type          text NOT NULL,
    payment_intent_id   uuid REFERENCES payment_intents(id) ON DELETE SET NULL,
    payload             jsonb NOT NULL,
    processing_status   text NOT NULL DEFAULT 'received'
                        CHECK (processing_status IN ('received', 'processed', 'failed')),
    processing_attempts integer NOT NULL DEFAULT 1,
    processing_started_at timestamptz NOT NULL DEFAULT now(),
    error_code          text,
    received_at         timestamptz NOT NULL DEFAULT now(),
    processed_at        timestamptz,
    UNIQUE (provider, provider_event_id)
);

CREATE TABLE callback_outbox (
    id                  bigserial PRIMARY KEY,
    payment_intent_id   uuid NOT NULL REFERENCES payment_intents(id) ON DELETE CASCADE,
    callback_url        text NOT NULL,
    event_type          text NOT NULL,
    payload             jsonb NOT NULL,
    attempts            integer NOT NULL DEFAULT 0,
    available_at        timestamptz NOT NULL DEFAULT now(),
    delivered_at        timestamptz,
    dead_letter_at      timestamptz,
    last_error          text,
    created_at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (payment_intent_id, event_type)
);

CREATE INDEX callback_outbox_pending_idx
    ON callback_outbox (available_at, id)
    WHERE delivered_at IS NULL AND dead_letter_at IS NULL;

-- migrate:down
DROP TABLE IF EXISTS callback_outbox;
DROP TABLE IF EXISTS payment_events;
DROP TABLE IF EXISTS payment_intents;
