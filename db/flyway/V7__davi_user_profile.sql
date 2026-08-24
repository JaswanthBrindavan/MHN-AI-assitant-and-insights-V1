-- V7__davi_user_profile.sql — consent-gated personalization profile, for
-- adoption into mhn-spring's Flyway chain (src/main/resources/db/migration/),
-- following the V6__davi_ai_tables.sql precedent.
--
-- On the shared production database Flyway owns ALL schema; the Davi repo's
-- Alembic chain (version table davi_alembic_version) builds local and test
-- databases only.
--
-- IDEMPOTENT: every statement is IF NOT EXISTS / duplicate-guarded, so this
-- also succeeds on a database where Davi's local Alembic already created the
-- table (the RUN_MIGRATIONS_ON_START testing shortcut).
--
-- Conventions (matching the existing ai_* and V6 Davi tables): user_id is a
-- plain uuid with NO foreign key to "user"; the only FK is to Davi's own
-- consent_ledger.
--
-- NOTE ON CONTENT: this table holds self-reported personal health context
-- (conditions, medications, allergies, pregnancy status). It is written ONLY
-- when a chat_personalization grant exists in consent_ledger, and it is
-- deleted in full when that consent is revoked. The ledger itself is
-- append-only and outlives the data — the record that consent existed IS the
-- audit trail.

CREATE TABLE IF NOT EXISTS public.user_profiles (
    id uuid NOT NULL,
    created_at timestamp with time zone NOT NULL,
    user_id uuid NOT NULL,
    age_band character varying(16),
    sex character varying(16),
    communication_style character varying(16),
    preferred_language character varying(16),
    chronic_conditions jsonb,
    current_medications jsonb,
    allergies jsonb,
    goals jsonb,
    is_pregnant boolean,
    consent_grant_id uuid,
    updated_at timestamp with time zone
);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'user_profiles_pkey'
    ) THEN
        ALTER TABLE public.user_profiles
            ADD CONSTRAINT user_profiles_pkey PRIMARY KEY (id);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'uq_user_profile_user'
    ) THEN
        ALTER TABLE public.user_profiles
            ADD CONSTRAINT uq_user_profile_user UNIQUE (user_id);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'user_profiles_consent_grant_id_fkey'
    ) THEN
        ALTER TABLE public.user_profiles
            ADD CONSTRAINT user_profiles_consent_grant_id_fkey
            FOREIGN KEY (consent_grant_id)
            REFERENCES public.consent_ledger(id);
    END IF;
END
$$;

CREATE INDEX IF NOT EXISTS ix_user_profiles_user_id
    ON public.user_profiles (user_id);
