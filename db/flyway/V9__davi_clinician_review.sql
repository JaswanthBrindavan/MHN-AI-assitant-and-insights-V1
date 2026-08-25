-- V9__davi_clinician_review.sql — clinician review of held insights, for
-- adoption into mhn-spring's Flyway chain (src/main/resources/db/migration/),
-- following the V6/V7/V8 precedent.
--
-- On the shared production database Flyway owns ALL schema; the Davi repo's
-- Alembic chain (version table davi_alembic_version) builds local and test
-- databases only.
--
-- IDEMPOTENT: every statement is IF NOT EXISTS / duplicate-guarded.
--
-- Conventions (matching ai_processing_runs and the other Davi tables):
-- user_id columns are plain uuid with NO foreign key to "user".
--
-- SECURITY NOTE — read this before granting anyone a row in
-- clinician_reviewers. That table is the ONLY thing standing between a user
-- id and cross-user read access to sensitive health insights. There is no
-- role claim in the session JWT; membership is exactly this table. Grants
-- should be deliberate, rare, and revoked with active=false (never DELETE:
-- the audit rows are the record of what happened while access was granted).
--
-- insight_review_audit is APPEND-ONLY by discipline. Nothing in the
-- application updates or deletes from it. A wrong decision is corrected by a
-- new row so the sequence stays readable.

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

-- "Who looked at my records?" is a question a patient is entitled to have
-- answered, so the subject side is indexed too.
CREATE INDEX IF NOT EXISTS ix_insight_review_audit_subject
    ON public.insight_review_audit (subject_user_id);
