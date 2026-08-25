-- V20__davi_chat_platform.sql
--
-- EVERY outstanding Davi schema change, in one file. Apply this and nothing
-- else is pending on the Davi side.
--
-- ============================================================================
-- WHY V20, AND WHY ONE FILE
-- ============================================================================
-- Davi previously staged four separate migrations numbered V7-V10. Those
-- numbers are TAKEN in mhn-spring's chain by other work:
--
--     Spring V7  = medical_history                (Davi V7  = user_profile)
--     Spring V8  = medical_history_date_order     (Davi V8  = feedback)
--     Spring V9  = period_pause_and_pregnancy     (Davi V9  = clinician_review)
--     Spring V10 = ai_name_check                  (Davi V10 = erasure + actor)
--
-- Applying the Davi files under those numbers would have collided with
-- migrations already in the chain. Verified against
-- mhn-spring/src/main/resources/db/migration: none of the Davi tables below
-- (user_profiles, turn_feedback, clinician_reviewers, insight_review_audit,
-- erasure_requests) appear anywhere in V1-V19, so nothing here has been
-- adopted yet and all of it is still pending.
--
-- Spring's chain ends at V19, so this is V20, and the four are consolidated
-- into one file because they are one adoption.
--
-- IDEMPOTENT THROUGHOUT: every statement is IF NOT EXISTS or guarded by a
-- catalog check, so this succeeds on a database where Davi's local Alembic
-- already created some of these objects (the RUN_MIGRATIONS_ON_START
-- shortcut), and a rerun is a no-op.
--
-- CONVENTIONS, matching V6__davi_ai_tables.sql and ai_processing_runs: every
-- user_id is a plain uuid with NO foreign key to "user". The only foreign keys
-- are to Davi's own tables.
--
-- ============================================================================
-- CONTENTS
--   1. user_profiles        — consent-gated personalization
--   2. turn_feedback        — reader verdicts on assistant turns
--   3. clinician_reviewers  — who may review held insights
--   4. insight_review_audit — who read whose insight, and what they decided
--   5. erasure_requests     — deferred, cancellable "forget me"
--   6. job_runs.actor_user_id — WHO caused a job, not just what
--   7. Retention indexes    — make the time-based purges cheap
--   8. user_memory_document — the assembled memory the assistant carries
-- ============================================================================


-- ============================================================================
-- 1. user_profiles
-- ============================================================================
-- Self-reported personal health context (conditions, medications, allergies,
-- pregnancy). Written ONLY while a chat_personalization grant exists in
-- consent_ledger, and erased in full when that consent is revoked. The ledger
-- itself is append-only and is never deleted.

CREATE TABLE IF NOT EXISTS public.user_profiles (
    id                  uuid NOT NULL,
    created_at          timestamptz NOT NULL,
    user_id             uuid NOT NULL,
    age_band            varchar(16),
    sex                 varchar(16),
    communication_style varchar(16),
    preferred_language  varchar(16),
    chronic_conditions  jsonb,
    current_medications jsonb,
    allergies           jsonb,
    goals               jsonb,
    is_pregnant         boolean,
    consent_grant_id    uuid,
    updated_at          timestamptz
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


-- ============================================================================
-- 2. turn_feedback
-- ============================================================================
-- Thumbs up/down on an assistant turn, so a bad reply can become a regression
-- test. NO foreign key to conversation_messages on purpose: feedback must
-- SURVIVE the deletion of the conversation it judges, or clearing history
-- would erase the evidence that produced a test case.
--
-- `comment` is free text a reader typed and may contain personal health
-- information. It is never logged and never sent to a model.

CREATE TABLE IF NOT EXISTS public.turn_feedback (
    id         uuid PRIMARY KEY,
    created_at timestamptz NOT NULL DEFAULT now(),
    user_id    uuid NOT NULL,
    message_id uuid NOT NULL,
    session_id uuid,
    receipt_id uuid,
    rating     varchar(8) NOT NULL,
    reason     varchar(16),
    comment    varchar(2000),
    triaged_at timestamptz
);

-- One verdict per reader per turn. Sending it again is a correction, not a
-- second vote -- counting both would skew the very numbers this exists for.
CREATE UNIQUE INDEX IF NOT EXISTS uq_turn_feedback
    ON public.turn_feedback (user_id, message_id);

CREATE INDEX IF NOT EXISTS ix_turn_feedback_user_id
    ON public.turn_feedback (user_id);

-- The review queue reads exactly this: down-votes not yet turned into a case.
CREATE INDEX IF NOT EXISTS ix_turn_feedback_untriaged
    ON public.turn_feedback (created_at DESC)
    WHERE rating = 'down' AND triaged_at IS NULL;


-- ============================================================================
-- 3. clinician_reviewers
-- ============================================================================
-- A row here is a grant of CROSS-USER read access to sensitive health
-- insights. There is no role claim in the session JWT, so this table is the
-- only thing standing between an ordinary user id and another person's held
-- insights. Create rows deliberately and rarely.
--
-- Revoke with active = false, never DELETE: insight_review_audit references
-- the reviewer, and the grant's history is part of the record.
--
-- It is NOT a defence against a leaked SERVICE_TOKEN, which the auth layer
-- accepts as identity together with X-User-Id.

CREATE TABLE IF NOT EXISTS public.clinician_reviewers (
    id           uuid PRIMARY KEY,
    created_at   timestamptz NOT NULL DEFAULT now(),
    user_id      uuid NOT NULL,
    display_name varchar(200),
    active       boolean NOT NULL DEFAULT true,
    granted_by   uuid,
    revoked_at   timestamptz
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_clinician_reviewer_user
    ON public.clinician_reviewers (user_id);

CREATE INDEX IF NOT EXISTS ix_clinician_reviewers_user_id
    ON public.clinician_reviewers (user_id);


-- ============================================================================
-- 4. insight_review_audit
-- ============================================================================
-- Append-only by discipline: nothing in the application updates or deletes a
-- row here. A wrong decision is corrected by a NEW row so the sequence stays
-- readable.
--
-- Every READ is recorded, not just every decision -- by the time a decision
-- exists the information has already been seen. The subject side is indexed
-- because "who looked at my records?" is a question a patient is entitled to
-- have answered.

CREATE TABLE IF NOT EXISTS public.insight_review_audit (
    id               uuid PRIMARY KEY,
    created_at       timestamptz NOT NULL DEFAULT now(),
    reviewer_user_id uuid NOT NULL,
    subject_user_id  uuid NOT NULL,
    artifact_id      uuid,
    action           varchar(16) NOT NULL,
    note             varchar(1000),
    content_hash     varchar(64)
);

CREATE INDEX IF NOT EXISTS ix_insight_review_audit_reviewer
    ON public.insight_review_audit (reviewer_user_id);

CREATE INDEX IF NOT EXISTS ix_insight_review_audit_subject
    ON public.insight_review_audit (subject_user_id);


-- ============================================================================
-- 5. erasure_requests
-- ============================================================================
-- A "forget me" is scheduled rather than immediate. Two properties make that
-- honest rather than a delay tactic:
--
--   * The data stops being USED the moment the request is made -- every
--     per-user memory read and write is suppressed from that second. The rows
--     survive only so an accidental or coerced deletion can be withdrawn.
--   * scheduled_for is FIXED at request time, never recomputed at purge time,
--     so changing the configured grace period later cannot move a promise
--     already given to somebody.
--
-- This row deliberately OUTLIVES the account it describes: it is the record
-- that the erasure was requested, authorised and carried out. deleted_counts
-- holds the per-table rows actually removed, because "we deleted your data" is
-- a claim somebody may one day have to substantiate.
--
-- NOT erased by the process, deliberately: consent_ledger (append-only, and
-- the evidence the erasure was authorised) and insight_review_audit (the
-- record of who read whose data; erasing it would let access go unaccounted
-- for).

CREATE TABLE IF NOT EXISTS public.erasure_requests (
    id             uuid PRIMARY KEY,
    created_at     timestamptz NOT NULL DEFAULT now(),
    user_id        uuid NOT NULL,
    requested_at   timestamptz NOT NULL,
    scheduled_for  timestamptz NOT NULL,
    status         varchar(16) NOT NULL DEFAULT 'pending',
    completed_at   timestamptz,
    cancelled_at   timestamptz,
    deleted_counts jsonb,
    source         varchar(16) NOT NULL DEFAULT 'api'
);

CREATE INDEX IF NOT EXISTS ix_erasure_requests_user_id
    ON public.erasure_requests (user_id);

-- The sweep reads exactly this: pending requests whose window has expired.
CREATE INDEX IF NOT EXISTS ix_erasure_requests_scheduled_for
    ON public.erasure_requests (scheduled_for);

-- At most one PENDING request per user. A second "forget me" returns the
-- first rather than opening a second window with a later deadline. Partial,
-- so completed and cancelled history is unconstrained.
CREATE UNIQUE INDEX IF NOT EXISTS uq_erasure_requests_pending
    ON public.erasure_requests (user_id)
    WHERE status = 'pending';


-- ============================================================================
-- 6. job_runs.actor_user_id
-- ============================================================================
-- Without this you learn that a document was read and never by whom -- the one
-- field an access-control audit exists for.
--
-- NULLABLE because scheduled work genuinely has no actor: the nightly sweep is
-- caused by the clock. A NULL therefore means "the system", and must not be
-- read as "unknown user".
--
-- No foreign key to "user", and here that is more than convention: this row
-- must outlive the account it attributes, or an erasure would quietly destroy
-- the evidence of access it is supposed to leave behind.

ALTER TABLE public.job_runs
    ADD COLUMN IF NOT EXISTS actor_user_id uuid;

CREATE INDEX IF NOT EXISTS ix_job_runs_actor_user_id
    ON public.job_runs (actor_user_id);


-- ============================================================================
-- 7. Retention indexes
-- ============================================================================
-- conversation_messages and rag_turn_receipts are ~97.5% of Davi-owned
-- per-user bytes -- derived at 9.94 TB/year at 10M users -- and nothing
-- deleted either of them before now. The purge selects oldest-first by
-- created_at in batches; without an index that is a sequential scan of the two
-- largest tables in the schema, every night.
--
-- Receipts are kept LONGER than messages on purpose: they hash the message
-- rather than storing it, so they carry no PHI and are the audit trail proper.
-- Keeping the evidence while dropping the content is the point.

CREATE INDEX IF NOT EXISTS ix_conversation_messages_created_at
    ON public.conversation_messages (created_at);

CREATE INDEX IF NOT EXISTS ix_rag_turn_receipts_created_at
    ON public.rag_turn_receipts (created_at);


-- ============================================================================
-- 8. user_memory_document
-- ============================================================================
-- One row per user: everything the assistant knows about that reader,
-- assembled once and read with a single primary-key lookup instead of the
-- twenty-odd queries that otherwise run on every chat turn.
--
-- DERIVED AND REBUILDABLE. Every source of truth stays where it is; losing
-- this table costs a rebuild, never data. It holds only the reader's OWN data
-- — family records are read live on every turn, because family permission is
-- checked live and a document that had absorbed a relative's result would
-- survive the revocation that should have removed it.
--
-- `prompt_block` is rendered at WRITE time and is byte-stable between
-- rebuilds. That is not tidiness: it is what allows the block to sit behind a
-- prompt-cache breakpoint. Text that varied between identical rebuilds would
-- break the cache for that reader on every turn.
--
-- `source_hash` covers everything the document was built from, so identical
-- inputs produce no write and the stored text — and therefore the reader's
-- cached prefix — survives untouched.
--
-- `token_estimate` is recorded because every token here is charged on every
-- turn, outside the cache, forever. It is the number that governs the bill.

CREATE TABLE IF NOT EXISTS public.user_memory_document (
    id             uuid PRIMARY KEY,
    user_id        uuid NOT NULL,
    document       jsonb NOT NULL,
    prompt_block   text NOT NULL,
    source_hash    varchar(64) NOT NULL,
    built_at       timestamptz NOT NULL,
    schema_version smallint NOT NULL DEFAULT 1,
    token_estimate integer NOT NULL DEFAULT 0
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_user_memory_document
    ON public.user_memory_document (user_id);

CREATE INDEX IF NOT EXISTS ix_user_memory_document_user_id
    ON public.user_memory_document (user_id);

-- The nightly sweep reads oldest-first to find what needs rebuilding.
CREATE INDEX IF NOT EXISTS ix_user_memory_document_built_at
    ON public.user_memory_document (built_at);
