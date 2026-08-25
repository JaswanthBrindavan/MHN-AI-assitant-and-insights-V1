-- V8__davi_feedback.sql — reader verdicts on assistant turns, for adoption
-- into mhn-spring's Flyway chain (src/main/resources/db/migration/),
-- following the V6/V7 precedent.
--
-- On the shared production database Flyway owns ALL schema; the Davi repo's
-- Alembic chain (version table davi_alembic_version) builds local and test
-- databases only.
--
-- IDEMPOTENT: every statement is IF NOT EXISTS / duplicate-guarded, so this
-- also succeeds where Davi's local Alembic already created the table.
--
-- Conventions (matching ai_processing_runs and the other Davi tables):
-- user_id is a plain uuid with NO foreign key to "user". message_id and
-- session_id are likewise unconstrained: feedback must SURVIVE the deletion
-- of the conversation it judges. A cascade here would erase the evidence
-- that produced a regression test every time a user cleared their history.
--
-- NOTE ON CONTENT: `comment` is free text a reader typed and may contain
-- personal health information. It is never logged and never sent to a model;
-- it exists so a correction carries the reader's own words.

CREATE TABLE IF NOT EXISTS public.turn_feedback (
    id           uuid PRIMARY KEY,
    created_at   timestamptz NOT NULL DEFAULT now(),
    user_id      uuid NOT NULL,
    message_id   uuid NOT NULL,
    session_id   uuid,
    receipt_id   uuid,
    rating       varchar(8) NOT NULL,
    reason       varchar(16),
    comment      varchar(2000),
    triaged_at   timestamptz
);

-- One verdict per reader per turn. Sending it again is a correction, not a
-- second vote — counting both would skew the very numbers this exists for.
CREATE UNIQUE INDEX IF NOT EXISTS uq_turn_feedback
    ON public.turn_feedback (user_id, message_id);

CREATE INDEX IF NOT EXISTS ix_turn_feedback_user_id
    ON public.turn_feedback (user_id);

-- The review queue reads exactly this: down-votes not yet turned into a case.
CREATE INDEX IF NOT EXISTS ix_turn_feedback_untriaged
    ON public.turn_feedback (created_at DESC)
    WHERE rating = 'down' AND triaged_at IS NULL;
