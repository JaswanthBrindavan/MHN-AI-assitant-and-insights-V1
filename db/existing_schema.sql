-- DROP SCHEMA public;

CREATE SCHEMA public AUTHORIZATION pg_database_owner;

COMMENT ON SCHEMA public IS 'standard public schema';

-- DROP TYPE public."blood_group_enum";

CREATE TYPE public."blood_group_enum" AS ENUM (
	'A+',
	'A-',
	'B+',
	'B-',
	'AB+',
	'AB-',
	'O+',
	'O-');

-- DROP TYPE public."body_measurement_type_enum";

CREATE TYPE public."body_measurement_type_enum" AS ENUM (
	'weight',
	'height',
	'bmi',
	'body_fat',
	'muscle_mass',
	'water',
	'bone_mass',
	'visceral_fat');

-- DROP TYPE public."created_by_doctor_connect_enum";

CREATE TYPE public."created_by_doctor_connect_enum" AS ENUM (
	'user',
	'doctor');

-- DROP TYPE public."currency_enum";

CREATE TYPE public."currency_enum" AS ENUM (
	'INR',
	'USD',
	'EUR',
	'GBP');

-- DROP TYPE public."day_pattern_enum";

CREATE TYPE public."day_pattern_enum" AS ENUM (
	'daily',
	'alternate',
	'custom',
	'interval');

-- DROP TYPE public."dosage_form_enum";

CREATE TYPE public."dosage_form_enum" AS ENUM (
	'tablet',
	'capsule',
	'syrup',
	'injection',
	'drops',
	'inhaler',
	'patch',
	'cream');

-- DROP TYPE public."dose_status_enum";

CREATE TYPE public."dose_status_enum" AS ENUM (
	'pending',
	'taken',
	'skipped',
	'forgotten');

-- DROP TYPE public."flow_intensity_enum";

CREATE TYPE public."flow_intensity_enum" AS ENUM (
	'light',
	'medium',
	'heavy',
	'spotting');

-- DROP TYPE public."gender_enum";

CREATE TYPE public."gender_enum" AS ENUM (
	'male',
	'female',
	'other');

-- DROP TYPE public."lifestyle_log_type_enum";

CREATE TYPE public."lifestyle_log_type_enum" AS ENUM (
	'water',
	'alcohol',
	'coffee',
	'tea',
	'smoking');

-- DROP TYPE public."login_provider_enum";

CREATE TYPE public."login_provider_enum" AS ENUM (
	'email',
	'google',
	'apple');

-- DROP TYPE public."manual_tracking_type_enum";

CREATE TYPE public."manual_tracking_type_enum" AS ENUM (
	'steps',
	'calories',
	'water',
	'sleep',
	'heart_rate',
	'blood_pressure',
	'blood_sugar',
	'spo2');

-- DROP TYPE public."medical_condition_status_enum";

CREATE TYPE public."medical_condition_status_enum" AS ENUM (
	'active',
	'resolved',
	'chronic',
	'monitoring');

-- DROP TYPE public."payment_method_enum";

CREATE TYPE public."payment_method_enum" AS ENUM (
	'card',
	'upi',
	'netbanking',
	'wallet',
	'paypal');

-- DROP TYPE public."payment_status_enum";

CREATE TYPE public."payment_status_enum" AS ENUM (
	'pending',
	'completed',
	'failed',
	'refunded');

-- DROP TYPE public."resource_type_enum";

CREATE TYPE public."resource_type_enum" AS ENUM (
	'bills',
	'vaccinations',
	'prescriptions',
	'reports',
	'scans_imaging',
	'insurance',
	'medical_condition');

-- DROP TYPE public."subscription_purpose_enum";

CREATE TYPE public."subscription_purpose_enum" AS ENUM (
	'basic',
	'premium',
	'family',
	'doctor');

-- DROP TYPE public."user_type_enum";

CREATE TYPE public."user_type_enum" AS ENUM (
	'user',
	'doctor',
	'admin',
	'staff',
	'superadmin');

-- DROP TYPE public."vital_source_enum";

CREATE TYPE public."vital_source_enum" AS ENUM (
	'manual',
	'apple',
	'google');

-- DROP TYPE public."vital_type_enum";

CREATE TYPE public."vital_type_enum" AS ENUM (
	'heart_rate',
	'blood_pressure',
	'blood_sugar',
	'spo2');

-- DROP SEQUENCE bills_id_seq;

CREATE SEQUENCE bills_id_seq
	INCREMENT BY 1
	MINVALUE 1
	MAXVALUE 2147483647
	START 1
	CACHE 1
	NO CYCLE;
-- DROP SEQUENCE body_measurement_id_seq;

CREATE SEQUENCE body_measurement_id_seq
	INCREMENT BY 1
	MINVALUE 1
	MAXVALUE 2147483647
	START 1
	CACHE 1
	NO CYCLE;
-- DROP SEQUENCE doctor_connect_id_seq;

CREATE SEQUENCE doctor_connect_id_seq
	INCREMENT BY 1
	MINVALUE 1
	MAXVALUE 2147483647
	START 1
	CACHE 1
	NO CYCLE;
-- DROP SEQUENCE doctor_department_id_seq;

CREATE SEQUENCE doctor_department_id_seq
	INCREMENT BY 1
	MINVALUE 1
	MAXVALUE 2147483647
	START 1
	CACHE 1
	NO CYCLE;
-- DROP SEQUENCE doctor_id_seq;

CREATE SEQUENCE doctor_id_seq
	INCREMENT BY 1
	MINVALUE 1
	MAXVALUE 2147483647
	START 1
	CACHE 1
	NO CYCLE;
-- DROP SEQUENCE doctor_specialization_id_seq;

CREATE SEQUENCE doctor_specialization_id_seq
	INCREMENT BY 1
	MINVALUE 1
	MAXVALUE 2147483647
	START 1
	CACHE 1
	NO CYCLE;
-- DROP SEQUENCE event_log_id_seq;

CREATE SEQUENCE event_log_id_seq
	INCREMENT BY 1
	MINVALUE 1
	MAXVALUE 9223372036854775807
	START 1
	CACHE 1
	NO CYCLE;
-- DROP SEQUENCE family_connect_id_seq;

CREATE SEQUENCE family_connect_id_seq
	INCREMENT BY 1
	MINVALUE 1
	MAXVALUE 2147483647
	START 1
	CACHE 1
	NO CYCLE;
-- DROP SEQUENCE family_file_access_id_seq;

CREATE SEQUENCE family_file_access_id_seq
	INCREMENT BY 1
	MINVALUE 1
	MAXVALUE 2147483647
	START 1
	CACHE 1
	NO CYCLE;
-- DROP SEQUENCE hospital_id_seq;

CREATE SEQUENCE hospital_id_seq
	INCREMENT BY 1
	MINVALUE 1
	MAXVALUE 2147483647
	START 1
	CACHE 1
	NO CYCLE;
-- DROP SEQUENCE insurance_id_seq;

CREATE SEQUENCE insurance_id_seq
	INCREMENT BY 1
	MINVALUE 1
	MAXVALUE 2147483647
	START 1
	CACHE 1
	NO CYCLE;
-- DROP SEQUENCE insurance_provider_id_seq;

CREATE SEQUENCE insurance_provider_id_seq
	INCREMENT BY 1
	MINVALUE 1
	MAXVALUE 2147483647
	START 1
	CACHE 1
	NO CYCLE;
-- DROP SEQUENCE lifestyle_log_id_seq;

CREATE SEQUENCE lifestyle_log_id_seq
	INCREMENT BY 1
	MINVALUE 1
	MAXVALUE 9223372036854775807
	START 1
	CACHE 1
	NO CYCLE;
-- DROP SEQUENCE manual_tracking_id_seq;

CREATE SEQUENCE manual_tracking_id_seq
	INCREMENT BY 1
	MINVALUE 1
	MAXVALUE 2147483647
	START 1
	CACHE 1
	NO CYCLE;
-- DROP SEQUENCE medical_condition_id_seq;

CREATE SEQUENCE medical_condition_id_seq
	INCREMENT BY 1
	MINVALUE 1
	MAXVALUE 2147483647
	START 1
	CACHE 1
	NO CYCLE;
-- DROP SEQUENCE medicine_dose_log_id_seq;

CREATE SEQUENCE medicine_dose_log_id_seq
	INCREMENT BY 1
	MINVALUE 1
	MAXVALUE 9223372036854775807
	START 1
	CACHE 1
	NO CYCLE;
-- DROP SEQUENCE medicine_manufacturer_id_seq;

CREATE SEQUENCE medicine_manufacturer_id_seq
	INCREMENT BY 1
	MINVALUE 1
	MAXVALUE 2147483647
	START 1
	CACHE 1
	NO CYCLE;
-- DROP SEQUENCE medicine_master_id_seq;

CREATE SEQUENCE medicine_master_id_seq
	INCREMENT BY 1
	MINVALUE 1
	MAXVALUE 2147483647
	START 1
	CACHE 1
	NO CYCLE;
-- DROP SEQUENCE medicine_tracking_id_seq;

CREATE SEQUENCE medicine_tracking_id_seq
	INCREMENT BY 1
	MINVALUE 1
	MAXVALUE 2147483647
	START 1
	CACHE 1
	NO CYCLE;
-- DROP SEQUENCE period_tracking_id_seq;

CREATE SEQUENCE period_tracking_id_seq
	INCREMENT BY 1
	MINVALUE 1
	MAXVALUE 2147483647
	START 1
	CACHE 1
	NO CYCLE;
-- DROP SEQUENCE prescriptions_id_seq;

CREATE SEQUENCE prescriptions_id_seq
	INCREMENT BY 1
	MINVALUE 1
	MAXVALUE 2147483647
	START 1
	CACHE 1
	NO CYCLE;
-- DROP SEQUENCE refresh_token_id_seq;

CREATE SEQUENCE refresh_token_id_seq
	INCREMENT BY 1
	MINVALUE 1
	MAXVALUE 9223372036854775807
	START 1
	CACHE 1
	NO CYCLE;
-- DROP SEQUENCE relations_id_seq;

CREATE SEQUENCE relations_id_seq
	INCREMENT BY 1
	MINVALUE 1
	MAXVALUE 2147483647
	START 1
	CACHE 1
	NO CYCLE;
-- DROP SEQUENCE reports_id_seq;

CREATE SEQUENCE reports_id_seq
	INCREMENT BY 1
	MINVALUE 1
	MAXVALUE 2147483647
	START 1
	CACHE 1
	NO CYCLE;
-- DROP SEQUENCE scans_imaging_id_seq;

CREATE SEQUENCE scans_imaging_id_seq
	INCREMENT BY 1
	MINVALUE 1
	MAXVALUE 2147483647
	START 1
	CACHE 1
	NO CYCLE;
-- DROP SEQUENCE sleep_sessions_id_seq;

CREATE SEQUENCE sleep_sessions_id_seq
	INCREMENT BY 1
	MINVALUE 1
	MAXVALUE 9223372036854775807
	START 1
	CACHE 1
	NO CYCLE;
-- DROP SEQUENCE sos_contact_id_seq;

CREATE SEQUENCE sos_contact_id_seq
	INCREMENT BY 1
	MINVALUE 1
	MAXVALUE 2147483647
	START 1
	CACHE 1
	NO CYCLE;
-- DROP SEQUENCE subscriptions_id_seq;

CREATE SEQUENCE subscriptions_id_seq
	INCREMENT BY 1
	MINVALUE 1
	MAXVALUE 2147483647
	START 1
	CACHE 1
	NO CYCLE;
-- DROP SEQUENCE thp_age_range_id_seq;

CREATE SEQUENCE thp_age_range_id_seq
	INCREMENT BY 1
	MINVALUE 1
	MAXVALUE 2147483647
	START 1
	CACHE 1
	NO CYCLE;
-- DROP SEQUENCE thp_alternate_units_id_seq;

CREATE SEQUENCE thp_alternate_units_id_seq
	INCREMENT BY 1
	MINVALUE 1
	MAXVALUE 2147483647
	START 1
	CACHE 1
	NO CYCLE;
-- DROP SEQUENCE traditional_health_parameters_id_seq;

CREATE SEQUENCE traditional_health_parameters_id_seq
	INCREMENT BY 1
	MINVALUE 1
	MAXVALUE 2147483647
	START 1
	CACHE 1
	NO CYCLE;
-- DROP SEQUENCE unclassified_files_id_seq;

CREATE SEQUENCE unclassified_files_id_seq
	INCREMENT BY 1
	MINVALUE 1
	MAXVALUE 2147483647
	START 1
	CACHE 1
	NO CYCLE;
-- DROP SEQUENCE vaccinations_id_seq;

CREATE SEQUENCE vaccinations_id_seq
	INCREMENT BY 1
	MINVALUE 1
	MAXVALUE 2147483647
	START 1
	CACHE 1
	NO CYCLE;
-- DROP SEQUENCE vital_reading_id_seq;

CREATE SEQUENCE vital_reading_id_seq
	INCREMENT BY 1
	MINVALUE 1
	MAXVALUE 9223372036854775807
	START 1
	CACHE 1
	NO CYCLE;-- public.ai_alembic_version definition

-- Drop table

-- DROP TABLE ai_alembic_version;

CREATE TABLE ai_alembic_version (
	version_num varchar(32) NOT NULL,
	CONSTRAINT ai_alembic_version_pkc PRIMARY KEY (version_num)
);


-- public.ai_processing_runs definition

-- Drop table

-- DROP TABLE ai_processing_runs;

CREATE TABLE ai_processing_runs (
	id uuid DEFAULT gen_random_uuid() NOT NULL,
	requested_by_user_id uuid NULL,
	caller varchar(64) NOT NULL,
	request_id varchar(128) NULL,
	force_reprocess bool DEFAULT false NOT NULL,
	created_at timestamptz DEFAULT now() NOT NULL,
	updated_at timestamptz DEFAULT now() NOT NULL,
	CONSTRAINT ai_processing_runs_pkey PRIMARY KEY (id)
);
CREATE INDEX ix_ai_processing_runs_created_at ON public.ai_processing_runs USING btree (created_at);


-- public.doctor_department definition

-- Drop table

-- DROP TABLE doctor_department;

CREATE TABLE doctor_department (
	id serial4 NOT NULL,
	"name" varchar(255) NOT NULL,
	CONSTRAINT pk_doctor_department PRIMARY KEY (id),
	CONSTRAINT uq_doctor_department_name UNIQUE (name)
);


-- public.doctor_specialization definition

-- Drop table

-- DROP TABLE doctor_specialization;

CREATE TABLE doctor_specialization (
	id serial4 NOT NULL,
	"name" varchar(255) NOT NULL,
	CONSTRAINT pk_doctor_specialization PRIMARY KEY (id),
	CONSTRAINT uq_doctor_specialization_name UNIQUE (name)
);


-- public.flyway_schema_history definition

-- Drop table

-- DROP TABLE flyway_schema_history;

CREATE TABLE flyway_schema_history (
	installed_rank int4 NOT NULL,
	"version" varchar(50) NULL,
	description varchar(200) NOT NULL,
	"type" varchar(20) NOT NULL,
	script varchar(1000) NOT NULL,
	checksum int4 NULL,
	installed_by varchar(100) NOT NULL,
	installed_on timestamp DEFAULT now() NOT NULL,
	execution_time int4 NOT NULL,
	success bool NOT NULL,
	CONSTRAINT flyway_schema_history_pk PRIMARY KEY (installed_rank)
);
CREATE INDEX flyway_schema_history_s_idx ON public.flyway_schema_history USING btree (success);


-- public.hospital definition

-- Drop table

-- DROP TABLE hospital;

CREATE TABLE hospital (
	id serial4 NOT NULL,
	"name" varchar(255) NOT NULL,
	logo varchar(500) NULL,
	address text NULL,
	CONSTRAINT pk_hospital PRIMARY KEY (id),
	CONSTRAINT uq_hospital_name UNIQUE (name)
);
CREATE INDEX idx_hospital_name ON public.hospital USING btree (name);


-- public.insurance_provider definition

-- Drop table

-- DROP TABLE insurance_provider;

CREATE TABLE insurance_provider (
	id serial4 NOT NULL,
	"name" varchar(255) NOT NULL,
	logo varchar(500) NULL,
	address text NULL,
	CONSTRAINT pk_insurance_provider PRIMARY KEY (id),
	CONSTRAINT uq_insurance_provider_name UNIQUE (name)
);


-- public.medicine_manufacturer definition

-- Drop table

-- DROP TABLE medicine_manufacturer;

CREATE TABLE medicine_manufacturer (
	id serial4 NOT NULL,
	"name" varchar(255) NOT NULL,
	logo varchar(500) NULL,
	CONSTRAINT pk_medicine_manufacturer PRIMARY KEY (id),
	CONSTRAINT uq_medicine_manufacturer_name UNIQUE (name)
);


-- public.relations definition

-- Drop table

-- DROP TABLE relations;

CREATE TABLE relations (
	id serial4 NOT NULL,
	"name" varchar(100) NOT NULL,
	inverse varchar(100) NOT NULL,
	CONSTRAINT pk_relations PRIMARY KEY (id)
);


-- public.traditional_health_parameters definition

-- Drop table

-- DROP TABLE traditional_health_parameters;

CREATE TABLE traditional_health_parameters (
	id serial4 NOT NULL,
	"name" varchar(100) NOT NULL,
	description text NULL,
	units varchar(25) NOT NULL,
	approved bool DEFAULT false NULL,
	visible bool DEFAULT false NULL,
	aliases _varchar NULL,
	CONSTRAINT traditional_health_parameters_pkey PRIMARY KEY (id)
);
CREATE INDEX idx_thp_approved_visible ON public.traditional_health_parameters USING btree (approved, visible);
CREATE UNIQUE INDEX idx_thp_name ON public.traditional_health_parameters USING btree (name);


-- public.ai_processing_run_items definition

-- Drop table

-- DROP TABLE ai_processing_run_items;

CREATE TABLE ai_processing_run_items (
	id uuid DEFAULT gen_random_uuid() NOT NULL,
	run_id uuid NOT NULL,
	document_id int4 NOT NULL,
	section_row_id int4 NULL,
	status varchar(32) NOT NULL,
	attempt_count int4 DEFAULT 0 NOT NULL,
	content_hash varchar(128) NULL,
	last_error_code varchar(64) NULL,
	last_error_message text NULL,
	created_at timestamptz DEFAULT now() NOT NULL,
	updated_at timestamptz DEFAULT now() NOT NULL,
	started_at timestamptz NULL,
	completed_at timestamptz NULL,
	filed_section varchar(32) NULL,
	intended_section varchar(32) NULL,
	source_key varchar(500) NULL,
	CONSTRAINT ai_processing_run_items_pkey PRIMARY KEY (id),
	CONSTRAINT ck_ai_processing_run_items_status CHECK (((status)::text = ANY ((ARRAY['pending'::character varying, 'queued'::character varying, 'processing'::character varying, 'classifying'::character varying, 'extracting'::character varying, 'generating_insights'::character varying, 'completed'::character varying, 'failed'::character varying, 'rejected'::character varying, 'cancelled'::character varying])::text[]))),
	CONSTRAINT ai_processing_run_items_run_id_fkey FOREIGN KEY (run_id) REFERENCES ai_processing_runs(id) ON DELETE CASCADE
);
CREATE INDEX ix_ai_run_items_document_id ON public.ai_processing_run_items USING btree (document_id);
CREATE INDEX ix_ai_run_items_run_id ON public.ai_processing_run_items USING btree (run_id);
CREATE INDEX ix_ai_run_items_status ON public.ai_processing_run_items USING btree (status);
CREATE UNIQUE INDEX uq_ai_run_items_active_document ON public.ai_processing_run_items USING btree (document_id) WHERE ((status)::text = ANY ((ARRAY['classifying'::character varying, 'extracting'::character varying, 'generating_insights'::character varying, 'pending'::character varying, 'processing'::character varying, 'queued'::character varying])::text[]));


-- public.ai_report_classifications definition

-- Drop table

-- DROP TABLE ai_report_classifications;

CREATE TABLE ai_report_classifications (
	id uuid DEFAULT gen_random_uuid() NOT NULL,
	run_item_id uuid NOT NULL,
	document_id int4 NOT NULL,
	"section" varchar(32) NOT NULL,
	title varchar(512) NOT NULL,
	confidence numeric(4, 3) NOT NULL,
	reasoning text NULL,
	prompt_version varchar(32) NOT NULL,
	schema_version varchar(32) NOT NULL,
	created_at timestamptz DEFAULT now() NOT NULL,
	updated_at timestamptz DEFAULT now() NOT NULL,
	CONSTRAINT ai_report_classifications_pkey PRIMARY KEY (id),
	CONSTRAINT ai_report_classifications_run_item_id_key UNIQUE (run_item_id),
	CONSTRAINT ai_report_classifications_run_item_id_fkey FOREIGN KEY (run_item_id) REFERENCES ai_processing_run_items(id) ON DELETE CASCADE
);


-- public.ai_report_extractions definition

-- Drop table

-- DROP TABLE ai_report_extractions;

CREATE TABLE ai_report_extractions (
	id uuid DEFAULT gen_random_uuid() NOT NULL,
	run_item_id uuid NOT NULL,
	document_id int4 NOT NULL,
	"data" jsonb NOT NULL,
	prompt_version varchar(32) NOT NULL,
	schema_version varchar(32) NOT NULL,
	created_at timestamptz DEFAULT now() NOT NULL,
	updated_at timestamptz DEFAULT now() NOT NULL,
	CONSTRAINT ai_report_extractions_pkey PRIMARY KEY (id),
	CONSTRAINT ai_report_extractions_run_item_id_key UNIQUE (run_item_id),
	CONSTRAINT ai_report_extractions_run_item_id_fkey FOREIGN KEY (run_item_id) REFERENCES ai_processing_run_items(id) ON DELETE CASCADE
);


-- public.ai_report_insights definition

-- Drop table

-- DROP TABLE ai_report_insights;

CREATE TABLE ai_report_insights (
	id uuid DEFAULT gen_random_uuid() NOT NULL,
	run_item_id uuid NOT NULL,
	document_id int4 NOT NULL,
	"data" jsonb NOT NULL,
	prompt_version varchar(32) NOT NULL,
	schema_version varchar(32) NOT NULL,
	created_at timestamptz DEFAULT now() NOT NULL,
	updated_at timestamptz DEFAULT now() NOT NULL,
	CONSTRAINT ai_report_insights_pkey PRIMARY KEY (id),
	CONSTRAINT ai_report_insights_run_item_id_key UNIQUE (run_item_id),
	CONSTRAINT ai_report_insights_run_item_id_fkey FOREIGN KEY (run_item_id) REFERENCES ai_processing_run_items(id) ON DELETE CASCADE
);


-- public.ai_section_extractions definition

-- Drop table

-- DROP TABLE ai_section_extractions;

CREATE TABLE ai_section_extractions (
	id uuid DEFAULT gen_random_uuid() NOT NULL,
	run_item_id uuid NOT NULL,
	document_id int4 NOT NULL,
	"section" varchar(32) NOT NULL,
	"data" jsonb NOT NULL,
	prompt_version varchar(32) NOT NULL,
	schema_version varchar(32) NOT NULL,
	created_at timestamptz DEFAULT now() NOT NULL,
	updated_at timestamptz DEFAULT now() NOT NULL,
	CONSTRAINT ai_section_extractions_pkey PRIMARY KEY (id),
	CONSTRAINT ai_section_extractions_run_item_id_key UNIQUE (run_item_id),
	CONSTRAINT ai_section_extractions_run_item_id_fkey FOREIGN KEY (run_item_id) REFERENCES ai_processing_run_items(id) ON DELETE CASCADE
);
CREATE INDEX ix_ai_section_extractions_section ON public.ai_section_extractions USING btree (section);


-- public.ai_thp_fallbacks definition

-- Drop table

-- DROP TABLE ai_thp_fallbacks;

CREATE TABLE ai_thp_fallbacks (
	id uuid DEFAULT gen_random_uuid() NOT NULL,
	run_item_id uuid NOT NULL,
	document_id int4 NOT NULL,
	test_name varchar(256) NOT NULL,
	matched_parameter varchar(256) NULL,
	group_attempted varchar(64) NULL,
	reason varchar(32) NOT NULL,
	patient_age varchar(32) NULL,
	patient_gender varchar(32) NULL,
	report_reference_range varchar(512) NULL,
	created_at timestamptz DEFAULT now() NOT NULL,
	report_unit varchar(64) NULL,
	CONSTRAINT ai_thp_fallbacks_pkey PRIMARY KEY (id),
	CONSTRAINT ai_thp_fallbacks_run_item_id_fkey FOREIGN KEY (run_item_id) REFERENCES ai_processing_run_items(id) ON DELETE CASCADE
);
CREATE INDEX ix_ai_thp_fallbacks_matched_parameter ON public.ai_thp_fallbacks USING btree (matched_parameter);
CREATE INDEX ix_ai_thp_fallbacks_reason ON public.ai_thp_fallbacks USING btree (reason);
CREATE INDEX ix_ai_thp_fallbacks_run_item_id ON public.ai_thp_fallbacks USING btree (run_item_id);


-- public.medicine_master definition

-- Drop table

-- DROP TABLE medicine_master;

CREATE TABLE medicine_master (
	id serial4 NOT NULL,
	"name" varchar(255) NOT NULL,
	dosage_form public."dosage_form_enum" NULL,
	description text NULL,
	used_for _text NULL,
	category _text NULL,
	sub_category _text NULL,
	strength varchar(100) NULL,
	manufacturer int4 NULL,
	prescription_reqd bool DEFAULT false NULL,
	side_effects text NULL,
	CONSTRAINT pk_medicine_master PRIMARY KEY (id),
	CONSTRAINT fk_medicine_master_medicine_manufacturer FOREIGN KEY (manufacturer) REFERENCES medicine_manufacturer(id) ON DELETE SET NULL
);
CREATE INDEX idx_medicine_master_category ON public.medicine_master USING gin (category);
CREATE INDEX idx_medicine_master_manufacturer ON public.medicine_master USING btree (manufacturer);
CREATE INDEX idx_medicine_master_name ON public.medicine_master USING btree (name);


-- public.thp_age_range definition

-- Drop table

-- DROP TABLE thp_age_range;

CREATE TABLE thp_age_range (
	id serial4 NOT NULL,
	thp_id int4 NOT NULL,
	age_min int4 NOT NULL,
	age_max int4 NOT NULL,
	min float8 NOT NULL,
	low_danger float8 NOT NULL,
	low_warn float8 NOT NULL,
	ideal float8 NOT NULL,
	high_warn float8 NOT NULL,
	high_danger float8 NOT NULL,
	max float8 NOT NULL,
	CONSTRAINT chk_age_range CHECK ((age_min <= age_max)),
	CONSTRAINT chk_value_order CHECK (((min <= low_danger) AND (low_danger <= low_warn) AND (low_warn <= ideal) AND (ideal <= high_warn) AND (high_warn <= high_danger) AND (high_danger <= max))),
	CONSTRAINT thp_age_range_pkey PRIMARY KEY (id),
	CONSTRAINT fk_thp_age_range_thp FOREIGN KEY (thp_id) REFERENCES traditional_health_parameters(id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX idx_age_range_thp_bounds ON public.thp_age_range USING btree (thp_id, age_min, age_max);
CREATE INDEX idx_age_range_thp_id ON public.thp_age_range USING btree (thp_id);


-- public.thp_alternate_units definition

-- Drop table

-- DROP TABLE thp_alternate_units;

CREATE TABLE thp_alternate_units (
	id serial4 NOT NULL,
	thp_id int4 NOT NULL,
	"name" varchar(100) NOT NULL,
	multiplier float8 DEFAULT 1 NOT NULL,
	offset_value float8 DEFAULT 0 NOT NULL,
	CONSTRAINT thp_alternate_units_pkey PRIMARY KEY (id),
	CONSTRAINT uq_alt_units_thp_name UNIQUE (thp_id, name),
	CONSTRAINT fk_alt_units_thp FOREIGN KEY (thp_id) REFERENCES traditional_health_parameters(id) ON DELETE CASCADE
);
CREATE INDEX idx_alt_units_thp_id ON public.thp_alternate_units USING btree (thp_id);


-- public."user" definition

-- Drop table

-- DROP TABLE "user";

CREATE TABLE "user" (
	id uuid DEFAULT gen_random_uuid() NOT NULL,
	"name" varchar(255) NOT NULL,
	email varchar(255) NOT NULL,
	mobile varchar(20) NULL,
	user_name varchar(20) NOT NULL,
	blood_group public."blood_group_enum" NULL,
	"password" varchar(255) NULL,
	dob date NULL,
	gender public."gender_enum" NULL,
	user_type public."user_type_enum" DEFAULT 'user'::user_type_enum NULL,
	health_card_number varchar(100) NOT NULL,
	image varchar(500) NULL,
	email_verified bool DEFAULT false NOT NULL,
	mobile_verified bool DEFAULT false NULL,
	active bool DEFAULT true NOT NULL,
	terms_accepted bool DEFAULT false NOT NULL,
	filecount int4 DEFAULT 5 NULL,
	hashcode varchar(255) NOT NULL,
	share_token varchar(255) NULL,
	created_at timestamptz DEFAULT now() NULL,
	created_by uuid NULL,
	updated_at timestamptz DEFAULT now() NULL,
	updated_by uuid NULL,
	last_login_at timestamptz NULL,
	last_login_provider public."login_provider_enum" NULL,
	backup_email varchar(255) NULL,
	provisional bool DEFAULT false NULL,
	CONSTRAINT pk_user PRIMARY KEY (id),
	CONSTRAINT uq_user_email UNIQUE (email),
	CONSTRAINT uq_user_hashcode UNIQUE (hashcode),
	CONSTRAINT uq_user_health_card_number UNIQUE (health_card_number),
	CONSTRAINT uq_user_share_token UNIQUE (share_token),
	CONSTRAINT uq_user_user_name UNIQUE (user_name),
	CONSTRAINT fk_user_created_by_user FOREIGN KEY (created_by) REFERENCES "user"(id) ON DELETE SET NULL,
	CONSTRAINT fk_user_updated_by_user FOREIGN KEY (updated_by) REFERENCES "user"(id) ON DELETE SET NULL
);
CREATE INDEX idx_user_created_by ON public."user" USING btree (created_by);
CREATE INDEX idx_user_email ON public."user" USING btree (email);
CREATE INDEX idx_user_mobile ON public."user" USING btree (mobile);
CREATE INDEX idx_user_name ON public."user" USING btree (name);
CREATE INDEX idx_user_user_type ON public."user" USING btree (user_type);


-- public.vaccinations definition

-- Drop table

-- DROP TABLE vaccinations;

CREATE TABLE vaccinations (
	id serial4 NOT NULL,
	user_id uuid NOT NULL,
	filepath varchar(500) NOT NULL,
	hospital int4 NULL,
	"content" jsonb NULL,
	private bool DEFAULT false NULL,
	next_due_on timestamptz NULL,
	created_by uuid NULL,
	created_at timestamptz DEFAULT now() NULL,
	CONSTRAINT pk_vaccinations PRIMARY KEY (id),
	CONSTRAINT fk_vaccinations_created_by_user FOREIGN KEY (created_by) REFERENCES "user"(id) ON DELETE SET NULL,
	CONSTRAINT fk_vaccinations_hospital FOREIGN KEY (hospital) REFERENCES hospital(id) ON DELETE SET NULL,
	CONSTRAINT fk_vaccinations_user FOREIGN KEY (user_id) REFERENCES "user"(id) ON DELETE CASCADE
);
CREATE INDEX idx_vaccinations_created_by ON public.vaccinations USING btree (created_by);
CREATE INDEX idx_vaccinations_hospital ON public.vaccinations USING btree (hospital);
CREATE INDEX idx_vaccinations_next_due ON public.vaccinations USING btree (next_due_on) WHERE (next_due_on IS NOT NULL);
CREATE INDEX idx_vaccinations_owner_vis ON public.vaccinations USING btree (user_id, private);
CREATE INDEX idx_vaccinations_user_id ON public.vaccinations USING btree (user_id);


-- public.vital_reading definition

-- Drop table

-- DROP TABLE vital_reading;

CREATE TABLE vital_reading (
	id bigserial NOT NULL,
	user_id uuid NOT NULL,
	vital_type public."vital_type_enum" NOT NULL,
	value_primary numeric(6, 2) NOT NULL,
	value_secondary numeric(6, 2) NULL,
	unit varchar(20) NULL,
	recorded_at timestamptz DEFAULT now() NOT NULL,
	"source" public."vital_source_enum" DEFAULT 'manual'::vital_source_enum NOT NULL,
	notes varchar(255) NULL,
	CONSTRAINT chk_vital_reading_bp_secondary CHECK ((((vital_type = 'blood_pressure'::vital_type_enum) AND (value_secondary IS NOT NULL)) OR ((vital_type <> 'blood_pressure'::vital_type_enum) AND (value_secondary IS NULL)))),
	CONSTRAINT pk_vital_reading PRIMARY KEY (id),
	CONSTRAINT fk_vital_reading_user FOREIGN KEY (user_id) REFERENCES "user"(id) ON DELETE CASCADE
);
CREATE INDEX idx_vital_reading_recorded_at ON public.vital_reading USING btree (recorded_at DESC);
CREATE INDEX idx_vital_reading_user_type_date ON public.vital_reading USING btree (user_id, vital_type, recorded_at DESC);


-- public.ai_process_logs definition

-- Drop table

-- DROP TABLE ai_process_logs;

CREATE TABLE ai_process_logs (
	id uuid DEFAULT gen_random_uuid() NOT NULL,
	run_item_id uuid NOT NULL,
	document_id int4 NOT NULL,
	stage varchar(32) NOT NULL,
	attempt int4 NOT NULL,
	provider varchar(64) NOT NULL,
	model varchar(128) NOT NULL,
	prompt_version varchar(32) NOT NULL,
	schema_version varchar(32) NOT NULL,
	input_tokens int4 DEFAULT 0 NOT NULL,
	output_tokens int4 DEFAULT 0 NOT NULL,
	cache_read_input_tokens int4 DEFAULT 0 NOT NULL,
	cache_creation_input_tokens int4 DEFAULT 0 NOT NULL,
	estimated_cost_usd numeric(12, 6) DEFAULT 0 NOT NULL,
	duration_ms int4 DEFAULT 0 NOT NULL,
	outcome varchar(32) NOT NULL,
	error_code varchar(64) NULL,
	error_detail text NULL,
	created_at timestamptz DEFAULT now() NOT NULL,
	CONSTRAINT ai_process_logs_pkey PRIMARY KEY (id),
	CONSTRAINT uq_ai_process_logs_item_stage_attempt UNIQUE (run_item_id, stage, attempt),
	CONSTRAINT ai_process_logs_run_item_id_fkey FOREIGN KEY (run_item_id) REFERENCES ai_processing_run_items(id) ON DELETE CASCADE
);


-- public.bills definition

-- Drop table

-- DROP TABLE bills;

CREATE TABLE bills (
	id serial4 NOT NULL,
	user_id uuid NOT NULL,
	filepath varchar(500) NOT NULL,
	hospital int4 NULL,
	amount numeric(10, 2) NULL,
	amount_due numeric(10, 2) NULL,
	amount_currency public."currency_enum" NULL,
	"content" jsonb NULL,
	private bool DEFAULT false NULL,
	created_by uuid NULL,
	created_at timestamptz DEFAULT now() NULL,
	CONSTRAINT pk_bills PRIMARY KEY (id),
	CONSTRAINT fk_bills_created_by_user FOREIGN KEY (created_by) REFERENCES "user"(id) ON DELETE SET NULL,
	CONSTRAINT fk_bills_hospital FOREIGN KEY (hospital) REFERENCES hospital(id) ON DELETE SET NULL,
	CONSTRAINT fk_bills_user FOREIGN KEY (user_id) REFERENCES "user"(id) ON DELETE CASCADE
);
CREATE INDEX idx_bills_created_by ON public.bills USING btree (created_by);
CREATE INDEX idx_bills_hospital ON public.bills USING btree (hospital);
CREATE INDEX idx_bills_owner_vis ON public.bills USING btree (user_id, private);
CREATE INDEX idx_bills_user_id ON public.bills USING btree (user_id);


-- public.body_measurement definition

-- Drop table

-- DROP TABLE body_measurement;

CREATE TABLE body_measurement (
	id serial4 NOT NULL,
	user_id uuid NOT NULL,
	"type" public."body_measurement_type_enum" NOT NULL,
	value float8 NOT NULL,
	"date" timestamptz DEFAULT now() NULL,
	goal float8 NULL,
	goal_set_on timestamptz NULL,
	CONSTRAINT pk_body_measurement PRIMARY KEY (id),
	CONSTRAINT fk_body_measurement_user FOREIGN KEY (user_id) REFERENCES "user"(id) ON DELETE CASCADE
);
CREATE INDEX idx_body_measurement_date ON public.body_measurement USING btree (date DESC);
CREATE INDEX idx_body_measurement_user_id ON public.body_measurement USING btree (user_id);
CREATE INDEX idx_body_measurement_user_type ON public.body_measurement USING btree (user_id, type);
CREATE INDEX idx_body_measurement_user_type_date ON public.body_measurement USING btree (user_id, type, date DESC) INCLUDE (value, goal);


-- public.doctor definition

-- Drop table

-- DROP TABLE doctor;

CREATE TABLE doctor (
	id serial4 NOT NULL,
	user_id uuid NOT NULL,
	verified bool DEFAULT false NULL,
	specialization_id int4 NULL,
	department_id int4 NULL,
	hospital int4 NULL,
	CONSTRAINT pk_doctor PRIMARY KEY (id),
	CONSTRAINT fk_doctor_doctor_department FOREIGN KEY (department_id) REFERENCES doctor_department(id) ON DELETE SET NULL,
	CONSTRAINT fk_doctor_doctor_specialization FOREIGN KEY (specialization_id) REFERENCES doctor_specialization(id) ON DELETE SET NULL,
	CONSTRAINT fk_doctor_hospital FOREIGN KEY (hospital) REFERENCES hospital(id) ON DELETE SET NULL,
	CONSTRAINT fk_doctor_user FOREIGN KEY (user_id) REFERENCES "user"(id) ON DELETE CASCADE
);
CREATE INDEX idx_doctor_department_id ON public.doctor USING btree (department_id);
CREATE INDEX idx_doctor_hospital ON public.doctor USING btree (hospital);
CREATE INDEX idx_doctor_specialization_id ON public.doctor USING btree (specialization_id);
CREATE INDEX idx_doctor_user_id ON public.doctor USING btree (user_id);


-- public.doctor_connect definition

-- Drop table

-- DROP TABLE doctor_connect;

CREATE TABLE doctor_connect (
	id int4 GENERATED BY DEFAULT AS IDENTITY( INCREMENT BY 1 MINVALUE 1 MAXVALUE 2147483647 START 1 CACHE 1 NO CYCLE) NOT NULL,
	user_id uuid NOT NULL,
	doctor_id int4 NOT NULL,
	doctor_acceptance bool NULL,
	user_acceptance bool NULL,
	created_at timestamptz DEFAULT now() NULL,
	created_by public."created_by_doctor_connect_enum" NULL,
	CONSTRAINT idx_doctor_connect_pair UNIQUE (user_id, doctor_id),
	CONSTRAINT pk_doctor_connect PRIMARY KEY (id),
	CONSTRAINT fk_doctor_connect_doctor FOREIGN KEY (doctor_id) REFERENCES doctor(id) ON DELETE CASCADE,
	CONSTRAINT fk_doctor_connect_user FOREIGN KEY (user_id) REFERENCES "user"(id) ON DELETE CASCADE
);
CREATE INDEX idx_doctor_connect_doctor_id ON public.doctor_connect USING btree (doctor_id);
CREATE INDEX idx_doctor_connect_user_id ON public.doctor_connect USING btree (user_id);


-- public.event_log definition

-- Drop table

-- DROP TABLE event_log;

CREATE TABLE event_log (
	id bigserial NOT NULL,
	user_id uuid NOT NULL,
	event_type varchar(100) NOT NULL,
	payload jsonb NULL,
	created_at timestamptz DEFAULT now() NULL,
	CONSTRAINT pk_event_log PRIMARY KEY (id),
	CONSTRAINT fk_event_log_user FOREIGN KEY (user_id) REFERENCES "user"(id) ON DELETE CASCADE
);
CREATE INDEX idx_event_log_created_at ON public.event_log USING btree (created_at DESC);
CREATE INDEX idx_event_log_user_created ON public.event_log USING btree (user_id, created_at);
CREATE INDEX idx_event_log_user_id ON public.event_log USING btree (user_id);
CREATE INDEX idx_event_log_user_type_at ON public.event_log USING btree (user_id, event_type, created_at DESC);


-- public.family_connect definition

-- Drop table

-- DROP TABLE family_connect;

CREATE TABLE family_connect (
	id serial4 NOT NULL,
	requester_id uuid NOT NULL,
	acceptor_id uuid NOT NULL,
	accepted bool DEFAULT false NOT NULL,
	req_file_share bool DEFAULT true NOT NULL,
	created_at timestamptz DEFAULT now() NULL,
	acc_file_share bool NULL,
	relation_id int4 NULL,
	CONSTRAINT pk_family_connect PRIMARY KEY (id),
	CONSTRAINT uq_family_connect_pair UNIQUE (requester_id, acceptor_id),
	CONSTRAINT fk_family_connect_acceptor_user FOREIGN KEY (acceptor_id) REFERENCES "user"(id) ON DELETE CASCADE,
	CONSTRAINT fk_family_connect_relation FOREIGN KEY (relation_id) REFERENCES relations(id) ON DELETE SET NULL,
	CONSTRAINT fk_family_connect_requester_user FOREIGN KEY (requester_id) REFERENCES "user"(id) ON DELETE CASCADE
);
CREATE INDEX idx_family_connect_acceptor ON public.family_connect USING btree (acceptor_id);
CREATE INDEX idx_family_connect_requester ON public.family_connect USING btree (requester_id);


-- public.family_file_access definition

-- Drop table

-- DROP TABLE family_file_access;

CREATE TABLE family_file_access (
	id serial4 NOT NULL,
	fc_id int4 NOT NULL,
	resource_type public."resource_type_enum" NOT NULL,
	resource_id int4 NOT NULL,
	allowed bool DEFAULT true NOT NULL,
	updated_at timestamptz DEFAULT now() NULL,
	CONSTRAINT pk_family_file_access PRIMARY KEY (id),
	CONSTRAINT uq_family_file_access_record UNIQUE (fc_id, resource_type, resource_id),
	CONSTRAINT fk_family_file_access_family_connect FOREIGN KEY (fc_id) REFERENCES family_connect(id) ON DELETE CASCADE
);
CREATE INDEX idx_family_file_access_fc_id ON public.family_file_access USING btree (fc_id);
CREATE INDEX idx_family_file_access_resource ON public.family_file_access USING btree (resource_type, resource_id);


-- public.insurance definition

-- Drop table

-- DROP TABLE insurance;

CREATE TABLE insurance (
	id serial4 NOT NULL,
	user_id uuid NOT NULL,
	filepath varchar(500) NULL,
	provider int4 NULL,
	"content" jsonb NULL,
	private bool DEFAULT false NULL,
	created_by uuid NULL,
	created_at timestamptz DEFAULT now() NULL,
	CONSTRAINT pk_insurance PRIMARY KEY (id),
	CONSTRAINT fk_insurance_created_by_user FOREIGN KEY (created_by) REFERENCES "user"(id) ON DELETE SET NULL,
	CONSTRAINT fk_insurance_insurance_provider FOREIGN KEY (provider) REFERENCES insurance_provider(id) ON DELETE SET NULL,
	CONSTRAINT fk_insurance_user FOREIGN KEY (user_id) REFERENCES "user"(id) ON DELETE CASCADE
);
CREATE INDEX idx_insurance_created_by ON public.insurance USING btree (created_by);
CREATE INDEX idx_insurance_owner_vis ON public.insurance USING btree (user_id, private);
CREATE INDEX idx_insurance_provider ON public.insurance USING btree (provider);
CREATE INDEX idx_insurance_user_id ON public.insurance USING btree (user_id);


-- public.lifestyle_log definition

-- Drop table

-- DROP TABLE lifestyle_log;

CREATE TABLE lifestyle_log (
	id bigserial NOT NULL,
	user_id uuid NOT NULL,
	log_type public."lifestyle_log_type_enum" NOT NULL,
	quantity numeric(6, 2) NOT NULL,
	unit varchar(20) NOT NULL,
	metadata jsonb NULL,
	logged_at timestamptz DEFAULT now() NOT NULL,
	CONSTRAINT pk_lifestyle_log PRIMARY KEY (id),
	CONSTRAINT fk_lifestyle_log_user FOREIGN KEY (user_id) REFERENCES "user"(id) ON DELETE CASCADE
);
CREATE INDEX idx_lifestyle_log_user_date ON public.lifestyle_log USING btree (user_id, logged_at DESC);
CREATE INDEX idx_lifestyle_log_user_type_date ON public.lifestyle_log USING btree (user_id, log_type, logged_at DESC);


-- public.lifestyle_monthly_total definition

-- Drop table

-- DROP TABLE lifestyle_monthly_total;

CREATE TABLE lifestyle_monthly_total (
	user_id uuid NOT NULL,
	log_type public."lifestyle_log_type_enum" NOT NULL,
	bucket_start date NOT NULL,
	total numeric(12, 2) NOT NULL,
	entries int4 NOT NULL,
	days_counted int4 NOT NULL,
	CONSTRAINT pk_lifestyle_monthly_total PRIMARY KEY (user_id, log_type, bucket_start),
	CONSTRAINT fk_lifestyle_monthly_total_user FOREIGN KEY (user_id) REFERENCES "user"(id) ON DELETE CASCADE
);


-- public.lifestyle_weekly_total definition

-- Drop table

-- DROP TABLE lifestyle_weekly_total;

CREATE TABLE lifestyle_weekly_total (
	user_id uuid NOT NULL,
	log_type public."lifestyle_log_type_enum" NOT NULL,
	bucket_start date NOT NULL,
	total numeric(12, 2) NOT NULL,
	entries int4 NOT NULL,
	days_counted int4 NOT NULL,
	CONSTRAINT pk_lifestyle_weekly_total PRIMARY KEY (user_id, log_type, bucket_start),
	CONSTRAINT fk_lifestyle_weekly_total_user FOREIGN KEY (user_id) REFERENCES "user"(id) ON DELETE CASCADE
);


-- public.manual_tracking definition

-- Drop table

-- DROP TABLE manual_tracking;

CREATE TABLE manual_tracking (
	id serial4 NOT NULL,
	user_id uuid NOT NULL,
	"type" public."manual_tracking_type_enum" NOT NULL,
	value float8 NULL,
	daily_limit float8 NULL,
	goal float8 NULL,
	unit varchar(50) NULL,
	effective_from timestamptz DEFAULT now() NULL,
	created_at timestamptz DEFAULT now() NULL,
	CONSTRAINT pk_manual_tracking PRIMARY KEY (id),
	CONSTRAINT fk_manual_tracking_user FOREIGN KEY (user_id) REFERENCES "user"(id) ON DELETE CASCADE
);
CREATE INDEX idx_manual_tracking_type ON public.manual_tracking USING btree (type);
CREATE INDEX idx_manual_tracking_user_id ON public.manual_tracking USING btree (user_id);
CREATE INDEX idx_manual_tracking_user_type ON public.manual_tracking USING btree (user_id, type);
CREATE INDEX idx_manual_tracking_user_type_eff ON public.manual_tracking USING btree (user_id, type, effective_from DESC);


-- public.medical_condition definition

-- Drop table

-- DROP TABLE medical_condition;

CREATE TABLE medical_condition (
	id serial4 NOT NULL,
	user_id uuid NOT NULL,
	"name" varchar(255) NOT NULL,
	status public."medical_condition_status_enum" DEFAULT 'active'::medical_condition_status_enum NULL,
	started_on timestamptz NOT NULL,
	ended_on timestamptz NULL,
	private bool DEFAULT false NULL,
	created_by uuid NULL,
	created_at timestamptz DEFAULT now() NULL,
	CONSTRAINT pk_medical_condition PRIMARY KEY (id),
	CONSTRAINT fk_medical_condition_created_by_user FOREIGN KEY (created_by) REFERENCES "user"(id) ON DELETE SET NULL,
	CONSTRAINT fk_medical_condition_user FOREIGN KEY (user_id) REFERENCES "user"(id) ON DELETE CASCADE
);
CREATE INDEX idx_medical_condition_created_by ON public.medical_condition USING btree (created_by);
CREATE INDEX idx_medical_condition_owner_vis ON public.medical_condition USING btree (user_id, private);
CREATE INDEX idx_medical_condition_status ON public.medical_condition USING btree (status);
CREATE INDEX idx_medical_condition_user_id ON public.medical_condition USING btree (user_id);


-- public.period_tracking definition

-- Drop table

-- DROP TABLE period_tracking;

CREATE TABLE period_tracking (
	id serial4 NOT NULL,
	user_id uuid NOT NULL,
	start_date timestamptz NOT NULL,
	end_date timestamptz NULL,
	is_predicted bool DEFAULT false NULL,
	correct_prediction bool DEFAULT false NULL,
	cycle_length int4 NULL,
	flow_intensity public."flow_intensity_enum" NULL,
	symptoms jsonb NULL,
	CONSTRAINT pk_period_tracking PRIMARY KEY (id),
	CONSTRAINT fk_period_tracking_user FOREIGN KEY (user_id) REFERENCES "user"(id) ON DELETE CASCADE
);
CREATE INDEX idx_period_tracking_start_date ON public.period_tracking USING btree (start_date);
CREATE INDEX idx_period_tracking_symptoms ON public.period_tracking USING gin (symptoms);
CREATE INDEX idx_period_tracking_user_id ON public.period_tracking USING btree (user_id);
CREATE INDEX idx_period_tracking_user_start ON public.period_tracking USING btree (user_id, start_date DESC);


-- public.prescriptions definition

-- Drop table

-- DROP TABLE prescriptions;

CREATE TABLE prescriptions (
	id serial4 NOT NULL,
	user_id uuid NOT NULL,
	filepath varchar(500) NOT NULL,
	hospital int4 NULL,
	"content" jsonb NULL,
	private bool DEFAULT false NULL,
	created_by uuid NULL,
	created_at timestamptz DEFAULT now() NULL,
	CONSTRAINT pk_prescriptions PRIMARY KEY (id),
	CONSTRAINT fk_prescriptions_created_by_user FOREIGN KEY (created_by) REFERENCES "user"(id) ON DELETE SET NULL,
	CONSTRAINT fk_prescriptions_hospital FOREIGN KEY (hospital) REFERENCES hospital(id) ON DELETE SET NULL,
	CONSTRAINT fk_prescriptions_user FOREIGN KEY (user_id) REFERENCES "user"(id) ON DELETE CASCADE
);
CREATE INDEX idx_prescriptions_created_by ON public.prescriptions USING btree (created_by);
CREATE INDEX idx_prescriptions_hospital ON public.prescriptions USING btree (hospital);
CREATE INDEX idx_prescriptions_owner_vis ON public.prescriptions USING btree (user_id, private);
CREATE INDEX idx_prescriptions_user_id ON public.prescriptions USING btree (user_id);


-- public.refresh_token definition

-- Drop table

-- DROP TABLE refresh_token;

CREATE TABLE refresh_token (
	id int8 GENERATED BY DEFAULT AS IDENTITY( INCREMENT BY 1 MINVALUE 1 MAXVALUE 9223372036854775807 START 1 CACHE 1 NO CYCLE) NOT NULL,
	user_id uuid NOT NULL,
	created_at timestamptz(6) NULL,
	expires_at timestamptz(6) NOT NULL,
	revoked_at timestamptz(6) NULL,
	user_agent varchar(255) NOT NULL,
	ip_addr inet NOT NULL,
	token_hash varchar(64) NOT NULL,
	CONSTRAINT pk_refresh_token PRIMARY KEY (id),
	CONSTRAINT uq_refresh_token_hash UNIQUE (token_hash),
	CONSTRAINT fk_refresh_token_user FOREIGN KEY (user_id) REFERENCES "user"(id) ON DELETE CASCADE
);
CREATE INDEX idx_refresh_token_active ON public.refresh_token USING btree (user_id) WHERE (revoked_at IS NULL);
CREATE INDEX idx_refresh_token_expires ON public.refresh_token USING btree (expires_at);


-- public.reports definition

-- Drop table

-- DROP TABLE reports;

CREATE TABLE reports (
	id serial4 NOT NULL,
	user_id uuid NOT NULL,
	filepath varchar(500) NOT NULL,
	hospital int4 NULL,
	"content" jsonb NULL,
	private bool DEFAULT false NULL,
	created_by uuid NULL,
	created_at timestamptz DEFAULT now() NULL,
	CONSTRAINT pk_reports PRIMARY KEY (id),
	CONSTRAINT fk_reports_created_by_user FOREIGN KEY (created_by) REFERENCES "user"(id) ON DELETE SET NULL,
	CONSTRAINT fk_reports_hospital FOREIGN KEY (hospital) REFERENCES hospital(id) ON DELETE SET NULL,
	CONSTRAINT fk_reports_user FOREIGN KEY (user_id) REFERENCES "user"(id) ON DELETE CASCADE
);
CREATE INDEX idx_reports_created_by ON public.reports USING btree (created_by);
CREATE INDEX idx_reports_hospital ON public.reports USING btree (hospital);
CREATE INDEX idx_reports_owner_vis ON public.reports USING btree (user_id, private);
CREATE INDEX idx_reports_user_id ON public.reports USING btree (user_id);


-- public.scans_imaging definition

-- Drop table

-- DROP TABLE scans_imaging;

CREATE TABLE scans_imaging (
	id serial4 NOT NULL,
	user_id uuid NOT NULL,
	filepath varchar(500) NOT NULL,
	hospital int4 NULL,
	"content" jsonb NULL,
	private bool DEFAULT false NULL,
	created_by uuid NULL,
	created_at timestamptz DEFAULT now() NULL,
	CONSTRAINT pk_scans_imaging PRIMARY KEY (id),
	CONSTRAINT fk_scans_imaging_created_by_user FOREIGN KEY (created_by) REFERENCES "user"(id) ON DELETE SET NULL,
	CONSTRAINT fk_scans_imaging_hospital FOREIGN KEY (hospital) REFERENCES hospital(id) ON DELETE SET NULL,
	CONSTRAINT fk_scans_imaging_user FOREIGN KEY (user_id) REFERENCES "user"(id) ON DELETE CASCADE
);
CREATE INDEX idx_scans_imaging_created_by ON public.scans_imaging USING btree (created_by);
CREATE INDEX idx_scans_imaging_hospital ON public.scans_imaging USING btree (hospital);
CREATE INDEX idx_scans_imaging_owner_vis ON public.scans_imaging USING btree (user_id, private);
CREATE INDEX idx_scans_imaging_user_id ON public.scans_imaging USING btree (user_id);


-- public.sleep_sessions definition

-- Drop table

-- DROP TABLE sleep_sessions;

CREATE TABLE sleep_sessions (
	id bigserial NOT NULL,
	user_id uuid NOT NULL,
	started_at timestamptz NOT NULL,
	ended_at timestamptz NOT NULL,
	duration_minutes int4 GENERATED ALWAYS AS ((EXTRACT(epoch FROM ended_at - started_at) / 60::numeric)) STORED NULL,
	quality_score int2 NULL,
	stages jsonb NULL,
	"source" public."vital_source_enum" DEFAULT 'manual'::vital_source_enum NOT NULL,
	CONSTRAINT chk_sleep_sessions_duration CHECK ((ended_at > started_at)),
	CONSTRAINT pk_sleep_sessions PRIMARY KEY (id),
	CONSTRAINT fk_sleep_sessions_user FOREIGN KEY (user_id) REFERENCES "user"(id) ON DELETE CASCADE
);
CREATE INDEX idx_sleep_sessions_user_started ON public.sleep_sessions USING btree (user_id, started_at DESC);


-- public.sos_contact definition

-- Drop table

-- DROP TABLE sos_contact;

CREATE TABLE sos_contact (
	id serial4 NOT NULL,
	user_id uuid NULL,
	"name" varchar(255) NOT NULL,
	mobile varchar(20) NULL,
	email varchar(255) NULL,
	relation varchar(100) NULL,
	created_at timestamptz DEFAULT now() NULL,
	CONSTRAINT pk_sos_contact PRIMARY KEY (id),
	CONSTRAINT fk_sos_contact_user FOREIGN KEY (user_id) REFERENCES "user"(id) ON DELETE CASCADE
);
CREATE INDEX idx_sos_contact_user_id ON public.sos_contact USING btree (user_id);


-- public.subscriptions definition

-- Drop table

-- DROP TABLE subscriptions;

CREATE TABLE subscriptions (
	id serial4 NOT NULL,
	user_id uuid NOT NULL,
	purpose public."subscription_purpose_enum" NOT NULL,
	starts_at timestamptz NOT NULL,
	ends_at timestamptz NULL,
	payment_id varchar(100) NULL,
	payment_status public."payment_status_enum" DEFAULT 'pending'::payment_status_enum NOT NULL,
	payment_method public."payment_method_enum" NULL,
	created_at timestamptz DEFAULT now() NULL,
	created_by uuid NULL,
	CONSTRAINT pk_subscriptions PRIMARY KEY (id),
	CONSTRAINT fk_subscriptions_created_by_user FOREIGN KEY (created_by) REFERENCES "user"(id) ON DELETE SET NULL,
	CONSTRAINT fk_subscriptions_user FOREIGN KEY (user_id) REFERENCES "user"(id) ON DELETE CASCADE
);
CREATE INDEX idx_subscriptions_created_by ON public.subscriptions USING btree (created_by);
CREATE INDEX idx_subscriptions_payment_status ON public.subscriptions USING btree (payment_status);
CREATE INDEX idx_subscriptions_purpose ON public.subscriptions USING btree (purpose);
CREATE INDEX idx_subscriptions_user_id ON public.subscriptions USING btree (user_id);


-- public.unclassified_files definition

-- Drop table

-- DROP TABLE unclassified_files;

CREATE TABLE unclassified_files (
	id serial4 NOT NULL,
	user_id uuid NOT NULL,
	filepath varchar(500) NOT NULL,
	private bool DEFAULT false NULL,
	created_by uuid NULL,
	created_at timestamptz DEFAULT now() NULL,
	"name" varchar(255) NULL,
	CONSTRAINT pk_unclassified_files PRIMARY KEY (id),
	CONSTRAINT fk_unclassified_files_created_by_user FOREIGN KEY (created_by) REFERENCES "user"(id) ON DELETE SET NULL,
	CONSTRAINT fk_unclassified_files_user FOREIGN KEY (user_id) REFERENCES "user"(id) ON DELETE CASCADE
);
CREATE INDEX idx_unclassified_files_created_by ON public.unclassified_files USING btree (created_by);
CREATE INDEX idx_unclassified_files_owner_vis ON public.unclassified_files USING btree (user_id, private);
CREATE INDEX idx_unclassified_files_user_id ON public.unclassified_files USING btree (user_id);


-- public.medicine_tracking definition

-- Drop table

-- DROP TABLE medicine_tracking;

CREATE TABLE medicine_tracking (
	id serial4 NOT NULL,
	user_id uuid NOT NULL,
	medicine_id int4 NULL,
	"name" varchar(255) NOT NULL,
	prescription_id int4 NULL,
	notes text NULL,
	private bool DEFAULT false NOT NULL,
	schedule_pattern varchar(4) NOT NULL,
	dose_config jsonb NULL,
	day_pattern public."day_pattern_enum" NOT NULL,
	active_days varchar(20) DEFAULT '1111111'::bpchar NOT NULL,
	every_n_days int2 NULL,
	starts_at date DEFAULT CURRENT_DATE NOT NULL,
	ends_at date NULL,
	extended_till date NULL,
	effective_end date GENERATED ALWAYS AS (GREATEST(ends_at, extended_till)) STORED NULL,
	stopped_at date NULL,
	stop_reason varchar(255) NULL,
	is_prn bool DEFAULT false NOT NULL,
	strength varchar(100) NULL,
	dosage_form public."dosage_form_enum" NULL,
	stock_count int2 NULL,
	refill_remind bool DEFAULT false NOT NULL,
	change_log jsonb DEFAULT '[]'::jsonb NOT NULL,
	created_at timestamptz DEFAULT now() NULL,
	updated_at timestamptz DEFAULT now() NULL,
	dose_time jsonb NULL,
	CONSTRAINT chk_medicine_tracking_active_days_format CHECK (((active_days)::text ~ '^[01]{7}$'::text)),
	CONSTRAINT chk_medicine_tracking_interval_days CHECK (((day_pattern <> 'interval'::day_pattern_enum) OR (every_n_days IS NOT NULL))),
	CONSTRAINT pk_medicine_tracking PRIMARY KEY (id),
	CONSTRAINT fk_medicine_tracking_medicine_master FOREIGN KEY (medicine_id) REFERENCES medicine_master(id) ON DELETE SET NULL,
	CONSTRAINT fk_medicine_tracking_prescriptions FOREIGN KEY (prescription_id) REFERENCES prescriptions(id) ON DELETE SET NULL,
	CONSTRAINT fk_medicine_tracking_user FOREIGN KEY (user_id) REFERENCES "user"(id) ON DELETE CASCADE
);
CREATE INDEX idx_medicine_tracking_medicine_id ON public.medicine_tracking USING btree (medicine_id) WHERE (medicine_id IS NOT NULL);
CREATE INDEX idx_medicine_tracking_prescription_id ON public.medicine_tracking USING btree (prescription_id) WHERE (prescription_id IS NOT NULL);
CREATE INDEX idx_medicine_tracking_user_active ON public.medicine_tracking USING btree (user_id, starts_at, effective_end) WHERE ((stopped_at IS NULL) AND (is_prn = false));
CREATE INDEX idx_medicine_tracking_user_private ON public.medicine_tracking USING btree (user_id, private);
CREATE INDEX idx_medicine_tracking_user_prn ON public.medicine_tracking USING btree (user_id) WHERE ((is_prn = true) AND (stopped_at IS NULL));


-- public.medicine_dose_log definition

-- Drop table

-- DROP TABLE medicine_dose_log;

CREATE TABLE medicine_dose_log (
	id bigserial NOT NULL,
	tracking_id int4 NOT NULL,
	user_id uuid NOT NULL,
	scheduled_date date NOT NULL,
	slot bpchar(1) NOT NULL,
	scheduled_time time NULL,
	dose_qty numeric(5, 2) NULL,
	status public."dose_status_enum" DEFAULT 'pending'::dose_status_enum NOT NULL,
	taken_at timestamptz NULL,
	skip_reason varchar(255) NULL,
	is_prn bool DEFAULT false NOT NULL,
	created_at timestamptz DEFAULT now() NULL,
	CONSTRAINT chk_medicine_dose_log_slot CHECK ((slot = ANY (ARRAY['M'::bpchar, 'A'::bpchar, 'E'::bpchar, 'N'::bpchar]))),
	CONSTRAINT chk_medicine_dose_log_taken_at CHECK ((((status = 'taken'::dose_status_enum) AND (taken_at IS NOT NULL)) OR ((status <> 'taken'::dose_status_enum) AND (taken_at IS NULL)))),
	CONSTRAINT pk_medicine_dose_log PRIMARY KEY (id),
	CONSTRAINT uq_medicine_dose_log_slot UNIQUE (tracking_id, scheduled_date, slot),
	CONSTRAINT fk_medicine_dose_log_medicine_tracking FOREIGN KEY (tracking_id) REFERENCES medicine_tracking(id) ON DELETE CASCADE,
	CONSTRAINT fk_medicine_dose_log_user FOREIGN KEY (user_id) REFERENCES "user"(id) ON DELETE CASCADE
);
CREATE INDEX idx_medicine_dose_log_flip_job ON public.medicine_dose_log USING btree (scheduled_date, scheduled_time) WHERE (status = 'pending'::dose_status_enum);
CREATE INDEX idx_medicine_dose_log_tracking_date ON public.medicine_dose_log USING btree (tracking_id, scheduled_date DESC);
CREATE INDEX idx_medicine_dose_log_user_date ON public.medicine_dose_log USING btree (user_id, scheduled_date DESC) INCLUDE (status, dose_qty);
CREATE INDEX idx_medicine_dose_log_user_date_pending ON public.medicine_dose_log USING btree (user_id, scheduled_date DESC, scheduled_time) WHERE (status = 'pending'::dose_status_enum);