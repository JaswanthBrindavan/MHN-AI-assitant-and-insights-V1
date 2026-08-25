-- V10__davi_erasure_and_actor.sql
--
-- ALL outstanding Davi schema changes in one file, for adoption into
-- mhn-spring's Flyway chain (src/main/resources/db/migration/), following the
-- V6–V9 precedent. Apply this one file; nothing else is pending.
--
-- On the shared production database Flyway owns ALL schema. The Davi repo's
-- Alembic chain (version table davi_alembic_version) builds local and test
-- databases only, and carries a matching revision.
--
-- IDEMPOTENT throughout: every statement is IF NOT EXISTS or duplicate-guarded,
-- so this also succeeds where Davi's local Alembic already created the objects.
--
-- Conventions (matching ai_processing_runs and the other Davi tables): every
-- user_id is a plain uuid with NO foreign key to "user".
--
-- ============================================================================
-- CONTENTS
--   1. erasure_requests      — new table. Deferred, cancellable "forget me".
--   2. job_runs.actor_user_id — new column. WHO caused a job, not just what.
--   3. Retention indexes     — make the new time-based purges cheap.
-- ============================================================================


-- ---------------------------------------------------------------------------
-- 1. erasure_requests
-- ---------------------------------------------------------------------------
-- A "forget me" is scheduled rather than immediate. Two properties make that
-- honest rather than a delay tactic:
--
--   * The data stops being USED the moment the request is made — every
--     per-user memory read and write is suppressed from that second. The rows
--     survive only so an accidental or coerced deletion can be withdrawn.
--   * scheduled_for is FIXED when the request is made, never recomputed at
--     purge time, so changing the configured grace period later cannot move a
--     promise already given to somebody.
--
-- This row deliberately OUTLIVES the account it describes: it is the record
-- that the erasure was requested, authorised and carried out. deleted_counts
-- holds the per-table row counts actually removed, because "we deleted your
-- data" is a claim somebody may one day have to substantiate.
--
-- NOT erased by this process, deliberately:
--   * consent_ledger      — append-only, and the evidence that the erasure was
--                           authorised in the first place.
--   * insight_review_audit — the record of which clinician read whose data. It
--                           protects the subject; a subject-triggered delete
--                           that erased it would let access go unaccounted for.

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

-- At most one PENDING request per user. A second "forget me" must return the
-- first one rather than opening a second window with a later deadline.
-- Partial, so completed and cancelled history is unconstrained.
CREATE UNIQUE INDEX IF NOT EXISTS uq_erasure_requests_pending
    ON public.erasure_requests (user_id)
    WHERE status = 'pending';


-- ---------------------------------------------------------------------------
-- 2. job_runs.actor_user_id
-- ---------------------------------------------------------------------------
-- Without this you learn that a document was read and never by whom — the one
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


-- ---------------------------------------------------------------------------
-- 3. Retention indexes
-- ---------------------------------------------------------------------------
-- conversation_messages and rag_turn_receipts are ~97.5% of Davi-owned
-- per-user bytes — derived at 9.94 TB/year at 10M users — and nothing deleted
-- either of them before now. The purge selects the oldest rows by created_at
-- in batches; without an index on created_at that is a sequential scan of the
-- largest tables in the schema, every night.
--
-- Receipts are kept LONGER than messages on purpose: they hash the message
-- rather than storing it, so they carry no PHI and are the audit trail proper.
-- Keeping the evidence while dropping the content is the point.

CREATE INDEX IF NOT EXISTS ix_conversation_messages_created_at
    ON public.conversation_messages (created_at);

CREATE INDEX IF NOT EXISTS ix_rag_turn_receipts_created_at
    ON public.rag_turn_receipts (created_at);
