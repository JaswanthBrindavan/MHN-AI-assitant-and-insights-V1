-- V5__davi_ai_tables.sql — Davi AI assistant tables, for adoption into
-- mhn-spring's Flyway chain (src/main/resources/db/migration/), following the
-- V4__ai_tables.sql precedent: on the shared production database, Flyway owns
-- ALL schema; the Davi repo's Alembic chain (version table davi_alembic_version)
-- builds local/test databases only and takes no new revisions once this ships.
--
-- Conventions (matching the existing ai_* tables): user_id is a plain uuid with
-- NO foreign key to "user"; FKs exist only among Davi-owned tables. mcp_chunks
-- needs the pgvector extension for its embedding column.

CREATE EXTENSION IF NOT EXISTS vector;

-- PostgreSQL database dump


-- Dumped from database version 16.15 (Homebrew)
-- Dumped by pg_dump version 16.15 (Homebrew)




-- Name: active_symptom_states; Type: TABLE; Schema: public; Owner: -

CREATE TABLE public.active_symptom_states (
    user_id uuid NOT NULL,
    symptom character varying(128) NOT NULL,
    risk_level character varying(16) NOT NULL,
    last_seen_at timestamp with time zone NOT NULL,
    id uuid NOT NULL,
    created_at timestamp with time zone NOT NULL
);


-- Name: condition_registry; Type: TABLE; Schema: public; Owner: -

CREATE TABLE public.condition_registry (
    condition_code character varying(32) NOT NULL,
    display_name character varying(200) NOT NULL,
    aliases jsonb,
    engine_codes jsonb,
    source_file character varying(255),
    active boolean NOT NULL,
    id uuid NOT NULL,
    created_at timestamp with time zone NOT NULL
);


-- Name: consent_ledger; Type: TABLE; Schema: public; Owner: -

CREATE TABLE public.consent_ledger (
    user_id uuid NOT NULL,
    purpose character varying(64) NOT NULL,
    action character varying(16) NOT NULL,
    scope jsonb,
    source character varying(64) NOT NULL,
    id uuid NOT NULL,
    created_at timestamp with time zone NOT NULL
);


-- Name: conversation_messages; Type: TABLE; Schema: public; Owner: -

CREATE TABLE public.conversation_messages (
    session_id uuid NOT NULL,
    role character varying(16) NOT NULL,
    message text NOT NULL,
    extracted_intent jsonb,
    id uuid NOT NULL,
    created_at timestamp with time zone NOT NULL
);


-- Name: conversation_sessions; Type: TABLE; Schema: public; Owner: -

CREATE TABLE public.conversation_sessions (
    user_id uuid NOT NULL,
    id uuid NOT NULL,
    created_at timestamp with time zone NOT NULL
);


-- Name: conversation_summaries; Type: TABLE; Schema: public; Owner: -

CREATE TABLE public.conversation_summaries (
    session_id uuid NOT NULL,
    version integer NOT NULL,
    summary jsonb NOT NULL,
    covers_through_message_id uuid,
    token_estimate integer NOT NULL,
    id uuid NOT NULL,
    created_at timestamp with time zone NOT NULL
);


-- Name: drug_reference; Type: TABLE; Schema: public; Owner: -

CREATE TABLE public.drug_reference (
    source_id character varying(32),
    name character varying(255) NOT NULL,
    name_normalized character varying(255) NOT NULL,
    manufacturer character varying(255),
    dosage_type character varying(64),
    pack_size character varying(128),
    price_inr double precision,
    is_discontinued boolean NOT NULL,
    composition1 character varying(255),
    composition2 character varying(255),
    composition_normalized character varying(512),
    side_effects jsonb,
    uses jsonb,
    substitutes jsonb,
    chemical_class character varying(128),
    habit_forming character varying(16),
    therapeutic_class character varying(128),
    action_class character varying(128),
    id uuid NOT NULL,
    created_at timestamp with time zone NOT NULL
);


-- Name: insight_artifacts; Type: TABLE; Schema: public; Owner: -

CREATE TABLE public.insight_artifacts (
    user_id uuid NOT NULL,
    condition_code character varying(32) NOT NULL,
    tier character varying(24) NOT NULL,
    title character varying(200) NOT NULL,
    body text NOT NULL,
    facts_used jsonb,
    fired_rules jsonb,
    template_key character varying(48) NOT NULL,
    template_version integer NOT NULL,
    pipeline_version integer NOT NULL,
    content_hash character varying(64) NOT NULL,
    status character varying(20) NOT NULL,
    superseded_by uuid,
    recompute_reason character varying(64),
    id uuid NOT NULL,
    created_at timestamp with time zone NOT NULL
);


-- Name: insight_templates; Type: TABLE; Schema: public; Owner: -

CREATE TABLE public.insight_templates (
    template_key character varying(48) NOT NULL,
    version integer NOT NULL,
    locale character varying(16) NOT NULL,
    title character varying(200) NOT NULL,
    body text NOT NULL,
    status character varying(16) NOT NULL,
    id uuid NOT NULL
);


-- Name: job_runs; Type: TABLE; Schema: public; Owner: -

CREATE TABLE public.job_runs (
    name character varying(64) NOT NULL,
    trigger character varying(32) NOT NULL,
    status character varying(16) NOT NULL,
    started_at timestamp with time zone,
    finished_at timestamp with time zone,
    error text,
    input_hash character varying(64),
    id uuid NOT NULL,
    created_at timestamp with time zone NOT NULL
);


-- Name: mcp_chunks; Type: TABLE; Schema: public; Owner: -

CREATE TABLE public.mcp_chunks (
    condition_code character varying(32) NOT NULL,
    chunk_type character varying(48) NOT NULL,
    content text NOT NULL,
    embedding public.vector(1024),
    metadata jsonb,
    id uuid NOT NULL,
    created_at timestamp with time zone NOT NULL
);


-- Name: pedigree_conditions; Type: TABLE; Schema: public; Owner: -

CREATE TABLE public.pedigree_conditions (
    user_id uuid NOT NULL,
    slot character varying(32) NOT NULL,
    condition_code character varying(32) NOT NULL,
    condition_display character varying(128) NOT NULL,
    onset_band character varying(16) NOT NULL,
    certainty character varying(24) NOT NULL,
    provenance character varying(24) NOT NULL,
    consent_grant_id uuid,
    soft_deleted boolean NOT NULL,
    id uuid NOT NULL,
    created_at timestamp with time zone NOT NULL,
    soft_deleted_at timestamp with time zone
);


-- Name: pedigree_members; Type: TABLE; Schema: public; Owner: -

CREATE TABLE public.pedigree_members (
    user_id uuid NOT NULL,
    slot character varying(32) NOT NULL,
    vital_status character varying(16),
    cause_of_death character varying(128),
    id uuid NOT NULL,
    created_at timestamp with time zone NOT NULL
);


-- Name: rag_turn_receipts; Type: TABLE; Schema: public; Owner: -

CREATE TABLE public.rag_turn_receipts (
    user_id uuid NOT NULL,
    session_id uuid,
    query_hash character varying(64) NOT NULL,
    model_name character varying(64) NOT NULL,
    prompt_version character varying(32) NOT NULL,
    retrieved jsonb,
    grounding jsonb,
    grounding_mode character varying(16) NOT NULL,
    grounding_status character varying(24) NOT NULL,
    used_rag boolean NOT NULL,
    id uuid NOT NULL,
    created_at timestamp with time zone NOT NULL
);


-- Name: risk_rules; Type: TABLE; Schema: public; Owner: -

CREATE TABLE public.risk_rules (
    rule_key character varying(32) NOT NULL,
    pattern_key character varying(48) NOT NULL,
    params jsonb,
    condition_code character varying(32) NOT NULL,
    tier character varying(24) NOT NULL,
    modifier integer NOT NULL,
    template_key character varying(48),
    sensitive boolean NOT NULL,
    active boolean NOT NULL,
    version integer NOT NULL,
    rationale text NOT NULL,
    id uuid NOT NULL
);


-- Name: symptom_logs; Type: TABLE; Schema: public; Owner: -

CREATE TABLE public.symptom_logs (
    user_id uuid NOT NULL,
    symptom character varying(128) NOT NULL,
    risk_level character varying(16) NOT NULL,
    matched_terms jsonb,
    id uuid NOT NULL,
    created_at timestamp with time zone NOT NULL
);


-- Name: user_memories; Type: TABLE; Schema: public; Owner: -

CREATE TABLE public.user_memories (
    user_id uuid NOT NULL,
    kind character varying(24) NOT NULL,
    mem_key character varying(64) NOT NULL,
    value character varying(200) NOT NULL,
    mention_count integer NOT NULL,
    last_seen_at timestamp with time zone NOT NULL,
    id uuid NOT NULL,
    created_at timestamp with time zone NOT NULL
);


-- Name: active_symptom_states active_symptom_states_pkey; Type: CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.active_symptom_states
    ADD CONSTRAINT active_symptom_states_pkey PRIMARY KEY (id);


-- Name: condition_registry condition_registry_pkey; Type: CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.condition_registry
    ADD CONSTRAINT condition_registry_pkey PRIMARY KEY (id);


-- Name: consent_ledger consent_ledger_pkey; Type: CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.consent_ledger
    ADD CONSTRAINT consent_ledger_pkey PRIMARY KEY (id);


-- Name: conversation_messages conversation_messages_pkey; Type: CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.conversation_messages
    ADD CONSTRAINT conversation_messages_pkey PRIMARY KEY (id);


-- Name: conversation_sessions conversation_sessions_pkey; Type: CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.conversation_sessions
    ADD CONSTRAINT conversation_sessions_pkey PRIMARY KEY (id);


-- Name: conversation_summaries conversation_summaries_pkey; Type: CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.conversation_summaries
    ADD CONSTRAINT conversation_summaries_pkey PRIMARY KEY (id);


-- Name: drug_reference drug_reference_pkey; Type: CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.drug_reference
    ADD CONSTRAINT drug_reference_pkey PRIMARY KEY (id);


-- Name: insight_artifacts insight_artifacts_pkey; Type: CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.insight_artifacts
    ADD CONSTRAINT insight_artifacts_pkey PRIMARY KEY (id);


-- Name: insight_templates insight_templates_pkey; Type: CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.insight_templates
    ADD CONSTRAINT insight_templates_pkey PRIMARY KEY (id);


-- Name: job_runs job_runs_pkey; Type: CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.job_runs
    ADD CONSTRAINT job_runs_pkey PRIMARY KEY (id);


-- Name: mcp_chunks mcp_chunks_pkey; Type: CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.mcp_chunks
    ADD CONSTRAINT mcp_chunks_pkey PRIMARY KEY (id);


-- Name: pedigree_conditions pedigree_conditions_pkey; Type: CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.pedigree_conditions
    ADD CONSTRAINT pedigree_conditions_pkey PRIMARY KEY (id);


-- Name: pedigree_members pedigree_members_pkey; Type: CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.pedigree_members
    ADD CONSTRAINT pedigree_members_pkey PRIMARY KEY (id);


-- Name: rag_turn_receipts rag_turn_receipts_pkey; Type: CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.rag_turn_receipts
    ADD CONSTRAINT rag_turn_receipts_pkey PRIMARY KEY (id);


-- Name: risk_rules risk_rules_pkey; Type: CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.risk_rules
    ADD CONSTRAINT risk_rules_pkey PRIMARY KEY (id);


-- Name: symptom_logs symptom_logs_pkey; Type: CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.symptom_logs
    ADD CONSTRAINT symptom_logs_pkey PRIMARY KEY (id);


-- Name: active_symptom_states uq_active_symptom; Type: CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.active_symptom_states
    ADD CONSTRAINT uq_active_symptom UNIQUE (user_id, symptom);


-- Name: insight_templates uq_insight_template_key_version; Type: CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.insight_templates
    ADD CONSTRAINT uq_insight_template_key_version UNIQUE (template_key, version);


-- Name: pedigree_members uq_pedigree_member_slot; Type: CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.pedigree_members
    ADD CONSTRAINT uq_pedigree_member_slot UNIQUE (user_id, slot);


-- Name: user_memories uq_user_memory; Type: CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.user_memories
    ADD CONSTRAINT uq_user_memory UNIQUE (user_id, kind, mem_key);


-- Name: user_memories user_memories_pkey; Type: CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.user_memories
    ADD CONSTRAINT user_memories_pkey PRIMARY KEY (id);


-- Name: ix_active_symptom_states_user_id; Type: INDEX; Schema: public; Owner: -

CREATE INDEX ix_active_symptom_states_user_id ON public.active_symptom_states USING btree (user_id);


-- Name: ix_condition_registry_condition_code; Type: INDEX; Schema: public; Owner: -

CREATE UNIQUE INDEX ix_condition_registry_condition_code ON public.condition_registry USING btree (condition_code);


-- Name: ix_consent_ledger_user_id; Type: INDEX; Schema: public; Owner: -

CREATE INDEX ix_consent_ledger_user_id ON public.consent_ledger USING btree (user_id);


-- Name: ix_conversation_messages_session_id; Type: INDEX; Schema: public; Owner: -

CREATE INDEX ix_conversation_messages_session_id ON public.conversation_messages USING btree (session_id);


-- Name: ix_conversation_sessions_user_id; Type: INDEX; Schema: public; Owner: -

CREATE INDEX ix_conversation_sessions_user_id ON public.conversation_sessions USING btree (user_id);


-- Name: ix_conversation_summaries_session_id; Type: INDEX; Schema: public; Owner: -

CREATE INDEX ix_conversation_summaries_session_id ON public.conversation_summaries USING btree (session_id);


-- Name: ix_drug_reference_composition_normalized; Type: INDEX; Schema: public; Owner: -

CREATE INDEX ix_drug_reference_composition_normalized ON public.drug_reference USING btree (composition_normalized);


-- Name: ix_drug_reference_name_normalized; Type: INDEX; Schema: public; Owner: -

CREATE INDEX ix_drug_reference_name_normalized ON public.drug_reference USING btree (name_normalized);


-- Name: ix_insight_artifacts_content_hash; Type: INDEX; Schema: public; Owner: -

CREATE INDEX ix_insight_artifacts_content_hash ON public.insight_artifacts USING btree (content_hash);


-- Name: ix_insight_artifacts_user_id; Type: INDEX; Schema: public; Owner: -

CREATE INDEX ix_insight_artifacts_user_id ON public.insight_artifacts USING btree (user_id);


-- Name: ix_job_runs_name; Type: INDEX; Schema: public; Owner: -

CREATE INDEX ix_job_runs_name ON public.job_runs USING btree (name);


-- Name: ix_mcp_chunks_condition_code; Type: INDEX; Schema: public; Owner: -

CREATE INDEX ix_mcp_chunks_condition_code ON public.mcp_chunks USING btree (condition_code);


-- Name: ix_pedigree_conditions_user_id; Type: INDEX; Schema: public; Owner: -

CREATE INDEX ix_pedigree_conditions_user_id ON public.pedigree_conditions USING btree (user_id);


-- Name: ix_pedigree_members_user_id; Type: INDEX; Schema: public; Owner: -

CREATE INDEX ix_pedigree_members_user_id ON public.pedigree_members USING btree (user_id);


-- Name: ix_rag_turn_receipts_user_id; Type: INDEX; Schema: public; Owner: -

CREATE INDEX ix_rag_turn_receipts_user_id ON public.rag_turn_receipts USING btree (user_id);


-- Name: ix_risk_rules_rule_key; Type: INDEX; Schema: public; Owner: -

CREATE INDEX ix_risk_rules_rule_key ON public.risk_rules USING btree (rule_key);


-- Name: ix_symptom_logs_user_id; Type: INDEX; Schema: public; Owner: -

CREATE INDEX ix_symptom_logs_user_id ON public.symptom_logs USING btree (user_id);


-- Name: ix_user_memories_user_id; Type: INDEX; Schema: public; Owner: -

CREATE INDEX ix_user_memories_user_id ON public.user_memories USING btree (user_id);


-- Name: conversation_messages conversation_messages_session_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.conversation_messages
    ADD CONSTRAINT conversation_messages_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.conversation_sessions(id) ON DELETE CASCADE;


-- Name: conversation_summaries conversation_summaries_session_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.conversation_summaries
    ADD CONSTRAINT conversation_summaries_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.conversation_sessions(id) ON DELETE CASCADE;


-- Name: insight_artifacts insight_artifacts_superseded_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.insight_artifacts
    ADD CONSTRAINT insight_artifacts_superseded_by_fkey FOREIGN KEY (superseded_by) REFERENCES public.insight_artifacts(id);


-- Name: pedigree_conditions pedigree_conditions_consent_grant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.pedigree_conditions
    ADD CONSTRAINT pedigree_conditions_consent_grant_id_fkey FOREIGN KEY (consent_grant_id) REFERENCES public.consent_ledger(id);


-- PostgreSQL database dump complete


