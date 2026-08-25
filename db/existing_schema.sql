-- db/existing_schema.sql
--
-- The production schema Davi does NOT own, as mhn-spring's Flyway chain
-- defines it. Composed from that chain in application order, NOT a pg_dump —
-- so it can be regenerated whenever the other team adds a migration, and it
-- says exactly which migration each object came from.
--
-- REGENERATE:  python -m scripts.build_existing_schema
--
-- SOURCE: D:\mhn-spring-main\src\main\resources\db\migration
-- CHAIN:  V1..V19, excluding V6 (Davi's own adopted
--         migration — the coexistence check exists to prove Davi's tables do
--         not collide with the ones Davi does not own, so including it would
--         collide by construction).
--
-- WHY THIS MATTERS. Until now this file was the V1 baseline alone. Everything
-- V7-V19 changed was invisible to every check in this repository — including
-- `traditional_health_parameters`, whose population in V18 silently made three
-- of Davi's patient-facing answers wrong. The tests could not have caught it:
-- tests/conftest.py builds its schema from Davi's OWN partial mappings, so a
-- column Davi does not map does not exist in any test database.
--
-- The DATA inserts (V17 drinks, V18 the 192-parameter reference catalogue,
-- V19 the drug merge) are kept deliberately. They are not decoration: the V18
-- catalogue is precisely what broke the value-check matcher, and a schema file
-- without it would let the same class of bug through again.
--
-- ============================================================================

-- ============================================================
-- V1__baseline.sql
-- ============================================================
-- =============================================================================
-- MyHealthNotion :: schema
--
-- Applied by Flyway at startup, before Hibernate. Keep it re-runnable: it is the
-- baseline every existing database has applied to it as a repair, not only the
-- seed for empty ones (see spring.flyway.baseline-version in application.properties).
-- Every object is created with IF NOT EXISTS (enums use a duplicate_object guard,
-- since PostgreSQL has no CREATE TYPE IF NOT EXISTS). Later changes go in a new
-- V2__*.sql beside this file rather than being edited in here.
--
-- Layout: each enum is declared immediately above the first table that uses it,
-- and tables are grouped by domain in foreign-key dependency order:
--   1. Reference / master data
--   2. Identity & auth
--   3. Doctor & family connections
--   4. Health records (documents)
--   5. Medicine tracking
--   6. Health tracking & measurements
--   7. Subscriptions & payments
--   8. Audit
-- =============================================================================


-- =============================================================================
-- 1. REFERENCE / MASTER DATA
-- =============================================================================

-- public.doctor_department definition

-- Drop table

-- DROP TABLE IF EXISTS doctor_department;

CREATE TABLE IF NOT EXISTS doctor_department (
	id serial4 NOT NULL,
	"name" varchar(255) NOT NULL,
	CONSTRAINT pk_doctor_department PRIMARY KEY (id),
	CONSTRAINT uq_doctor_department_name UNIQUE (name)
);


-- public.doctor_specialization definition

-- Drop table

-- DROP TABLE IF EXISTS doctor_specialization;

CREATE TABLE IF NOT EXISTS doctor_specialization (
	id serial4 NOT NULL,
	"name" varchar(255) NOT NULL,
	CONSTRAINT pk_doctor_specialization PRIMARY KEY (id),
	CONSTRAINT uq_doctor_specialization_name UNIQUE (name)
);


-- public.hospital definition

-- Drop table

-- DROP TABLE IF EXISTS hospital;

CREATE TABLE IF NOT EXISTS hospital (
	id serial4 NOT NULL,
	"name" varchar(255) NOT NULL,
	logo varchar(500) NULL,
	address text NULL,
	CONSTRAINT pk_hospital PRIMARY KEY (id),
	CONSTRAINT uq_hospital_name UNIQUE (name)
);
CREATE INDEX IF NOT EXISTS idx_hospital_name ON public.hospital USING btree (name);


-- public.insurance_provider definition

-- Drop table

-- DROP TABLE IF EXISTS insurance_provider;

CREATE TABLE IF NOT EXISTS insurance_provider (
	id serial4 NOT NULL,
	"name" varchar(255) NOT NULL,
	logo varchar(500) NULL,
	address text NULL,
	CONSTRAINT pk_insurance_provider PRIMARY KEY (id),
	CONSTRAINT uq_insurance_provider_name UNIQUE (name)
);


-- public.medicine_manufacturer definition

-- Drop table

-- DROP TABLE IF EXISTS medicine_manufacturer;

CREATE TABLE IF NOT EXISTS medicine_manufacturer (
	id serial4 NOT NULL,
	"name" varchar(255) NOT NULL,
	logo varchar(500) NULL,
	CONSTRAINT pk_medicine_manufacturer PRIMARY KEY (id),
	CONSTRAINT uq_medicine_manufacturer_name UNIQUE (name)
);


-- public.relations definition

-- Drop table

-- DROP TABLE IF EXISTS relations;

CREATE TABLE IF NOT EXISTS relations (
	id serial4 NOT NULL,
	"name" varchar(100) NOT NULL,
	inverse varchar(100) NOT NULL,
	CONSTRAINT pk_relations PRIMARY KEY (id)
);


-- public.medicine_master definition

-- Drop table

-- DROP TABLE IF EXISTS medicine_master;

-- used by: medicine_master.dosage_form, medicine_tracking.dosage_form
-- DROP TYPE IF EXISTS dosage_form_enum;
DO $$ BEGIN
	CREATE TYPE dosage_form_enum AS ENUM (
		'tablet','capsule','syrup','injection','drops','inhaler','patch','cream'
	);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS medicine_master (
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
CREATE INDEX IF NOT EXISTS idx_medicine_master_category ON public.medicine_master USING gin (category);
CREATE INDEX IF NOT EXISTS idx_medicine_master_manufacturer ON public.medicine_master USING btree (manufacturer);
CREATE INDEX IF NOT EXISTS idx_medicine_master_name ON public.medicine_master USING btree (name);


-- =============================================================================
-- 2. IDENTITY & AUTH
-- =============================================================================

-- public."user" definition

-- Drop table

-- DROP TABLE IF EXISTS "user";

-- used by: user.blood_group
-- DROP TYPE IF EXISTS blood_group_enum;
DO $$ BEGIN
	CREATE TYPE blood_group_enum AS ENUM ('A+','A-','B+','B-','AB+','AB-','O+','O-');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- used by: user.gender
-- DROP TYPE IF EXISTS gender_enum;
DO $$ BEGIN
	CREATE TYPE gender_enum AS ENUM ('male','female','other');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- used by: user.user_type
-- DROP TYPE IF EXISTS user_type_enum;
DO $$ BEGIN
	CREATE TYPE user_type_enum AS ENUM ('user','doctor','admin','staff','superadmin');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- used by: user.last_login_provider
-- DROP TYPE IF EXISTS login_provider_enum;
DO $$ BEGIN
	CREATE TYPE login_provider_enum AS ENUM ('email','google','apple');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS "user" (
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
	track_periods bool DEFAULT false NULL,
	CONSTRAINT pk_user PRIMARY KEY (id),
	CONSTRAINT uq_user_email UNIQUE (email),
	CONSTRAINT uq_user_hashcode UNIQUE (hashcode),
	CONSTRAINT uq_user_health_card_number UNIQUE (health_card_number),
	CONSTRAINT uq_user_share_token UNIQUE (share_token),
	CONSTRAINT uq_user_user_name UNIQUE (user_name),
	CONSTRAINT fk_user_created_by_user FOREIGN KEY (created_by) REFERENCES "user"(id) ON DELETE SET NULL,
	CONSTRAINT fk_user_updated_by_user FOREIGN KEY (updated_by) REFERENCES "user"(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_user_created_by ON public."user" USING btree (created_by);
CREATE INDEX IF NOT EXISTS idx_user_email ON public."user" USING btree (email);
CREATE INDEX IF NOT EXISTS idx_user_mobile ON public."user" USING btree (mobile);
CREATE INDEX IF NOT EXISTS idx_user_name ON public."user" USING btree (name);
CREATE INDEX IF NOT EXISTS idx_user_user_type ON public."user" USING btree (user_type);


-- public.refresh_token definition

-- Drop table

-- DROP TABLE IF EXISTS refresh_token;

CREATE TABLE IF NOT EXISTS refresh_token (
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
CREATE INDEX IF NOT EXISTS idx_refresh_token_active ON public.refresh_token USING btree (user_id) WHERE (revoked_at IS NULL);
CREATE INDEX IF NOT EXISTS idx_refresh_token_expires ON public.refresh_token USING btree (expires_at);


-- public.sos_contact definition

-- Drop table

-- DROP TABLE IF EXISTS sos_contact;

CREATE TABLE IF NOT EXISTS sos_contact (
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
CREATE INDEX IF NOT EXISTS idx_sos_contact_user_id ON public.sos_contact USING btree (user_id);


-- =============================================================================
-- 3. DOCTOR & FAMILY CONNECTIONS
-- =============================================================================

-- public.doctor definition

-- Drop table

-- DROP TABLE IF EXISTS doctor;

CREATE TABLE IF NOT EXISTS doctor (
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
CREATE INDEX IF NOT EXISTS idx_doctor_department_id ON public.doctor USING btree (department_id);
CREATE INDEX IF NOT EXISTS idx_doctor_hospital ON public.doctor USING btree (hospital);
CREATE INDEX IF NOT EXISTS idx_doctor_specialization_id ON public.doctor USING btree (specialization_id);
CREATE INDEX IF NOT EXISTS idx_doctor_user_id ON public.doctor USING btree (user_id);


-- public.doctor_connect definition

-- Drop table

-- DROP TABLE IF EXISTS doctor_connect;

-- used by: doctor_connect.created_by
-- DROP TYPE IF EXISTS created_by_doctor_connect_enum;
DO $$ BEGIN
	CREATE TYPE created_by_doctor_connect_enum AS ENUM ('user','doctor');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS doctor_connect (
	id int4 GENERATED BY DEFAULT AS IDENTITY( INCREMENT BY 1 MINVALUE 1 MAXVALUE 2147483647 START 1 CACHE 1 NO CYCLE) NOT NULL,
	user_id uuid NOT NULL,
	doctor_id int4 NOT NULL,
	doctor_acceptance bool NULL,
	user_acceptance bool NULL,
	created_at timestamptz DEFAULT now() NULL,
	created_by public."created_by_doctor_connect_enum" NULL,
	CONSTRAINT idx_doctor_connect_pair UNIQUE (user_id, doctor_id),
	CONSTRAINT pk_doctor_connect PRIMARY KEY (id),
	CONSTRAINT uq_doctor_connect_pair UNIQUE (user_id, doctor_id),
	CONSTRAINT fk_doctor_connect_doctor FOREIGN KEY (doctor_id) REFERENCES doctor(id) ON DELETE CASCADE,
	CONSTRAINT fk_doctor_connect_user FOREIGN KEY (user_id) REFERENCES "user"(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_doctor_connect_doctor_id ON public.doctor_connect USING btree (doctor_id);
CREATE INDEX IF NOT EXISTS idx_doctor_connect_user_id ON public.doctor_connect USING btree (user_id);


-- public.family_connect definition

-- Drop table

-- DROP TABLE IF EXISTS family_connect;

CREATE TABLE IF NOT EXISTS family_connect (
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
CREATE INDEX IF NOT EXISTS idx_family_connect_acceptor ON public.family_connect USING btree (acceptor_id);
CREATE INDEX IF NOT EXISTS idx_family_connect_requester ON public.family_connect USING btree (requester_id);


-- public.family_file_access definition

-- Drop table

-- DROP TABLE IF EXISTS family_file_access;

-- used by: family_file_access.resource_type (one value per document table in section 4)
-- DROP TYPE IF EXISTS resource_type_enum;
DO $$ BEGIN
	CREATE TYPE resource_type_enum AS ENUM (
		'bills','vaccinations','prescriptions','reports','scans_imaging','insurance','medical_condition','unclassified'
	);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS family_file_access (
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
CREATE INDEX IF NOT EXISTS idx_family_file_access_fc_id ON public.family_file_access USING btree (fc_id);
CREATE INDEX IF NOT EXISTS idx_family_file_access_resource ON public.family_file_access USING btree (resource_type, resource_id);


-- =============================================================================
-- 4. HEALTH RECORDS (DOCUMENTS)
-- =============================================================================

-- public.vaccinations definition

-- Drop table

-- DROP TABLE IF EXISTS vaccinations;

CREATE TABLE IF NOT EXISTS vaccinations (
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
CREATE INDEX IF NOT EXISTS idx_vaccinations_created_by ON public.vaccinations USING btree (created_by);
CREATE INDEX IF NOT EXISTS idx_vaccinations_hospital ON public.vaccinations USING btree (hospital);
CREATE INDEX IF NOT EXISTS idx_vaccinations_next_due ON public.vaccinations USING btree (next_due_on) WHERE (next_due_on IS NOT NULL);
CREATE INDEX IF NOT EXISTS idx_vaccinations_owner_vis ON public.vaccinations USING btree (user_id, private);
CREATE INDEX IF NOT EXISTS idx_vaccinations_user_id ON public.vaccinations USING btree (user_id);


-- public.bills definition

-- Drop table

-- DROP TABLE IF EXISTS bills;

-- used by: bills.amount_currency
-- DROP TYPE IF EXISTS currency_enum;
DO $$ BEGIN
	CREATE TYPE currency_enum AS ENUM ('INR','USD','EUR','GBP');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS bills (
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
CREATE INDEX IF NOT EXISTS idx_bills_created_by ON public.bills USING btree (created_by);
CREATE INDEX IF NOT EXISTS idx_bills_hospital ON public.bills USING btree (hospital);
CREATE INDEX IF NOT EXISTS idx_bills_owner_vis ON public.bills USING btree (user_id, private);
CREATE INDEX IF NOT EXISTS idx_bills_user_id ON public.bills USING btree (user_id);


-- public.insurance definition

-- Drop table

-- DROP TABLE IF EXISTS insurance;

CREATE TABLE IF NOT EXISTS insurance (
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
CREATE INDEX IF NOT EXISTS idx_insurance_created_by ON public.insurance USING btree (created_by);
CREATE INDEX IF NOT EXISTS idx_insurance_owner_vis ON public.insurance USING btree (user_id, private);
CREATE INDEX IF NOT EXISTS idx_insurance_provider ON public.insurance USING btree (provider);
CREATE INDEX IF NOT EXISTS idx_insurance_user_id ON public.insurance USING btree (user_id);


-- public.prescriptions definition

-- Drop table

-- DROP TABLE IF EXISTS prescriptions;

CREATE TABLE IF NOT EXISTS prescriptions (
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
CREATE INDEX IF NOT EXISTS idx_prescriptions_created_by ON public.prescriptions USING btree (created_by);
CREATE INDEX IF NOT EXISTS idx_prescriptions_hospital ON public.prescriptions USING btree (hospital);
CREATE INDEX IF NOT EXISTS idx_prescriptions_owner_vis ON public.prescriptions USING btree (user_id, private);
CREATE INDEX IF NOT EXISTS idx_prescriptions_user_id ON public.prescriptions USING btree (user_id);


-- public.reports definition

-- Drop table

-- DROP TABLE IF EXISTS reports;

CREATE TABLE IF NOT EXISTS reports (
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
CREATE INDEX IF NOT EXISTS idx_reports_created_by ON public.reports USING btree (created_by);
CREATE INDEX IF NOT EXISTS idx_reports_hospital ON public.reports USING btree (hospital);
CREATE INDEX IF NOT EXISTS idx_reports_owner_vis ON public.reports USING btree (user_id, private);
CREATE INDEX IF NOT EXISTS idx_reports_user_id ON public.reports USING btree (user_id);


-- public.scans_imaging definition

-- Drop table

-- DROP TABLE IF EXISTS scans_imaging;

CREATE TABLE IF NOT EXISTS scans_imaging (
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
CREATE INDEX IF NOT EXISTS idx_scans_imaging_created_by ON public.scans_imaging USING btree (created_by);
CREATE INDEX IF NOT EXISTS idx_scans_imaging_hospital ON public.scans_imaging USING btree (hospital);
CREATE INDEX IF NOT EXISTS idx_scans_imaging_owner_vis ON public.scans_imaging USING btree (user_id, private);
CREATE INDEX IF NOT EXISTS idx_scans_imaging_user_id ON public.scans_imaging USING btree (user_id);


-- public.unclassified_files definition

-- Drop table

-- DROP TABLE IF EXISTS unclassified_files;

CREATE TABLE IF NOT EXISTS unclassified_files (
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
CREATE INDEX IF NOT EXISTS idx_unclassified_files_created_by ON public.unclassified_files USING btree (created_by);
CREATE INDEX IF NOT EXISTS idx_unclassified_files_owner_vis ON public.unclassified_files USING btree (user_id, private);
CREATE INDEX IF NOT EXISTS idx_unclassified_files_user_id ON public.unclassified_files USING btree (user_id);


-- public.medical_condition definition

-- Drop table

-- DROP TABLE IF EXISTS medical_condition;

-- used by: medical_condition.status
-- DROP TYPE IF EXISTS medical_condition_status_enum;
DO $$ BEGIN
	CREATE TYPE medical_condition_status_enum AS ENUM ('active','resolved','chronic','monitoring');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS medical_condition (
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
CREATE INDEX IF NOT EXISTS idx_medical_condition_created_by ON public.medical_condition USING btree (created_by);
CREATE INDEX IF NOT EXISTS idx_medical_condition_owner_vis ON public.medical_condition USING btree (user_id, private);
CREATE INDEX IF NOT EXISTS idx_medical_condition_status ON public.medical_condition USING btree (status);
CREATE INDEX IF NOT EXISTS idx_medical_condition_user_id ON public.medical_condition USING btree (user_id);


-- =============================================================================
-- 5. MEDICINE TRACKING
-- =============================================================================

-- public.medicine_tracking definition

-- Drop table

-- DROP TABLE IF EXISTS medicine_tracking;

-- used by: medicine_tracking.day_pattern
-- DROP TYPE IF EXISTS day_pattern_enum;
DO $$ BEGIN
	CREATE TYPE day_pattern_enum AS ENUM ('daily','alternate','custom','interval');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS medicine_tracking (
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
CREATE INDEX IF NOT EXISTS idx_medicine_tracking_medicine_id ON public.medicine_tracking USING btree (medicine_id) WHERE (medicine_id IS NOT NULL);
CREATE INDEX IF NOT EXISTS idx_medicine_tracking_prescription_id ON public.medicine_tracking USING btree (prescription_id) WHERE (prescription_id IS NOT NULL);
CREATE INDEX IF NOT EXISTS idx_medicine_tracking_user_active ON public.medicine_tracking USING btree (user_id, starts_at, effective_end) WHERE ((stopped_at IS NULL) AND (is_prn = false));
CREATE INDEX IF NOT EXISTS idx_medicine_tracking_user_private ON public.medicine_tracking USING btree (user_id, private);
CREATE INDEX IF NOT EXISTS idx_medicine_tracking_user_prn ON public.medicine_tracking USING btree (user_id) WHERE ((is_prn = true) AND (stopped_at IS NULL));


-- public.medicine_dose_log definition

-- Drop table

-- DROP TABLE IF EXISTS medicine_dose_log;

-- used by: medicine_dose_log.status
-- DROP TYPE IF EXISTS dose_status_enum;
DO $$ BEGIN
	CREATE TYPE dose_status_enum AS ENUM ('pending','taken','skipped','forgotten');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS medicine_dose_log (
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
CREATE INDEX IF NOT EXISTS idx_medicine_dose_log_flip_job ON public.medicine_dose_log USING btree (scheduled_date, scheduled_time) WHERE (status = 'pending'::dose_status_enum);
CREATE INDEX IF NOT EXISTS idx_medicine_dose_log_tracking_date ON public.medicine_dose_log USING btree (tracking_id, scheduled_date DESC);
CREATE INDEX IF NOT EXISTS idx_medicine_dose_log_user_date ON public.medicine_dose_log USING btree (user_id, scheduled_date DESC) INCLUDE (status, dose_qty);
CREATE INDEX IF NOT EXISTS idx_medicine_dose_log_user_date_pending ON public.medicine_dose_log USING btree (user_id, scheduled_date DESC, scheduled_time) WHERE (status = 'pending'::dose_status_enum);


-- =============================================================================
-- 6. HEALTH TRACKING & MEASUREMENTS
-- =============================================================================

-- public.vital_reading definition

-- Drop table

-- DROP TABLE IF EXISTS vital_reading;

-- used by: vital_reading.vital_type
-- DROP TYPE IF EXISTS vital_type_enum;
DO $$ BEGIN
	CREATE TYPE vital_type_enum AS ENUM ('heart_rate','blood_pressure','blood_sugar','spo2');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- used by: vital_reading.source, sleep_sessions.source
-- DROP TYPE IF EXISTS vital_source_enum;
DO $$ BEGIN
	CREATE TYPE vital_source_enum AS ENUM ('manual','apple','google');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS vital_reading (
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
CREATE INDEX IF NOT EXISTS idx_vital_reading_recorded_at ON public.vital_reading USING btree (recorded_at DESC);
CREATE INDEX IF NOT EXISTS idx_vital_reading_user_type_date ON public.vital_reading USING btree (user_id, vital_type, recorded_at DESC);


-- public.body_measurement definition

-- Drop table

-- DROP TABLE IF EXISTS body_measurement;

-- used by: body_measurement.type
-- DROP TYPE IF EXISTS body_measurement_type_enum;
DO $$ BEGIN
	CREATE TYPE body_measurement_type_enum AS ENUM (
		'weight','height','bmi','body_fat','muscle_mass','water','bone_mass','visceral_fat'
	);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS body_measurement (
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
CREATE INDEX IF NOT EXISTS idx_body_measurement_date ON public.body_measurement USING btree (date DESC);
CREATE INDEX IF NOT EXISTS idx_body_measurement_user_id ON public.body_measurement USING btree (user_id);
CREATE INDEX IF NOT EXISTS idx_body_measurement_user_type ON public.body_measurement USING btree (user_id, type);
CREATE INDEX IF NOT EXISTS idx_body_measurement_user_type_date ON public.body_measurement USING btree (user_id, type, date DESC) INCLUDE (value, goal);


-- public.manual_tracking definition

-- Drop table

-- DROP TABLE IF EXISTS manual_tracking;

-- used by: manual_tracking.type
-- DROP TYPE IF EXISTS manual_tracking_type_enum;
DO $$ BEGIN
	CREATE TYPE manual_tracking_type_enum AS ENUM (
		'steps','calories','water','sleep','heart_rate','blood_pressure','blood_sugar','spo2'
	);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS manual_tracking (
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
CREATE INDEX IF NOT EXISTS idx_manual_tracking_type ON public.manual_tracking USING btree (type);
CREATE INDEX IF NOT EXISTS idx_manual_tracking_user_id ON public.manual_tracking USING btree (user_id);
CREATE INDEX IF NOT EXISTS idx_manual_tracking_user_type ON public.manual_tracking USING btree (user_id, type);
CREATE INDEX IF NOT EXISTS idx_manual_tracking_user_type_eff ON public.manual_tracking USING btree (user_id, type, effective_from DESC);


-- public.lifestyle_log definition

-- Drop table

-- DROP TABLE IF EXISTS lifestyle_log;

-- used by: lifestyle_log.log_type
-- DROP TYPE IF EXISTS lifestyle_log_type_enum;
DO $$ BEGIN
	CREATE TYPE lifestyle_log_type_enum AS ENUM ('water','alcohol','coffee','tea','smoking');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS lifestyle_log (
	id bigserial NOT NULL,
	user_id uuid NOT NULL,
	log_type public."lifestyle_log_type_enum" NOT NULL,
	quantity numeric(6, 2) NOT NULL,
	unit varchar(20) NOT NULL,
	metadata jsonb NULL,
	logged_at timestamptz DEFAULT now() NOT NULL,
	-- Quick-add coalescing: repeated taps inside the same five-minute bucket fold
	-- into one row instead of nine identical ones. Set only for one-tap writes —
	-- NULL for the Custom dialog, for anything backdated, and for every row written
	-- before this existed — and the unique index below skips NULLs, so a precise
	-- entry is never blocked by a quick-add sharing its bucket.
	--
	-- The value is stored rather than derived because date_trunc(text, timestamptz)
	-- is STABLE, not IMMUTABLE (it reads the TimeZone GUC), so PostgreSQL rejects it
	-- in an index expression. The floor is computed in Java, on the UTC instant.
	coalesce_bucket timestamptz NULL,
	CONSTRAINT pk_lifestyle_log PRIMARY KEY (id),
	CONSTRAINT fk_lifestyle_log_user FOREIGN KEY (user_id) REFERENCES "user"(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_lifestyle_log_user_date ON public.lifestyle_log USING btree (user_id, logged_at DESC);
CREATE INDEX IF NOT EXISTS idx_lifestyle_log_user_type_date ON public.lifestyle_log USING btree (user_id, log_type, logged_at DESC);

-- The arbiter for the INSERT ... ON CONFLICT that folds repeated quick-add taps.
-- It must be *partial*: the predicate keeps the index off the NULL rows (the great
-- majority) and, more importantly, the matching ON CONFLICT clause has to repeat it
-- verbatim — PostgreSQL will not infer a partial index as an arbiter otherwise, and
-- every quick-add fails with 42P10.
--
-- Hibernate cannot produce this. @Index has no predicate attribute and
-- @UniqueConstraint is unconditional, so ddl-auto=update will happily add the column
-- above and leave this index missing. This file is the only thing that creates it;
-- run it before deploying. ManualTrackingIndexCheck fails startup if it is absent.
CREATE UNIQUE INDEX IF NOT EXISTS uq_lifestyle_log_coalesce ON public.lifestyle_log USING btree (user_id, log_type, coalesce_bucket) WHERE coalesce_bucket IS NOT NULL;


-- public.lifestyle_daily_total / _weekly_total / _monthly_total definitions
--
-- Pre-aggregated graph feed. lifestyle_log stays the record of what happened; these
-- hold what the charts ask for, so a year of history reads as at most 365 rows per
-- type instead of every entry that went into it, and the year view is 12 rows rather
-- than 365 the client has to re-bucket.
--
-- All three share one shape so a single upsert, a single projection and a single
-- entity mapping can serve them, chosen by granularity.
--
--   bucket_start  the day itself; for weekly the SUNDAY that opens the week; for
--                 monthly the 1st. Sunday, NOT date_trunc('week') — PostgreSQL weeks
--                 start Monday, and the client buckets on Sunday (see startOfWeek in
--                 the React app's series.ts). A mismatch here is a chart silently off
--                 by one bar, so the value is computed in Java and stored, never
--                 derived in SQL.
--   total         numeric(12,2), not the log's (6,2): a month of water legitimately
--                 runs past 9,999.99.
--   entries       stored rows folded in, which after quick-add coalescing is not the
--                 same as taps.
--   days_counted  distinct days in the bucket that hold anything. The charts render a
--                 weekly or monthly point as total/days-with-data, so this can't be
--                 derived from entries once coalescing is on. Always 1 for daily.
--
-- Maintained incrementally by the write path (add/adjust/delete each apply their
-- delta up the chain) and reconciled from lifestyle_log on a schedule, since anything
-- that writes the log directly would otherwise drift these silently.
--
-- The primary key is the only index needed: every read is
-- user_id = ? AND bucket_start BETWEEN ? AND ?, optionally narrowed by log_type.

CREATE TABLE IF NOT EXISTS lifestyle_daily_total (
	user_id uuid NOT NULL,
	log_type public."lifestyle_log_type_enum" NOT NULL,
	bucket_start date NOT NULL,
	total numeric(12, 2) NOT NULL,
	entries int NOT NULL,
	days_counted int NOT NULL,
	CONSTRAINT pk_lifestyle_daily_total PRIMARY KEY (user_id, log_type, bucket_start),
	CONSTRAINT fk_lifestyle_daily_total_user FOREIGN KEY (user_id) REFERENCES "user"(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS lifestyle_weekly_total (
	user_id uuid NOT NULL,
	log_type public."lifestyle_log_type_enum" NOT NULL,
	bucket_start date NOT NULL,
	total numeric(12, 2) NOT NULL,
	entries int NOT NULL,
	days_counted int NOT NULL,
	CONSTRAINT pk_lifestyle_weekly_total PRIMARY KEY (user_id, log_type, bucket_start),
	CONSTRAINT fk_lifestyle_weekly_total_user FOREIGN KEY (user_id) REFERENCES "user"(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS lifestyle_monthly_total (
	user_id uuid NOT NULL,
	log_type public."lifestyle_log_type_enum" NOT NULL,
	bucket_start date NOT NULL,
	total numeric(12, 2) NOT NULL,
	entries int NOT NULL,
	days_counted int NOT NULL,
	CONSTRAINT pk_lifestyle_monthly_total PRIMARY KEY (user_id, log_type, bucket_start),
	CONSTRAINT fk_lifestyle_monthly_total_user FOREIGN KEY (user_id) REFERENCES "user"(id) ON DELETE CASCADE
);


-- public.sleep_sessions definition
-- (uses vital_source_enum, declared above with vital_reading)

-- Drop table

-- DROP TABLE IF EXISTS sleep_sessions;

CREATE TABLE IF NOT EXISTS sleep_sessions (
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
CREATE INDEX IF NOT EXISTS idx_sleep_sessions_user_started ON public.sleep_sessions USING btree (user_id, started_at DESC);


-- public.period_tracking definition

-- Drop table

-- DROP TABLE IF EXISTS period_tracking;

-- used by: period_tracking.flow_intensity
-- DROP TYPE IF EXISTS flow_intensity_enum;
DO $$ BEGIN
	CREATE TYPE flow_intensity_enum AS ENUM ('light','medium','heavy','spotting');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS period_tracking (
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
CREATE INDEX IF NOT EXISTS idx_period_tracking_start_date ON public.period_tracking USING btree (start_date);
CREATE INDEX IF NOT EXISTS idx_period_tracking_symptoms ON public.period_tracking USING gin (symptoms);
CREATE INDEX IF NOT EXISTS idx_period_tracking_user_id ON public.period_tracking USING btree (user_id);
CREATE INDEX IF NOT EXISTS idx_period_tracking_user_start ON public.period_tracking USING btree (user_id, start_date DESC);


-- =============================================================================
-- 7. SUBSCRIPTIONS & PAYMENTS
-- =============================================================================

-- public.subscriptions definition

-- Drop table

-- DROP TABLE IF EXISTS subscriptions;

-- used by: subscriptions.purpose
-- DROP TYPE IF EXISTS subscription_purpose_enum;
DO $$ BEGIN
	CREATE TYPE subscription_purpose_enum AS ENUM ('basic','premium','family','doctor');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- used by: subscriptions.payment_status
-- DROP TYPE IF EXISTS payment_status_enum;
DO $$ BEGIN
	CREATE TYPE payment_status_enum AS ENUM ('pending','completed','failed','refunded');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- used by: subscriptions.payment_method
-- DROP TYPE IF EXISTS payment_method_enum;
DO $$ BEGIN
	CREATE TYPE payment_method_enum AS ENUM ('card','upi','netbanking','wallet','paypal');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS subscriptions (
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
CREATE INDEX IF NOT EXISTS idx_subscriptions_created_by ON public.subscriptions USING btree (created_by);
CREATE INDEX IF NOT EXISTS idx_subscriptions_payment_status ON public.subscriptions USING btree (payment_status);
CREATE INDEX IF NOT EXISTS idx_subscriptions_purpose ON public.subscriptions USING btree (purpose);
CREATE INDEX IF NOT EXISTS idx_subscriptions_user_id ON public.subscriptions USING btree (user_id);


-- =============================================================================
-- 8. AUDIT
-- =============================================================================

-- public.event_log definition

-- Drop table

-- DROP TABLE IF EXISTS event_log;

CREATE TABLE IF NOT EXISTS event_log (
	id bigserial NOT NULL,
	user_id uuid NOT NULL,
	event_type varchar(100) NOT NULL,
	payload jsonb NULL,
	created_at timestamptz DEFAULT now() NULL,
	CONSTRAINT pk_event_log PRIMARY KEY (id),
	CONSTRAINT fk_event_log_user FOREIGN KEY (user_id) REFERENCES "user"(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_event_log_created_at ON public.event_log USING btree (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_event_log_user_created ON public.event_log USING btree (user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_event_log_user_id ON public.event_log USING btree (user_id);
CREATE INDEX IF NOT EXISTS idx_event_log_user_type_at ON public.event_log USING btree (user_id, event_type, created_at DESC);


-- =============================================================================
-- 9. PATCHES FOR DATABASES CREATED BEFORE THE DEFINITIONS ABOVE
-- Everything here is already folded into the CREATE statements; these run as
-- no-ops on a fresh database and only matter for an existing one.
-- =============================================================================

-- resource_type_enum gained 'unclassified' with the unclassified_files feature.
-- Note: ALTER TYPE ... ADD VALUE cannot run inside a transaction block on
-- PostgreSQL < 12, so keep this outside any explicit BEGIN.
ALTER TYPE resource_type_enum ADD VALUE IF NOT EXISTS 'unclassified';

ALTER TABLE "user" ADD COLUMN IF NOT EXISTS track_periods bool DEFAULT false;

-- lifestyle_log gained coalesce_bucket with quick-add coalescing. Existing rows take
-- NULL, which the partial unique index ignores, so this needs no backfill and the
-- index builds instantly. The CREATE UNIQUE INDEX beside the table definition is
-- already IF NOT EXISTS and covers the existing-database case too.
ALTER TABLE public.lifestyle_log ADD COLUMN IF NOT EXISTS coalesce_bucket timestamptz NULL;

-- Backfill the tracking rollups from the log they summarise.
--
-- Re-runnable and self-correcting: each grain is emptied and rebuilt, so running
-- this repairs drift as readily as it seeds a fresh database. That makes it the
-- whole-history counterpart to the nightly reconciliation, which does the same
-- computation over a trailing few days.
--
-- Replace the zone below if app.tracking.zone is set to anything else — this decides
-- which calendar day each logged instant belongs to, and it has to be the same answer
-- the application gives or the two will disagree about the boundaries of every day.
--
-- Weeks open on SUNDAY, matching the client. date_trunc('week') opens on Monday, so
-- the day is shifted forward before truncating and the result shifted back.
DO $$
DECLARE
	tracking_zone text := 'Asia/Kolkata';
BEGIN
	DELETE FROM public.lifestyle_daily_total;
	DELETE FROM public.lifestyle_weekly_total;
	DELETE FROM public.lifestyle_monthly_total;

	CREATE TEMP TABLE tmp_lifestyle_days ON COMMIT DROP AS
	SELECT l.user_id,
	       l.log_type,
	       (l.logged_at AT TIME ZONE tracking_zone)::date AS day,
	       SUM(l.quantity)                                AS total,
	       COUNT(*)::int                                  AS entries
	FROM public.lifestyle_log l
	GROUP BY 1, 2, 3;

	INSERT INTO public.lifestyle_daily_total (user_id, log_type, bucket_start, total, entries, days_counted)
	SELECT user_id, log_type, day, total, entries, 1 FROM tmp_lifestyle_days;

	INSERT INTO public.lifestyle_weekly_total (user_id, log_type, bucket_start, total, entries, days_counted)
	SELECT user_id, log_type,
	       (date_trunc('week', day + INTERVAL '1 day') - INTERVAL '1 day')::date,
	       SUM(total), SUM(entries)::int, COUNT(*)::int
	FROM tmp_lifestyle_days
	GROUP BY 1, 2, 3;

	INSERT INTO public.lifestyle_monthly_total (user_id, log_type, bucket_start, total, entries, days_counted)
	SELECT user_id, log_type, date_trunc('month', day)::date,
	       SUM(total), SUM(entries)::int, COUNT(*)::int
	FROM tmp_lifestyle_days
	GROUP BY 1, 2, 3;
END $$;

DO $$ BEGIN
	ALTER TABLE public.unclassified_files ADD CONSTRAINT fk_unclassified_files_created_by_user FOREIGN KEY (created_by) REFERENCES "user"(id) ON DELETE SET NULL;
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
	ALTER TABLE public.unclassified_files ADD CONSTRAINT fk_unclassified_files_user FOREIGN KEY (user_id) REFERENCES "user"(id) ON DELETE CASCADE;
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS traditional_health_parameters (
    id          serial4 PRIMARY KEY,
    name        varchar(100) NOT NULL,
    description text,
    units       varchar(25) NOT NULL,
    approved    boolean DEFAULT false,
    visible     boolean DEFAULT false,
    aliases     varchar(100)[]
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_thp_name ON traditional_health_parameters (name);
CREATE INDEX IF NOT EXISTS idx_thp_approved_visible ON traditional_health_parameters (approved, visible);


CREATE TABLE IF NOT EXISTS thp_age_range (
    id          serial4 PRIMARY KEY,
    thp_id      integer NOT NULL,
    age_min     integer NOT NULL,
    age_max     integer NOT NULL,
    min         float NOT NULL,
    low_danger  float NOT NULL,
    low_warn    float NOT NULL,
    ideal       float NOT NULL,
    high_warn   float NOT NULL,
    high_danger float NOT NULL,
    max         float NOT NULL,

    CONSTRAINT fk_thp_age_range_thp FOREIGN KEY (thp_id) REFERENCES traditional_health_parameters (id) ON DELETE CASCADE,
    CONSTRAINT chk_age_range CHECK (age_min <= age_max),
    CONSTRAINT chk_value_order CHECK (
        min <= low_danger AND
        low_danger <= low_warn AND
        low_warn <= ideal AND
        ideal <= high_warn AND
        high_warn <= high_danger AND
        high_danger <= max
    )
);

CREATE INDEX IF NOT EXISTS idx_age_range_thp_id ON thp_age_range (thp_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_age_range_thp_bounds ON thp_age_range (thp_id, age_min, age_max);


CREATE TABLE IF NOT EXISTS thp_alternate_units (
    id            serial4 PRIMARY KEY,
    thp_id        integer NOT NULL,
    name          varchar(100) NOT NULL,
    multiplier    float NOT NULL DEFAULT 1,
    offset_value  float NOT NULL DEFAULT 0,

    CONSTRAINT fk_alt_units_thp FOREIGN KEY (thp_id) REFERENCES traditional_health_parameters (id) ON DELETE CASCADE,
    CONSTRAINT uq_alt_units_thp_name UNIQUE (thp_id, name)
);

CREATE INDEX IF NOT EXISTS idx_alt_units_thp_id ON thp_alternate_units (thp_id);

-- ============================================================
-- V2__lifestyle_limits.sql
-- ============================================================
-- =============================================================================
-- public.lifestyle_limit
--
-- The daily limit a user sets for a tracking type — 2,000 ml of water, five
-- cigarettes — kept as a history rather than a single current value.
--
-- The rule the feature exists for: changing a limit changes it *from now on* and
-- never behind you. A week you got through under a 1,500 ml limit stays a week
-- under 1,500 ml even after you raise it to 2,000, because the chart for that week
-- resolves the row that was in force on each of its days rather than reading one
-- mutable column. That is the whole reason this is a table of rows keyed by
-- effective_from instead of a limit column on the user.
--
--   effective_from  the first day the row applies to, in the tracking zone
--                   (app.tracking.zone) — the same zone lifestyle_daily_total
--                   buckets on, so a limit and the total it bounds always agree
--                   about where a day begins. Only ever written as "today": the
--                   application never accepts a date from the client, which is what
--                   makes a past limit unreachable rather than merely discouraged.
--   limit_value     NULL means "no limit from this day on" — how a limit is removed
--                   without erasing the days it did apply to. NOT NULL would force
--                   removal to be a DELETE, which would take the history with it.
--   unit            the canonical unit of the type at the time, stored so a row is
--                   self-describing. Reads take the unit from the application's
--                   canonical map, never from here, for the same reason the rollups
--                   don't carry one: keying on a stored unit would split a series in
--                   two if any row ever held a different one.
--
-- The unique constraint is doing two jobs. It is the arbiter for the ON CONFLICT
-- upsert (setting a limit twice in one day corrects that day rather than stacking a
-- second row — today isn't a past day yet), and its index is the only one this table
-- needs: "the row in force on day D" is a backward scan of
-- (user_id, log_type, effective_from) stopping at the first hit.
-- =============================================================================

CREATE TABLE IF NOT EXISTS lifestyle_limit (
	id bigserial NOT NULL,
	user_id uuid NOT NULL,
	log_type public."lifestyle_log_type_enum" NOT NULL,
	effective_from date NOT NULL,
	limit_value numeric(8, 2) NULL,
	unit varchar(20) NOT NULL,
	created_at timestamptz DEFAULT now() NOT NULL,
	CONSTRAINT pk_lifestyle_limit PRIMARY KEY (id),
	CONSTRAINT uq_lifestyle_limit UNIQUE (user_id, log_type, effective_from),
	CONSTRAINT chk_lifestyle_limit_positive CHECK (limit_value IS NULL OR limit_value > 0),
	CONSTRAINT fk_lifestyle_limit_user FOREIGN KEY (user_id) REFERENCES "user"(id) ON DELETE CASCADE
);

-- ============================================================
-- V3__unclassified_intended_section.sql
-- ============================================================
-- =============================================================================
-- public.unclassified_files.intended_section
--
-- The section the USER chose when uploading — and only that. It is never a claim
-- about what the document *is*: the AI service classifies it and decides, and a
-- disagreement between the two is the whole reason this column exists.
--
-- Before this, a typed upload was written straight into its section table and a
-- global upload into unclassified_files, so the AI service (whose only intake is
-- this table) could never see a typed upload at all. Now every upload of a
-- processable section lands here first and is filed by that service once it knows
-- what the document is.
--
-- NULL means a global upload — the user expressed no preference. It is also what a
-- row carries if it was created before this column existed, which reads correctly:
-- those rows were global uploads.
--
-- Two things read it:
--   * the section lists, so a document still being processed shows in the section
--     the user put it in rather than vanishing into Unclassified until the AI
--     finishes;
--   * nothing else. The AI service is TOLD the intent in the submit request and
--     keeps its own copy on the run item; it never reads this column. The two
--     copies have different jobs — this one renders a list, theirs decides whether
--     to reject a mismatch — so neither derives from the other.
--
-- bills, prescriptions and medical_condition never appear here: they keep being
-- written straight into their own tables, because no extractor exists for them and
-- routing them through this table would strand them in Unclassified.
-- =============================================================================

ALTER TABLE unclassified_files
	ADD COLUMN IF NOT EXISTS intended_section public."resource_type_enum" NULL;

COMMENT ON COLUMN unclassified_files.intended_section IS
	'Section the user uploaded into; NULL for a global upload. Intent, never a classification.';

-- ============================================================
-- V4__ai_tables.sql
-- ============================================================
-- =============================================================================
-- MyHealthNotion :: AI service tables (ai_*)
--
-- The tables owned by the AI service (MHN-AI), expressed as a Flyway migration so
-- one migration run can stand up the whole schema. Copy this file into
-- src/main/resources/db/migration/ in mhn-spring, beside V3__unclassified_intended_section.sql.
--
-- V4, not V3: V3 is taken by the intended_section column. Flyway's separator is a
-- DOUBLE underscore — V4_ai_tables.sql (single) is not a versioned migration and is
-- ignored or rejected, depending on version.
--
-- -----------------------------------------------------------------------------
-- OWNERSHIP: Flyway owns these tables from here on (decided 2026-08-06).
--
-- One database, owned by the Spring team. Alembic built these tables originally and
-- is now FROZEN at revision b6d1f8a3c209: it no longer runs against any deployed
-- database (MHN-AI's preDeployCommand was removed with this change), and no further
-- Alembic revision will be written. Every schema change after this one is a new
-- V5__*.sql beside this file.
--
-- Nothing in here writes to ai_alembic_version. An earlier draft stamped it so that
-- MHN-AI's `alembic upgrade head` would stay a no-op — but that was our migration
-- tool's bookkeeping being written into someone else's database to satisfy a command
-- that should not be running there. Removing the command removes the need.
--
-- Alembic stays in the MHN-AI repo as the way local and test databases are built,
-- and it produces this schema exactly (verified column for column, index for index,
-- constraint for constraint). The consequence to know: a change made in a V5__*.sql
-- will NOT be in an Alembic-built database, so once one exists, local and test
-- databases need the Flyway files applied too.
-- -----------------------------------------------------------------------------
--
-- Everything is IF NOT EXISTS, so this is re-runnable and safe against a database
-- Alembic already built.
--
-- The UNIQUE constraints below are deliberately left unnamed. Alembic created them
-- unnamed too, so PostgreSQL's default names (ai_report_insights_run_item_id_key
-- and so on) are what exist in every database today. Naming them here in the house
-- style would produce a schema that differs from an Alembic-built one by constraint
-- name — the one difference that makes two "identical" databases disagree.
--
-- Layout, in foreign-key dependency order:
--   1. Runs and their items       — the unit of work, and the state machine
--   2. Per-stage results          — classification, extraction, insights, sections
--   3. Operational records        — cost/provenance logs, the THP worklist
--
-- Corresponds to Alembic revision b6d1f8a3c209 (MHN-AI).
-- =============================================================================


-- =============================================================================
-- 1. RUNS AND THEIR ITEMS
-- =============================================================================

-- public.ai_processing_runs definition
--
-- One submission from Spring: a batch of uploaded documents. Identified by UUID
-- because it is minted by this service rather than by a sequence Spring can see.

CREATE TABLE IF NOT EXISTS ai_processing_runs (
	id uuid DEFAULT gen_random_uuid() NOT NULL,
	requested_by_user_id uuid NULL,
	caller varchar(64) NOT NULL,
	request_id varchar(128) NULL,
	force_reprocess bool DEFAULT false NOT NULL,
	created_at timestamptz DEFAULT now() NOT NULL,
	updated_at timestamptz DEFAULT now() NOT NULL,
	PRIMARY KEY (id)
);

CREATE INDEX IF NOT EXISTS ix_ai_processing_runs_created_at ON ai_processing_runs (created_at);

COMMENT ON COLUMN ai_processing_runs.requested_by_user_id IS
	'Audit only. This service performs no user-level authorization; Spring has already made that decision.';


-- public.ai_processing_run_items definition
--
-- One uploaded document being processed. document_id is an unclassified_files id —
-- the unit of work is the upload, not a section row.
--
-- section_row_id / filed_section are written when the document is filed into its
-- section table, which happens straight after classification rather than at the end.
-- intended_section is the section the USER chose (never a claim about what the
-- document is); a disagreement with the detected section rejects the item.
-- source_key is the document's current S3 key, so the stages stop reading
-- unclassified_files — that row is deleted the moment the document is filed.

CREATE TABLE IF NOT EXISTS ai_processing_run_items (
	id uuid DEFAULT gen_random_uuid() NOT NULL,
	run_id uuid NOT NULL,
	document_id int4 NOT NULL,
	section_row_id int4 NULL,
	filed_section varchar(32) NULL,
	intended_section varchar(32) NULL,
	source_key varchar(500) NULL,
	status varchar(32) NOT NULL,
	attempt_count int4 DEFAULT 0 NOT NULL,
	content_hash varchar(128) NULL,
	last_error_code varchar(64) NULL,
	last_error_message text NULL,
	created_at timestamptz DEFAULT now() NOT NULL,
	updated_at timestamptz DEFAULT now() NOT NULL,
	started_at timestamptz NULL,
	completed_at timestamptz NULL,
	PRIMARY KEY (id),
	CONSTRAINT ck_ai_processing_run_items_status CHECK (status IN (
		'pending', 'queued', 'processing', 'classifying', 'extracting',
		'generating_insights', 'completed', 'failed', 'rejected', 'cancelled'
	)),
	FOREIGN KEY (run_id) REFERENCES ai_processing_runs (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_ai_run_items_document_id ON ai_processing_run_items (document_id);
CREATE INDEX IF NOT EXISTS ix_ai_run_items_run_id ON ai_processing_run_items (run_id);
CREATE INDEX IF NOT EXISTS ix_ai_run_items_status ON ai_processing_run_items (status);

-- Idempotency, enforced by the database rather than by application code: at most one
-- in-flight item per document. SQS is at-least-once, so a duplicate submission must
-- reuse the live item instead of processing the document twice. Partial, so any
-- number of finished items may exist for the same document (every retry is a new one).
-- The statuses are listed alphabetically rather than in lifecycle order to match what
-- Alembic emits byte for byte. PostgreSQL stores the predicate as written, so the two
-- orders produce indexes that behave identically and compare as different — which is
-- exactly the sort of phantom difference this file exists to avoid.
CREATE UNIQUE INDEX IF NOT EXISTS uq_ai_run_items_active_document
	ON ai_processing_run_items (document_id)
	WHERE status IN ('classifying', 'extracting', 'generating_insights', 'pending', 'processing', 'queued');

COMMENT ON COLUMN ai_processing_run_items.document_id IS
	'unclassified_files.id — the uploaded document, not a section row.';
COMMENT ON COLUMN ai_processing_run_items.section_row_id IS
	'Row created when the document was filed, in the table named by filed_section.';
COMMENT ON COLUMN ai_processing_run_items.intended_section IS
	'Section the user uploaded into; NULL for a global upload. Intent, never a classification.';


-- =============================================================================
-- 2. PER-STAGE RESULTS
--
-- One row per run item, upserted: a redelivered SQS message re-runs the stage and
-- overwrites its own prior attempt rather than appending a duplicate.
-- =============================================================================

-- public.ai_report_classifications definition
--
-- The detected section and title for one document. Written for EVERY document,
-- including those whose section has no pipeline — the classification is the reason
-- such a document is rejected, so it is a result, not a failure.

CREATE TABLE IF NOT EXISTS ai_report_classifications (
	id uuid DEFAULT gen_random_uuid() NOT NULL,
	run_item_id uuid NOT NULL,
	document_id int4 NOT NULL,
	section varchar(32) NOT NULL,
	title varchar(512) NOT NULL,
	confidence numeric(4, 3) NOT NULL,
	reasoning text NULL,
	prompt_version varchar(32) NOT NULL,
	schema_version varchar(32) NOT NULL,
	created_at timestamptz DEFAULT now() NOT NULL,
	updated_at timestamptz DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (run_item_id),
	FOREIGN KEY (run_item_id) REFERENCES ai_processing_run_items (id) ON DELETE CASCADE
);


-- public.ai_report_extractions definition
--
-- Lab results for a REPORT, with the deterministic fields already computed in
-- Python: abnormal flags, parsed ranges, curated unit conversions. The model only
-- transcribes; none of the arithmetic in here came from it.

CREATE TABLE IF NOT EXISTS ai_report_extractions (
	id uuid DEFAULT gen_random_uuid() NOT NULL,
	run_item_id uuid NOT NULL,
	document_id int4 NOT NULL,
	"data" jsonb NOT NULL,
	prompt_version varchar(32) NOT NULL,
	schema_version varchar(32) NOT NULL,
	created_at timestamptz DEFAULT now() NOT NULL,
	updated_at timestamptz DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (run_item_id),
	FOREIGN KEY (run_item_id) REFERENCES ai_processing_run_items (id) ON DELETE CASCADE
);


-- public.ai_report_insights definition
--
-- Patient-facing interpretation, for reports only. Reasoned over the validated
-- extraction rather than the file, so a value that bypassed extraction cannot
-- reappear here.

CREATE TABLE IF NOT EXISTS ai_report_insights (
	id uuid DEFAULT gen_random_uuid() NOT NULL,
	run_item_id uuid NOT NULL,
	document_id int4 NOT NULL,
	"data" jsonb NOT NULL,
	prompt_version varchar(32) NOT NULL,
	schema_version varchar(32) NOT NULL,
	created_at timestamptz DEFAULT now() NOT NULL,
	updated_at timestamptz DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (run_item_id),
	FOREIGN KEY (run_item_id) REFERENCES ai_processing_run_items (id) ON DELETE CASCADE
);


-- public.ai_section_extractions definition
--
-- Transcribed fields for a NON-report section: insurance, scans_imaging,
-- vaccinations. These sections have no insights row and never will — there is
-- nothing clinical to interpret in a policy schedule or a vaccination card.

CREATE TABLE IF NOT EXISTS ai_section_extractions (
	id uuid DEFAULT gen_random_uuid() NOT NULL,
	run_item_id uuid NOT NULL,
	document_id int4 NOT NULL,
	section varchar(32) NOT NULL,
	"data" jsonb NOT NULL,
	prompt_version varchar(32) NOT NULL,
	schema_version varchar(32) NOT NULL,
	created_at timestamptz DEFAULT now() NOT NULL,
	updated_at timestamptz DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (run_item_id),
	FOREIGN KEY (run_item_id) REFERENCES ai_processing_run_items (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_ai_section_extractions_section ON ai_section_extractions (section);


-- =============================================================================
-- 3. OPERATIONAL RECORDS
-- =============================================================================

-- public.ai_process_logs definition
--
-- What each model call cost and which vendor received the document. One row per
-- (run_item_id, stage, attempt): a redelivery of the SAME attempt updates its row,
-- so one attempt is never billed twice, while a genuine retry gets its own row.
--
-- Never stores document contents, prompts, or credentials — error_detail is
-- sanitised to field locations and message types before it is written.

CREATE TABLE IF NOT EXISTS ai_process_logs (
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
	PRIMARY KEY (id),
	CONSTRAINT uq_ai_process_logs_item_stage_attempt UNIQUE (run_item_id, stage, attempt),
	FOREIGN KEY (run_item_id) REFERENCES ai_processing_run_items (id) ON DELETE CASCADE
);

COMMENT ON COLUMN ai_process_logs.provider IS
	'Vendor that answered — anthropic / google, or "skipped" when the stage made no model call.';


-- public.ai_thp_fallbacks definition
--
-- An R&D worklist, not an error log. A row here means a test fell back to the
-- report's own printed range because no approved traditional_health_parameters
-- ideal range applied — unmatched, unapproved, no bracket for the patient's age, or
-- a unit with no curated conversion. report_unit is kept so a unit_mismatch row says
-- which unit needs curating.

CREATE TABLE IF NOT EXISTS ai_thp_fallbacks (
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
	report_unit varchar(64) NULL,
	created_at timestamptz DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY (run_item_id) REFERENCES ai_processing_run_items (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_ai_thp_fallbacks_matched_parameter ON ai_thp_fallbacks (matched_parameter);
CREATE INDEX IF NOT EXISTS ix_ai_thp_fallbacks_reason ON ai_thp_fallbacks (reason);
CREATE INDEX IF NOT EXISTS ix_ai_thp_fallbacks_run_item_id ON ai_thp_fallbacks (run_item_id);


-- =============================================================================

-- ============================================================
-- V5__period_tracking.sql
-- ============================================================
-- =============================================================================
-- MyHealthNotion :: period & cycle tracking
--
-- V1 created period_tracking and an entity to map it, and nothing else. No
-- repository, no service, no controller — the table has never been written to.
-- This migration turns it into the anchor row of a real cycle model and adds the
-- three tables that make PCOS, menopause and contraception behave correctly,
-- plus the generic notification tables the push pipeline needs.
--
-- Design rules encoded here rather than in Java, because a query can forget a
-- rule and a generated column cannot:
--
--   * period_tracking.counts_toward_stats — a withdrawal bleed, lochia or a
--     postmenopausal bleed is not a cycle and never enters a cycle-length average.
--   * period_status.cycles_countable / predictions_suppressed / prompts_silenced —
--     the whole "stop counting", "stop predicting" and "stop asking" policy is one
--     boolean each, derived from the status in force rather than re-derived by
--     every caller.
--
-- Dates are `date`, never `timestamptz`: a menstrual day is a calendar day. That
-- is already how medicine_dose_log.scheduled_date and lifestyle_limit.effective_from
-- work, and it is what makes (user_id, start_date) a usable ON CONFLICT arbiter.
-- =============================================================================


-- =============================================================================
-- 1. ENUM TYPES
-- =============================================================================

-- Why a bleed happened. Only 'menstrual' counts as a cycle; the rest are recorded
-- and shown on the calendar but excluded from every statistic.
DO $$ BEGIN
	CREATE TYPE bleed_type_enum AS ENUM (
		'menstrual','withdrawal','breakthrough','postpartum','postmenopausal','unknown'
	);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- Early and late perimenopause are separate values because they behave differently:
-- early still predicts (with a wider band), late does not.
DO $$ BEGIN
	CREATE TYPE life_stage_enum AS ENUM (
		'premenarche','premenopause','perimenopause_early','perimenopause_late',
		'menopause','postmenopause','unknown'
	);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- pill_cyclic is split from pill_continuous because only the first produces a
-- scheduled withdrawal bleed that can be predicted from the packet.
DO $$ BEGIN
	CREATE TYPE contraception_enum AS ENUM (
		'none','pill_cyclic','pill_continuous','pop','hormonal_iud','copper_iud',
		'implant','injection','patch','ring','barrier','sterilisation','other'
	);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
	CREATE TYPE pregnancy_enum AS ENUM ('not_pregnant','trying','pregnant','postpartum');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- hysterectomy leaves the ovaries: bleeding stops forever, hormonal cycling does
-- not. hysterectomy_bso and oophorectomy_bilateral are surgical menopause. They
-- are NOT the same silence, which is why they are separate values.
DO $$ BEGIN
	CREATE TYPE surgical_enum AS ENUM (
		'none','hysterectomy','hysterectomy_bso','oophorectomy_bilateral',
		'oophorectomy_unilateral','tubal_ligation','other'
	);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
	CREATE TYPE cycle_goal_enum AS ENUM (
		'cycle_health','conception','avoid_pregnancy','menopause','none'
	);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
	CREATE TYPE device_platform_enum AS ENUM ('web','android','ios');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
	CREATE TYPE notification_status_enum AS ENUM ('pending','sending','sent','failed','skipped');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- flow_intensity_enum is deliberately left alone. Its V1 declaration order
-- ('light','medium','heavy','spotting') means PostgreSQL sorts spotting above
-- heavy, so MAX(flow) over a bleed would return the wrong value — and enum values
-- cannot be reordered. Rather than migrate the type, the Java enum is declared in
-- ascending order (NAMED_ENUM matches by name, not ordinal) and flow is never
-- compared in SQL. See entities/enums/FlowIntensity.java.
COMMENT ON TYPE flow_intensity_enum IS
	'Declaration order is NOT ascending by heaviness (spotting sorts last). Never '
	'use MAX/ORDER BY on a column of this type; compare flow in Java, where '
	'FlowIntensity is declared in the correct order.';


-- =============================================================================
-- 2. period_tracking — one row per cycle (altered in place)
-- =============================================================================

-- The table is empty in every environment (nothing has ever written to it), so
-- the USING clauses below are a formality rather than a data conversion. The zone
-- is spelled out anyway so the statement is correct if a row ever did exist.
DO $$ BEGIN
	IF EXISTS (SELECT 1 FROM information_schema.columns
	           WHERE table_schema = 'public' AND table_name = 'period_tracking'
	             AND column_name = 'start_date' AND data_type <> 'date') THEN
		ALTER TABLE public.period_tracking
			ALTER COLUMN start_date TYPE date USING (start_date AT TIME ZONE 'Asia/Kolkata')::date,
			ALTER COLUMN end_date   TYPE date USING (end_date   AT TIME ZONE 'Asia/Kolkata')::date;
	END IF;
END $$;

-- is_predicted / correct_prediction put predictions in the table the statistics
-- read from, which is how a model ends up trained on its own output. Predictions
-- are computed on read instead. symptoms was an untyped jsonb blob with no writer
-- and no vocabulary; it is superseded by period_day_log.symptoms.
ALTER TABLE public.period_tracking
	DROP COLUMN IF EXISTS is_predicted,
	DROP COLUMN IF EXISTS correct_prediction,
	DROP COLUMN IF EXISTS symptoms,
	DROP COLUMN IF EXISTS cycle_length;

DROP INDEX IF EXISTS public.idx_period_tracking_symptoms;   -- GIN over a column that is gone

ALTER TABLE public.period_tracking
	ADD COLUMN IF NOT EXISTS bleed_end date NULL,
	ADD COLUMN IF NOT EXISTS bleed_type public."bleed_type_enum" DEFAULT 'menstrual'::bleed_type_enum NOT NULL,
	ADD COLUMN IF NOT EXISTS notes varchar(500) NULL,
	ADD COLUMN IF NOT EXISTS created_at timestamptz DEFAULT now() NOT NULL,
	ADD COLUMN IF NOT EXISTS updated_at timestamptz DEFAULT now() NOT NULL;

-- Added after the type change, in their own statement: a generated column cannot
-- reference a column being altered in the same ALTER TABLE.
--
-- end_date is the day BEFORE the next cycle starts, so the cycle spans
-- [start_date, end_date] inclusive and its length is the difference plus one.
-- Generated rather than a plain int4 so it can never disagree with the dates it
-- summarises — the one thing V1's nullable cycle_length could not promise.
ALTER TABLE public.period_tracking
	ADD COLUMN IF NOT EXISTS cycle_length int4
		GENERATED ALWAYS AS ((end_date - start_date) + 1) STORED,
	ADD COLUMN IF NOT EXISTS bleed_length int4
		GENERATED ALWAYS AS ((bleed_end - start_date) + 1) STORED,
	ADD COLUMN IF NOT EXISTS counts_toward_stats bool
		GENERATED ALWAYS AS (bleed_type = 'menstrual'::bleed_type_enum) STORED;

DO $$ BEGIN
	ALTER TABLE public.period_tracking
		-- The ON CONFLICT arbiter that makes "my period started" tapped twice
		-- idempotent. Only possible now start_date is a date; two taps five minutes
		-- apart used to produce two distinct instants and two cycles.
		ADD CONSTRAINT uq_period_tracking_user_start UNIQUE (user_id, start_date);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
	ALTER TABLE public.period_tracking
		ADD CONSTRAINT chk_period_tracking_end CHECK (end_date IS NULL OR end_date >= start_date),
		ADD CONSTRAINT chk_period_tracking_bleed_end CHECK (bleed_end IS NULL OR bleed_end >= start_date),
		ADD CONSTRAINT chk_period_tracking_bleed_in CHECK (
			bleed_end IS NULL OR end_date IS NULL OR bleed_end <= end_date),
		ADD CONSTRAINT chk_period_tracking_length CHECK (
			end_date IS NULL OR (end_date - start_date) + 1 BETWEEN 1 AND 400);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- "Not in the future" cannot be a CHECK — CURRENT_DATE is not immutable — so it
-- stays an application rule in PeriodServiceImpl. Noted here so nobody tries.

-- The statistics read: countable, closed cycles only, newest first, length already
-- in the index. Index-only, at most twelve rows.
CREATE INDEX IF NOT EXISTS idx_period_tracking_stats
	ON public.period_tracking USING btree (user_id, start_date DESC)
	INCLUDE (cycle_length)
	WHERE counts_toward_stats AND end_date IS NOT NULL;

-- "The user's current cycle" in one index probe, and a guarantee that a
-- half-finished history import cannot leave two open cycles behind.
CREATE UNIQUE INDEX IF NOT EXISTS uq_period_tracking_open
	ON public.period_tracking USING btree (user_id) WHERE end_date IS NULL;

COMMENT ON COLUMN public.period_tracking.end_date IS
	'The day BEFORE the next cycle starts — the cycle boundary, not the last day of '
	'bleeding. NULL means this is the current, open cycle. Last bleeding day is bleed_end.';


-- =============================================================================
-- 3. period_day_log — one row per user per calendar day
-- =============================================================================

-- Exists INDEPENDENTLY of bleeding: a day with no flow and three symptoms is the
-- normal mid-cycle entry, and that is the whole point. Attaching symptoms to a
-- period row — which is what V1's period_tracking.symptoms did — leaves nowhere to
-- record the mid-cycle days where PCOS and perimenopause actually show up.
CREATE TABLE IF NOT EXISTS period_day_log (
	id bigserial NOT NULL,
	user_id uuid NOT NULL,
	-- Nullable and ON DELETE SET NULL: deleting a cycle entered by mistake must not
	-- delete the symptom history recorded under it. The days happened; only the
	-- boundary was wrong. CycleAssembler re-links them.
	cycle_id int4 NULL,
	log_date date NOT NULL,
	-- Denormalised (log_date - cycle_start + 1). Rendered on every calendar cell;
	-- recomputed whenever cycle_id changes.
	cycle_day int2 NULL,

	-- NULL means bleeding was not recorded for this day. It is NOT the same as
	-- no_bleeding = true, which is the user explicitly saying it has stopped — one
	-- is an absence of data, the other a measurement, and only the second can close
	-- a bleed.
	flow public."flow_intensity_enum" NULL,
	no_bleeding bool DEFAULT false NOT NULL,

	-- Codes from the PeriodSymptom Java enum, validated on write. A text[] rather
	-- than a join table: the vocabulary is ~45 entries, the only query that matters
	-- is unnest + GROUP BY, and adding a symptom stays a Java constant rather than a
	-- migration. Same shape as medicine_master.used_for.
	symptoms text[] DEFAULT '{}'::text[] NOT NULL,

	pain_level int2 NULL,
	mood varchar(30) NULL,
	notes varchar(500) NULL,
	created_at timestamptz DEFAULT now() NOT NULL,
	updated_at timestamptz DEFAULT now() NOT NULL,

	CONSTRAINT pk_period_day_log PRIMARY KEY (id),
	-- The ON CONFLICT arbiter for PeriodDayLogUpsertDao.
	CONSTRAINT uq_period_day_log_user_date UNIQUE (user_id, log_date),
	CONSTRAINT chk_period_day_log_flow CHECK (NOT (no_bleeding AND flow IS NOT NULL)),
	CONSTRAINT chk_period_day_log_pain CHECK (pain_level IS NULL OR pain_level BETWEEN 0 AND 4),
	CONSTRAINT chk_period_day_log_cycle_day CHECK (cycle_day IS NULL OR cycle_day BETWEEN 1 AND 400),
	CONSTRAINT fk_period_day_log_user FOREIGN KEY (user_id) REFERENCES "user"(id) ON DELETE CASCADE,
	CONSTRAINT fk_period_day_log_cycle FOREIGN KEY (cycle_id) REFERENCES period_tracking(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_period_day_log_user_date
	ON public.period_day_log USING btree (user_id, log_date DESC);
CREATE INDEX IF NOT EXISTS idx_period_day_log_cycle
	ON public.period_day_log USING btree (cycle_id, log_date) WHERE cycle_id IS NOT NULL;
-- Bleeding-day scans (prolonged bleeding, postmenopausal bleeding, closing a bleed).
-- Partial, because most logged days are not bleeding days.
CREATE INDEX IF NOT EXISTS idx_period_day_log_bleeding
	ON public.period_day_log USING btree (user_id, log_date DESC) WHERE flow IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_period_day_log_symptoms
	ON public.period_day_log USING gin (symptoms);


-- =============================================================================
-- 4. period_status — the clinical context, dated
-- =============================================================================

-- The one table that makes PCOS, menopause and contraception behave correctly.
--
-- Effective-dated snapshots in the lifestyle_limit shape: one row per change,
-- never updated in place, so "the state in force on day D" is a backward index
-- scan stopping at the first hit and no history is lost. Backdating IS allowed
-- here, unlike lifestyle_limit — clinical history genuinely is retrospective.
--
-- DENORMALISATION, DELIBERATE: this is a full snapshot, not a delta. Changing only
-- the contraception method copies the other axes forward. Four effective-dated
-- tables, one per axis, would make "what was true on 14 March" four correlated
-- subqueries and would make the three derived booleans impossible, because a
-- generated column can only see its own row. Writes here are a handful per user
-- per lifetime.
CREATE TABLE IF NOT EXISTS period_status (
	id bigserial NOT NULL,
	user_id uuid NOT NULL,
	effective_from date NOT NULL,

	stage         public."life_stage_enum"    DEFAULT 'unknown'::life_stage_enum        NOT NULL,
	contraception public."contraception_enum" DEFAULT 'none'::contraception_enum        NOT NULL,
	pregnancy     public."pregnancy_enum"     DEFAULT 'not_pregnant'::pregnancy_enum    NOT NULL,
	surgical      public."surgical_enum"      DEFAULT 'none'::surgical_enum             NOT NULL,
	breastfeeding  bool DEFAULT false NOT NULL,
	-- A boolean rather than a medical_condition lookup because medical_condition.name
	-- is free text with no taxonomy: WHERE name = 'PCOS' matches neither 'pcos' nor
	-- 'Polycystic ovary syndrome'. The app writes both — the medical_condition row is
	-- the human-facing record, this is the machine-readable one. Drop this in favour
	-- of a condition_code column on medical_condition if that taxonomy ever lands.
	diagnosed_pcos bool DEFAULT false NOT NULL,
	-- user | clinician | derived. 'derived' marks rows the nightly job wrote, e.g.
	-- the twelve-month amenorrhoea confirmation.
	source varchar(20) DEFAULT 'user' NOT NULL,
	note varchar(255) NULL,
	created_at timestamptz DEFAULT now() NOT NULL,

	-- Does a bleed under this status count as a physiological cycle? Hormonal
	-- methods produce withdrawal bleeds on a schedule set by the packet, not by an
	-- ovary; pregnancy, lactational amenorrhoea and menopause are not "missed"
	-- cycles. Everything the statistics must exclude is this one boolean being false.
	cycles_countable bool GENERATED ALWAYS AS (
		stage NOT IN ('premenarche'::life_stage_enum,'menopause'::life_stage_enum,
		              'postmenopause'::life_stage_enum)
		AND pregnancy IN ('not_pregnant'::pregnancy_enum,'trying'::pregnancy_enum)
		AND breastfeeding = false
		AND surgical IN ('none'::surgical_enum,'tubal_ligation'::surgical_enum,
		                 'oophorectomy_unilateral'::surgical_enum)
		-- EVERY hormonal method, not just the suppressive ones. pill_cyclic, patch and
		-- ring do produce a bleed, but it is a withdrawal bleed timed by the packet's
		-- hormone-free interval rather than by an ovary, so counting it would let the
		-- app report the pill's calendar back to the user as though it were her cycle.
		-- This list must stay in step with Contraception.isHormonal().
		AND contraception NOT IN ('pill_cyclic'::contraception_enum,'pill_continuous'::contraception_enum,
		                          'pop'::contraception_enum,'hormonal_iud'::contraception_enum,
		                          'implant'::contraception_enum,'injection'::contraception_enum,
		                          'patch'::contraception_enum,'ring'::contraception_enum)
	) STORED,

	-- Stop predicting. Not low confidence — a refusal.
	predictions_suppressed bool GENERATED ALWAYS AS (
		stage IN ('premenarche'::life_stage_enum,'menopause'::life_stage_enum,
		          'postmenopause'::life_stage_enum)
		OR pregnancy = 'pregnant'::pregnancy_enum
		OR surgical IN ('hysterectomy'::surgical_enum,'hysterectomy_bso'::surgical_enum,
		                'oophorectomy_bilateral'::surgical_enum)
	) STORED,

	-- Stop ASKING about periods, permanently: no uterus means there will never be
	-- another bleed to log. Deliberately NOT set by menopause — a postmenopausal
	-- user must still be able to log bleeding, because that bleed is the red flag.
	prompts_silenced bool GENERATED ALWAYS AS (
		surgical IN ('hysterectomy'::surgical_enum,'hysterectomy_bso'::surgical_enum)
	) STORED,

	CONSTRAINT pk_period_status PRIMARY KEY (id),
	CONSTRAINT uq_period_status_user_from UNIQUE (user_id, effective_from),
	-- Removing both ovaries IS menopause. Recording the surgery without its
	-- consequence is a data error, so the two are written together or not at all.
	CONSTRAINT chk_period_status_surgical CHECK (
		surgical NOT IN ('hysterectomy_bso'::surgical_enum,'oophorectomy_bilateral'::surgical_enum)
		OR stage IN ('menopause'::life_stage_enum,'postmenopause'::life_stage_enum)),
	CONSTRAINT chk_period_status_source CHECK (source IN ('user','clinician','derived')),
	CONSTRAINT fk_period_status_user FOREIGN KEY (user_id) REFERENCES "user"(id) ON DELETE CASCADE
);

-- The only index needed: every read is "the row in force on day D", a backward scan.
CREATE INDEX IF NOT EXISTS idx_period_status_user_from
	ON public.period_status USING btree (user_id, effective_from DESC);


-- =============================================================================
-- 5. period_settings — one row per user
-- =============================================================================

-- Not columns on "user": that row is loaded on every authenticated request for
-- every user type including doctors and admins, and this is the privacy control
-- for the most sensitive data in the product — it wants its own row, its own audit
-- and its own deletion.
CREATE TABLE IF NOT EXISTS period_settings (
	id serial4 NOT NULL,
	user_id uuid NOT NULL,

	enabled bool DEFAULT true NOT NULL,
	-- DEFAULT TRUE, unlike every other `private` column in this schema.
	--
	-- The existing family sharing model is default-ALLOW: absent a
	-- family_file_access row, an accepted connection sees the record. Shipping cycle
	-- data on that model would, on release day, hand every accepted connection —
	-- spouse, parent, sibling, in-law — visibility of contraception and pregnancy
	-- status that nobody opted into. This is the wrong default for this class of
	-- data, so it is inverted here and resource_type_enum is deliberately NOT
	-- extended with a cycle value.
	private bool DEFAULT true NOT NULL,
	-- Opt-in, and only meaningful for doctors who already hold an accepted
	-- doctor_connect. A cardiologist has no business with a cycle log.
	share_with_doctor bool DEFAULT false NOT NULL,

	goal public."cycle_goal_enum" DEFAULT 'cycle_health'::cycle_goal_enum NOT NULL,
	predict_enabled bool DEFAULT true NOT NULL,
	-- Off unless the user turns it on. A fertile window is an estimate, it is not
	-- contraception, and defaulting it on would put a claim in front of people who
	-- never asked for one.
	show_fertile_window bool DEFAULT false NOT NULL,

	notify_period_due     bool DEFAULT true  NOT NULL,
	notify_period_late    bool DEFAULT true  NOT NULL,
	notify_log_reminder   bool DEFAULT false NOT NULL,
	notify_care_tips      bool DEFAULT true  NOT NULL,
	-- Separately controllable and on by default. ANOMALY_URGENT ignores this
	-- entirely — see the notification catalogue.
	notify_insights       bool DEFAULT true  NOT NULL,

	-- Seed for a user with no history yet, and what an irregular user can pin.
	assumed_cycle_length int2 NULL,
	reminder_lead_days int2 DEFAULT 2 NOT NULL,
	reminder_time time DEFAULT '09:00'::time NOT NULL,
	paused_until date NULL,
	created_at timestamptz DEFAULT now() NOT NULL,
	updated_at timestamptz DEFAULT now() NOT NULL,

	CONSTRAINT pk_period_settings PRIMARY KEY (id),
	CONSTRAINT uq_period_settings_user UNIQUE (user_id),
	CONSTRAINT chk_period_settings_lead CHECK (reminder_lead_days BETWEEN 0 AND 7),
	CONSTRAINT chk_period_settings_assumed CHECK (
		assumed_cycle_length IS NULL OR assumed_cycle_length BETWEEN 15 AND 90),
	CONSTRAINT fk_period_settings_user FOREIGN KEY (user_id) REFERENCES "user"(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_period_settings_reminder
	ON public.period_settings USING btree (reminder_time)
	WHERE enabled AND notify_period_due;


-- =============================================================================
-- 6. Per-user timezone
-- =============================================================================

-- app.tracking.zone is global BY DESIGN — TrackingZone's javadoc is explicit that
-- it must be identical everywhere writing to a given database, because the
-- lifestyle rollup tables store the resolved calendar day at write time. It cannot
-- be made per-user without silently reinterpreting rows already written.
--
-- Using it to schedule notifications would therefore be a product bug: a 09:00
-- reminder computed in Asia/Kolkata fires at 03:30 for a user in London, which is
-- the fastest known way to get notifications turned off permanently. Hence a
-- per-user column, read by common/util/UserZone with TrackingZone as the fallback.
ALTER TABLE public."user" ADD COLUMN IF NOT EXISTS timezone varchar(64) NULL;

COMMENT ON COLUMN public."user".timezone IS
	'IANA region id (Asia/Kolkata), never a fixed offset. NULL falls back to '
	'app.tracking.zone. Used for notification scheduling only — it must not be used '
	'to bucket lifestyle rollups, which are global by design.';

COMMENT ON COLUMN public."user".track_periods IS
	'The answer given at signup, and only that. period_settings.enabled is the '
	'authoritative on/off switch from V5 onward; this column is never read as a '
	'setting and is never written after signup.';


-- =============================================================================
-- 7. Notifications (generic — medicine dose reminders will reuse these)
-- =============================================================================

CREATE TABLE IF NOT EXISTS device_token (
	id bigserial NOT NULL,
	user_id uuid NOT NULL,
	-- UNIQUE across users, not per user. A registration token identifies a device
	-- installation, so re-registration after a device changes hands must MOVE the
	-- token to the new account rather than leave it pushing to the old one.
	token text NOT NULL,
	platform public."device_platform_enum" NOT NULL,
	device_label varchar(120) NULL,
	app_version varchar(32) NULL,
	created_at timestamptz DEFAULT now() NOT NULL,
	last_seen_at timestamptz DEFAULT now() NOT NULL,
	last_success_at timestamptz NULL,
	-- Consecutive RETRYABLE failures. A permanent failure revokes instead.
	failure_count int2 DEFAULT 0 NOT NULL,
	-- Soft delete, so "why did my pushes stop" stays answerable in support.
	revoked_at timestamptz NULL,
	revoke_reason varchar(40) NULL,
	CONSTRAINT pk_device_token PRIMARY KEY (id),
	CONSTRAINT uq_device_token_token UNIQUE (token),
	CONSTRAINT fk_device_token_user FOREIGN KEY (user_id) REFERENCES "user"(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_device_token_user_active
	ON public.device_token USING btree (user_id) WHERE revoked_at IS NULL;

-- The user-visible record AND the queue state, in one row. A separate outbox table
-- would buy an aggregate-consistency guarantee there is nothing to be consistent
-- with — the scheduler is the only producer and commits nothing else — while
-- adding a write and a join to every history list. The history the frontend
-- renders IS the queue.
CREATE TABLE IF NOT EXISTS notification (
	id bigserial NOT NULL,
	user_id uuid NOT NULL,
	code varchar(60) NOT NULL,
	source_module varchar(30) NOT NULL,
	title varchar(120) NOT NULL,
	body varchar(500) NOT NULL,
	data jsonb NULL,
	status public."notification_status_enum" DEFAULT 'pending'::notification_status_enum NOT NULL,
	-- Already adjusted for the user's zone and quiet hours by the scheduling job.
	-- Generation is separate from sending precisely because quiet hours mean
	-- "decide at 03:00, deliver at 08:00 local".
	send_after timestamptz NOT NULL,
	attempts int2 DEFAULT 0 NOT NULL,
	claimed_at timestamptz NULL,
	last_error varchar(255) NULL,
	-- Whatever makes this notification unique in time — the predicted date for a
	-- period reminder, the cycle id for a cycle-scoped flag, the ISO week otherwise.
	-- With the unique constraint below it is what makes the hourly scheduler
	-- re-runnable and what expresses every cadence limit.
	dedupe_key varchar(80) NOT NULL,
	sent_at timestamptz NULL,
	read_at timestamptz NULL,
	created_at timestamptz DEFAULT now() NOT NULL,
	CONSTRAINT pk_notification PRIMARY KEY (id),
	CONSTRAINT uq_notification_dedupe UNIQUE (user_id, code, dedupe_key),
	CONSTRAINT fk_notification_user FOREIGN KEY (user_id) REFERENCES "user"(id) ON DELETE CASCADE
);
-- The dispatcher's FOR UPDATE SKIP LOCKED claim.
CREATE INDEX IF NOT EXISTS idx_notification_claim
	ON public.notification USING btree (send_after)
	WHERE status = 'pending'::notification_status_enum;
CREATE INDEX IF NOT EXISTS idx_notification_user_created
	ON public.notification USING btree (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_notification_user_unread
	ON public.notification USING btree (user_id, created_at DESC)
	WHERE read_at IS NULL AND status = 'sent'::notification_status_enum;


-- =============================================================================
-- 8. Seeds
-- =============================================================================

-- Turn the feature on for everyone who said yes at signup, so it works for them
-- without a first-run write. private = true and enabled = true are the column
-- defaults; nothing here overrides them.
INSERT INTO public.period_settings (user_id)
SELECT u.id FROM public."user" u WHERE u.track_periods IS TRUE
ON CONFLICT ON CONSTRAINT uq_period_settings_user DO NOTHING;

-- A baseline status row per settings row, so the "status in force on day D"
-- backward scan always finds something. stage = 'unknown' with everything else
-- defaulted makes cycles_countable true — the right default: absent information,
-- treat a bleed as a period.
INSERT INTO public.period_status (user_id, effective_from, stage, source, note)
SELECT s.user_id, COALESCE(u.created_at::date, CURRENT_DATE), 'unknown'::life_stage_enum, 'derived',
       'Baseline row created by V5. Replaced by the first status the user records.'
FROM public.period_settings s
JOIN public."user" u ON u.id = s.user_id
ON CONFLICT ON CONSTRAINT uq_period_status_user_from DO NOTHING;

-- ----------------------------------------------------------
-- V6 davi_ai_tables — SKIPPED (Davi's own adopted migration)
-- ----------------------------------------------------------

-- ============================================================
-- V7__medical_history.sql
-- ============================================================
-- Conditions, surgeries and allergies all live in medical_condition, split by a
-- type column. They share user, dates, notes and the private flag, and the hub
-- screen reads all three together — three tables would have meant three copies of
-- the family-sharing switch in FamilyServiceImpl for nothing.
--
-- Past vs upcoming surgery and ongoing vs recovered are NOT stored. They are
-- started_on against today and ended_on being null. An upcoming surgery becomes a
-- past one the day it happens, which is what the recovery card expects.

DO $$ BEGIN
	CREATE TYPE medical_record_type_enum AS ENUM ('condition','surgery','allergy');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
	CREATE TYPE allergy_category_enum AS ENUM ('food','environmental','medication');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
	CREATE TYPE allergy_severity_enum AS ENUM ('mild','medium','severe');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- 'active' already covers Ongoing. resolved/chronic/monitoring stay for the rows
-- that already use them.
ALTER TYPE medical_condition_status_enum ADD VALUE IF NOT EXISTS 'controlled';
ALTER TYPE medical_condition_status_enum ADD VALUE IF NOT EXISTS 'remission';


ALTER TABLE medical_condition
	ADD COLUMN IF NOT EXISTS "type" public."medical_record_type_enum" DEFAULT 'condition' NOT NULL,
	ADD COLUMN IF NOT EXISTS notes text NULL,
	ADD COLUMN IF NOT EXISTS reaction varchar(255) NULL,
	ADD COLUMN IF NOT EXISTS category public."allergy_category_enum" NULL,
	ADD COLUMN IF NOT EXISTS severity public."allergy_severity_enum" NULL,
	ADD COLUMN IF NOT EXISTS recovery_expected_on date NULL;

-- An allergy's "since when" is optional on the form.
ALTER TABLE medical_condition ALTER COLUMN started_on DROP NOT NULL;

ALTER TABLE medical_condition
	ADD CONSTRAINT chk_medical_condition_shape CHECK (
		CASE "type"
			WHEN 'allergy' THEN category IS NOT NULL AND severity IS NOT NULL
			ELSE started_on IS NOT NULL
		END
	);

CREATE INDEX IF NOT EXISTS idx_medical_condition_user_type
	ON public.medical_condition USING btree (user_id, "type");


-- Medications linked to a record. The pair is the key, so the same medicine
-- cannot be linked to the same record twice.
CREATE TABLE IF NOT EXISTS medical_record_medicine (
	record_id int4 NOT NULL,
	tracking_id int4 NOT NULL,
	CONSTRAINT pk_medical_record_medicine PRIMARY KEY (record_id, tracking_id),
	CONSTRAINT fk_medical_record_medicine_record FOREIGN KEY (record_id)
		REFERENCES medical_condition(id) ON DELETE CASCADE,
	CONSTRAINT fk_medical_record_medicine_tracking FOREIGN KEY (tracking_id)
		REFERENCES medicine_tracking(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_medical_record_medicine_tracking
	ON public.medical_record_medicine USING btree (tracking_id);

-- ============================================================
-- V8__medical_history_date_order.sql
-- ============================================================
-- A record's dates have to run forwards. The service already refuses both of
-- these, but a rule that lives only in one method is one new caller away from
-- being skipped, and the rows outlive the code that wrote them.
--
-- Added NOT VALID: this enforces every insert and update from now on without
-- scanning what is already there. A deployed database may hold rows written
-- before the service checked, and a migration that fails on historical data
-- takes the release down with it. Run
--   ALTER TABLE medical_condition VALIDATE CONSTRAINT chk_medical_condition_date_order;
-- once the existing rows are known to be clean; it takes a lock but no rewrite.

ALTER TABLE medical_condition
	ADD CONSTRAINT chk_medical_condition_date_order
	CHECK (ended_on IS NULL OR started_on IS NULL OR ended_on >= started_on)
	NOT VALID;

-- Recovery cannot be expected before the surgery happens. started_on is a
-- timestamptz and this is a date, so the comparison fixes the zone rather than
-- reading the session's — a CHECK has to mean the same thing whoever writes.
ALTER TABLE medical_condition
	ADD CONSTRAINT chk_medical_condition_recovery_after_surgery
	CHECK (recovery_expected_on IS NULL OR started_on IS NULL
	       OR recovery_expected_on >= (started_on AT TIME ZONE 'UTC')::date)
	NOT VALID;

-- ============================================================
-- V9__period_pause_and_pregnancy_dates.sql
-- ============================================================
-- =============================================================================
-- MyHealthNotion :: why tracking is paused, and how far along a pregnancy is
--
-- V5 gave period_settings a bare paused_until date. It recorded that the user
-- wanted quiet and nothing about why, which makes it impossible to say anything
-- useful when the pause is the most significant thing happening to them — a
-- pregnancy is not the same silence as a fortnight's travel and should not read
-- like one.
--
-- Two additions, deliberately kept apart:
--
--   * period_settings gains a reason, a start and a note. The pause is a MUTE.
--     It decides nothing about whether a bleed counts as a period; that is
--     period_status and its generated booleans, unchanged here.
--   * period_status gains two dates, pregnancy_start and delivery_date — the two
--     halves of the same question, "how far along is this?", asked either side of
--     a birth. They are clinical anchors, so they live with the rest of the
--     clinical record: a bleed read against 14 March is read against the
--     circumstances in force on 14 March, and these are part of those.
--
-- Gestational week, month, trimester, due date, and the postpartum month and
-- fourth-trimester window, are all derived from those two dates against TODAY, so
-- none of them can be a generated column: CURRENT_DATE is not immutable. They are
-- computed on read in PeriodServiceImpl, for the same reason "not in the future"
-- is an application rule there.
-- =============================================================================


-- =============================================================================
-- 1. pause_reason_enum
-- =============================================================================

-- pregnancy, postpartum and breastfeeding duplicate a fact period_status already
-- holds. They are here anyway because "why is the app quiet?" is a question about
-- the pause, not about clinical history — and PeriodServiceImpl refuses a pause
-- for any of the three unless the status in force agrees, so the duplication can
-- never become a disagreement.
DO $$ BEGIN
	CREATE TYPE pause_reason_enum AS ENUM (
		'pregnancy','postpartum','breastfeeding','contraception','medical_treatment',
		'illness','gender_affirming_hormones','travel','stress','personal','other'
	);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;


-- =============================================================================
-- 2. period_settings — the pause, with a reason and a start
-- =============================================================================

ALTER TABLE public.period_settings
	ADD COLUMN IF NOT EXISTS pause_reason public."pause_reason_enum" NULL,
	ADD COLUMN IF NOT EXISTS paused_from date NULL,
	ADD COLUMN IF NOT EXISTS pause_note varchar(255) NULL;

-- Any pause already in flight predates the reason column, so the only honest
-- value is 'other'. LEAST() keeps an already-expired pause from ending before it
-- started, which would fail chk_period_settings_pause_window below.
UPDATE public.period_settings
SET paused_from  = LEAST(created_at::date, paused_until),
    pause_reason = 'other'::pause_reason_enum
WHERE paused_until IS NOT NULL AND paused_from IS NULL;

DO $$ BEGIN
	ALTER TABLE public.period_settings
		-- A pause without a reason, or a reason without a pause, is half a record.
		ADD CONSTRAINT chk_period_settings_pause_pair CHECK (
			(paused_from IS NULL) = (pause_reason IS NULL)),
		-- paused_from, not paused_until, is what says the user is paused.
		ADD CONSTRAINT chk_period_settings_pause_anchor CHECK (
			paused_until IS NULL OR paused_from IS NOT NULL),
		ADD CONSTRAINT chk_period_settings_pause_window CHECK (
			paused_until IS NULL OR paused_from IS NULL OR paused_until >= paused_from);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

COMMENT ON COLUMN public.period_settings.paused_until IS
	'Last day of the pause, or NULL for one with no end in sight — a pregnancy pause '
	'does not know its own end date, and forcing a guess would be worse than leaving '
	'it open. paused_from is what says the user is paused; this only bounds it.';

COMMENT ON COLUMN public.period_settings.paused_from IS
	'First day of the pause. Also the anchor for "which month of the pause is this", '
	'which is the only sensible answer for a reason that has no clinical clock of its '
	'own. A pregnancy has one — period_status.pregnancy_start — and that wins.';


-- =============================================================================
-- 3. period_status — the two anchors, either side of a birth
-- =============================================================================

-- Each is the clock for exactly one pregnancy state, and each is scoped to it:
-- pregnancy_start while pregnant, delivery_date once postpartum. They are never
-- both set, because they never both mean anything at once. Keeping them as two
-- columns rather than one "anchor date" plus its state is what lets the CHECKs
-- below say which is which — one nullable column shared by both would have to
-- trust the application to remember what it was currently measuring.
ALTER TABLE public.period_status
	ADD COLUMN IF NOT EXISTS pregnancy_start date NULL,
	ADD COLUMN IF NOT EXISTS delivery_date   date NULL;

DO $$ BEGIN
	ALTER TABLE public.period_status
		-- Only meaningful while pregnant. PeriodServiceImpl clears it as pregnancy
		-- leaves 'pregnant', rather than letting a stale LMP copy forward into a
		-- postpartum row and be read as an ongoing pregnancy.
		ADD CONSTRAINT chk_period_status_pregnancy_start CHECK (
			pregnancy_start IS NULL OR pregnancy = 'pregnant'::pregnancy_enum),
		-- A pregnancy in force on a day cannot have begun after it. Safe under the
		-- snapshot copy-forward, since effective_from only ever moves later.
		ADD CONSTRAINT chk_period_status_pregnancy_start_order CHECK (
			pregnancy_start IS NULL OR pregnancy_start <= effective_from),
		-- The mirror image, and cleared the same way when postpartum ends. Menses
		-- returning moves the user to 'not_pregnant', and a delivery date left behind
		-- on that row would keep a postpartum clock running for someone who is not.
		ADD CONSTRAINT chk_period_status_delivery_date CHECK (
			delivery_date IS NULL OR pregnancy = 'postpartum'::pregnancy_enum),
		ADD CONSTRAINT chk_period_status_delivery_date_order CHECK (
			delivery_date IS NULL OR delivery_date <= effective_from);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- The upper bounds — about 44 weeks of gestation, about two years postpartum —
-- are deliberately NOT CHECKs. Either would hold at the moment of writing and then
-- be broken by a user who simply stopped updating: copying a nine-month-old LMP
-- forward onto a new row would start failing, and a 500 on "I changed my
-- contraception" is a bad way to learn someone forgot to record a birth.
-- PeriodServiceImpl bounds both on input and flags a stale record on read instead.
--
-- Note the postpartum bound is generous on purpose. pregnancy_enum's own
-- documentation puts the return of menses anywhere from six weeks to over a year,
-- strongly affected by lactation, so a year postpartum is ordinary rather than
-- suspicious and nothing here should treat it as an error.
COMMENT ON COLUMN public.period_status.pregnancy_start IS
	'First day of the last menstrual period, the standard obstetric anchor. Gestational '
	'age, month, trimester and estimated due date are all derived from it on read — '
	'never stored, and never generated columns, since all four depend on CURRENT_DATE.';

COMMENT ON COLUMN public.period_status.delivery_date IS
	'The day the pregnancy ended. The postpartum month and the twelve-week fourth '
	'trimester are derived from it on read, on the same terms as pregnancy_start: '
	'never stored, never generated, because both move with the calendar.';

-- ============================================================
-- V10__ai_name_check.sql
-- ============================================================
-- The patient name a document printed, and what we concluded about it.
--
-- On ai_report_classifications because that is the stage that reads the name, and because
-- it already carries document_id — the stable key. Run items are per-attempt, so a retry
-- creates a new one; a decision the user made must outlive that.
--
-- identity_confirmed_at being non-null IS the user's acceptance. No separate table: the
-- fact belongs to the classification it overrides.
ALTER TABLE ai_report_classifications
	ADD COLUMN IF NOT EXISTS patient_name          varchar(255),
	ADD COLUMN IF NOT EXISTS name_match            varchar(16),
	ADD COLUMN IF NOT EXISTS identity_confirmed_at timestamptz;

-- The gate looks up the latest settled verdict for a document on every run.
CREATE INDEX IF NOT EXISTS ix_ai_clf_document_name_match
	ON ai_report_classifications (document_id, name_match);

COMMENT ON COLUMN ai_report_classifications.name_match IS
	'match | mismatch | unknown — computed by app/services/names.py, never by the model.';

-- ============================================================
-- V11__mood_tracking.sql
-- ============================================================
-- =============================================================================
-- MyHealthNotion :: mood check-ins
--
-- One row per user per calendar day, upserted. A mood log answers "how was my
-- day", so the day is the identity: logging again in the evening corrects the
-- morning's answer rather than competing with it, and the row as it stands at
-- midnight is how the day ended. That is what the History page reads, and it is
-- why there is a unique constraint here rather than an append-only stream.
--
-- score is the raw 1-10 slider stop. The seven display bands (Very Unpleasant ..
-- Very Pleasant) are NOT stored: they are a presentation grouping of the same
-- number, derived in MoodScale, and storing them would mean a migration every
-- time the wording moves plus a second source of truth that can disagree with
-- the first. It also keeps the column orderable and averageable, which the
-- week/month/year charts need — unlike flow_intensity_enum, whose declaration
-- order deliberately does not match its meaning.
--
-- factors is text[] for the same reason period_day_log.symptoms is: seventeen
-- entries, the only aggregate that matters is a count, and adding a chip stays a
-- Java constant reviewed like any other change instead of a production INSERT.
-- =============================================================================

CREATE TABLE IF NOT EXISTS mood_log (
	id         bigserial   NOT NULL,
	user_id    uuid        NOT NULL,
	log_date   date        NOT NULL,

	-- The slider stop the user left the thumb on. MoodScale maps it to a band.
	score      int2        NOT NULL,

	-- Codes from the MoodFactor Java enum, validated on write. An empty array is
	-- a real answer — "I logged a score and skipped the chips" — and is not the
	-- same as "no factor applied"; anything counting factors must use
	-- entries-with-factors as its denominator.
	factors    text[]      DEFAULT '{}'::text[] NOT NULL,

	created_at timestamptz DEFAULT now() NOT NULL,
	-- The moment the day's mood was last recorded, and what the History row shows
	-- next to the score. It has to be this rather than created_at: the time shown
	-- must belong to the score shown, and an evening correction replaces both.
	updated_at timestamptz DEFAULT now() NOT NULL,

	CONSTRAINT pk_mood_log PRIMARY KEY (id),
	-- One mood per day per user. Also the backstop against a double-tapped Save
	-- racing itself into two rows; MoodServiceImpl.findOrCreateDay catches the
	-- violation and re-reads rather than surfacing it.
	CONSTRAINT uq_mood_log_user_date UNIQUE (user_id, log_date),
	CONSTRAINT chk_mood_log_score CHECK (score BETWEEN 1 AND 10),
	CONSTRAINT fk_mood_log_user FOREIGN KEY (user_id)
		REFERENCES "user"(id) ON DELETE CASCADE
);

-- Every read is "this user's days in this window, newest first": the history
-- list, the calendar dots and the dashboard series are one query with one sort.
CREATE INDEX IF NOT EXISTS idx_mood_log_user_date
	ON public.mood_log USING btree (user_id, log_date DESC);

-- "Not in the future" is deliberately NOT a CHECK: now()/CURRENT_DATE are not
-- IMMUTABLE, so PostgreSQL refuses them in a constraint. It is an application
-- rule in MoodServiceImpl. Noted here so nobody tries.

-- No GIN index on factors: nothing counts them in SQL. Analytics is computed in
-- the client over the rows GET /mood/days already returns. If a server-side
-- factor aggregate ever lands, add
--   CREATE INDEX ... ON public.mood_log USING gin (factors);
-- and register it in a startup guard in the same commit, the way PeriodIndexCheck
-- does — ddl-auto=update cannot express a GIN index, so it would silently vanish
-- on any database Flyway did not reach.

-- ============================================================
-- V12__employee.sql
-- ============================================================
CREATE TABLE IF NOT EXISTS employee (
	id            bigserial   NOT NULL,
	employee_name varchar(150) NOT NULL,
	role          varchar(100) NOT NULL,
	email         varchar(255) NOT NULL,
	contact_no    varchar(20),
	status        varchar(20)  NOT NULL DEFAULT 'ACTIVE',
	password_hash text         NOT NULL,

	created_at    timestamptz  DEFAULT now() NOT NULL,
	updated_at    timestamptz  DEFAULT now() NOT NULL,

	CONSTRAINT pk_employee PRIMARY KEY (id),
	CONSTRAINT uq_employee_email UNIQUE (email),
	CONSTRAINT chk_employee_status CHECK (status IN ('ACTIVE', 'INACTIVE'))
);

CREATE INDEX IF NOT EXISTS idx_employee_status
	ON public.employee USING btree (status);

CREATE INDEX IF NOT EXISTS idx_employee_role
	ON public.employee USING btree (role);

-- ============================================================
-- V13__document_name_and_date.sql
-- ============================================================
-- A document's own name and date, on every section table.
--
-- Two facts a filed document could not carry until now.
--
-- The NAME is the one the user typed at upload. It lives on `unclassified_files.name`
-- and was destroyed the moment the document was filed, by both movers -- Spring's and the
-- AI service's -- because no section table had a column to put it in. Only `reports` did,
-- and only because Hibernate made it.
--
-- The DATE is the one printed on the document: when the sample was collected, the scan
-- performed, the bill issued. Nothing read it, so every list fell back to `created_at`,
-- which is the moment the AI service filed the row -- not when the document was issued,
-- and not even when it was uploaded.
--
-- Most of these columns already exist in deployed databases, created by Hibernate from
-- the JPA entities (`spring.jpa.hibernate.ddl-auto=update`) and invisible to Flyway. A
-- Flyway-built database -- CI, a fresh local, the AI service's Alembic-built test DB --
-- has none of them, which is why writing to them would work in production and fail in CI.
-- Every statement is IF NOT EXISTS, so this is a no-op wherever Hibernate got there first
-- and the real thing everywhere else.
--
-- `insurance.from_date` / `to_date` are deliberately untouched: a policy's validity
-- period is a different fact from the date the document was issued, and conflating them
-- would date every policy by the day its cover starts.

ALTER TABLE reports       ADD COLUMN IF NOT EXISTS "name" varchar(255) NULL,
                          ADD COLUMN IF NOT EXISTS "date" timestamptz NULL;
ALTER TABLE scans_imaging ADD COLUMN IF NOT EXISTS "name" varchar(255) NULL,
                          ADD COLUMN IF NOT EXISTS "date" timestamptz NULL;
ALTER TABLE insurance     ADD COLUMN IF NOT EXISTS "name" varchar(255) NULL,
                          ADD COLUMN IF NOT EXISTS "date" timestamptz NULL;
ALTER TABLE vaccinations  ADD COLUMN IF NOT EXISTS "name" varchar(255) NULL,
                          ADD COLUMN IF NOT EXISTS "date" timestamptz NULL;
ALTER TABLE prescriptions ADD COLUMN IF NOT EXISTS "name" varchar(255) NULL,
                          ADD COLUMN IF NOT EXISTS "date" timestamptz NULL;
ALTER TABLE bills         ADD COLUMN IF NOT EXISTS "name" varchar(255) NULL,
                          ADD COLUMN IF NOT EXISTS "date" timestamptz NULL;

-- The AI's own reading, kept apart from the user-facing column above so that a correction
-- the user makes is never confused with what the model transcribed. The label is the
-- provenance that matters: a report prints a collection date, a received date and a
-- release date, and when the wrong one is chosen the label is the whole diagnosis.
ALTER TABLE ai_report_classifications
  ADD COLUMN IF NOT EXISTS document_date       date        NULL,
  ADD COLUMN IF NOT EXISTS document_date_label varchar(64) NULL;

-- ============================================================
-- V14__dashboard_tables.sql
-- ============================================================
-- ============================================================================
-- davi_rnd_schema_final.sql
-- Complete R&D dashboard schema delta for the MHN database — single-file deploy.
-- Generated 2026-08-22 from the Staff Dashboard v2 R&D Build Pack, current as of
-- the latest revisions: NO vaccines module, warning-only range model
-- (danger columns exist for compatibility, pinned to graph bounds by the app).
--
-- Apply on top of the current production schema (Updated_schema.sql baseline).
-- Idempotent: guarded types, IF NOT EXISTS tables/columns/indexes,
-- ON CONFLICT DO NOTHING seed — safe to re-run.
--
-- Contents:
--   PART 1  Enum types (3 new; reuses existing dosage_form_enum / resource_type_enum)
--   PART 2  7 new tables, 8 extended Spring tables, data migration, triggers, view
--   PART 3  Seed: roles + permission keys, employee->role mapping, 20 drinks
--   PART 4  AI-service-owned tables (condition_registry, risk_rules,
--           insight_templates) — same database; if the AI team ports this into
--           their Alembic chain instead, delete PART 4 before running.
--
-- Tested end-to-end on PostgreSQL 16 + pgvector against Updated_schema.sql.
-- ============================================================================

-- ============================================================================
-- PART 1 · ENUM TYPES
-- ============================================================================

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'reference_status_enum') THEN
        CREATE TYPE public.reference_status_enum AS ENUM (
            'draft',
            'pending',
            'approved',
            'rejected',
            'archived',
            'merged');
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'approval_status_enum') THEN
        CREATE TYPE public.approval_status_enum AS ENUM (
            'pending',
            'approved',
            'rejected',
            'cancelled');
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'notification_priority_enum') THEN
        CREATE TYPE public.notification_priority_enum AS ENUM (
            'push',
            'priority',
            'full_screen');
    END IF;
END
$$;

-- ============================================================================
-- PART 2 · TABLES, EXTENSIONS, MIGRATION, TRIGGERS, VIEW
-- ============================================================================

CREATE TABLE IF NOT EXISTS public.role (
    id          serial4 PRIMARY KEY,
    name        varchar(100) NOT NULL,
    slug        varchar(50)  NOT NULL,
    description varchar(255),
    is_system   bool         NOT NULL DEFAULT false,
    permissions jsonb        NOT NULL DEFAULT '[]'::jsonb,
    created_by  int8         REFERENCES public.employee(id) ON DELETE SET NULL,
    created_at  timestamptz  NOT NULL DEFAULT now(),
    updated_at  timestamptz  NOT NULL DEFAULT now(),
    CONSTRAINT uq_role_slug UNIQUE (slug),
    CONSTRAINT uq_role_name UNIQUE (name)
);
CREATE INDEX IF NOT EXISTS idx_role_created_by ON public.role (created_by);

-- Every dashboard action; also the reference-data change log.
CREATE TABLE IF NOT EXISTS public.employee_activity_log (
    id             bigserial PRIMARY KEY,
    employee_id    int8        NOT NULL REFERENCES public.employee(id) ON DELETE CASCADE,
    activity_type  varchar(32) NOT NULL,
    resource_type  varchar(64),
    resource_id    varchar(64),
    target_user_id uuid        REFERENCES public."user"(id) ON DELETE SET NULL,
    details        jsonb,
    ip_addr        inet,
    user_agent     varchar(255),
    occurred_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_eal_employee_time ON public.employee_activity_log (employee_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_eal_resource      ON public.employee_activity_log (resource_type, resource_id);
CREATE INDEX IF NOT EXISTS idx_eal_target_user   ON public.employee_activity_log (target_user_id, occurred_at DESC);

-- One maker-checker queue for every approvable entity (the Approvals Inbox).
CREATE TABLE IF NOT EXISTS public.approval_request (
    id           bigserial PRIMARY KEY,
    entity_type  varchar(48) NOT NULL,
    entity_id    varchar(64) NOT NULL,
    action       varchar(16) NOT NULL,
    payload_diff jsonb,
    requested_by int8        NOT NULL REFERENCES public.employee(id) ON DELETE CASCADE,
    requested_at timestamptz NOT NULL DEFAULT now(),
    status       public.approval_status_enum NOT NULL DEFAULT 'pending',
    reviewed_by  int8        REFERENCES public.employee(id) ON DELETE SET NULL,
    reviewed_at  timestamptz,
    review_note  varchar(500)
);
CREATE INDEX IF NOT EXISTS idx_approval_status  ON public.approval_request (status, requested_at);
CREATE INDEX IF NOT EXISTS idx_approval_entity  ON public.approval_request (entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_approval_by      ON public.approval_request (requested_by);
CREATE INDEX IF NOT EXISTS idx_approval_reviewer ON public.approval_request (reviewed_by);

-- Aliases as rows with approval state. thp_id NULL + status rejected records
-- staff decisions on OCR names that are not parameters.
CREATE TABLE IF NOT EXISTS public.thp_alias (
    id               serial4 PRIMARY KEY,
    thp_id           int4        REFERENCES public.traditional_health_parameters(id) ON DELETE CASCADE,
    alias            varchar(150) NOT NULL,
    source           varchar(16) NOT NULL DEFAULT 'staff',      -- staff / ocr_suggestion / ai_suggestion / migrated
    status           public.reference_status_enum NOT NULL DEFAULT 'approved',
    occurrence_count int4        NOT NULL DEFAULT 0,
    created_by       int8        REFERENCES public.employee(id) ON DELETE SET NULL,
    approved_by      int8        REFERENCES public.employee(id) ON DELETE SET NULL,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_thp_alias UNIQUE (alias)
);
CREATE INDEX IF NOT EXISTS idx_thp_alias_thp    ON public.thp_alias (thp_id);
CREATE INDEX IF NOT EXISTS idx_thp_alias_status ON public.thp_alias (status);

-- Drink catalogue for the Caffeine and Alcohol trackers.
CREATE TABLE IF NOT EXISTS public.drink_master (
    id                          serial4 PRIMARY KEY,
    name                        varchar(150) NOT NULL,
    name_normalized             varchar(150) NOT NULL,
    kind                        varchar(16)  NOT NULL,   -- caffeinated / non_caffeinated / alcoholic
    category                    varchar(24)  NOT NULL,   -- coffee / tea / herbal_tea / energy_drink / soda / juice / milk / water / beer / wine / spirits / cocktail / other
    caffeine_mg_per_serving     numeric(7,2),
    serving_size_ml             int2,
    alcohol_abv_percent         numeric(5,2),
    standard_units_per_serving  numeric(5,2),
    icon_key                    varchar(40),
    synonyms                    jsonb,
    status                      public.reference_status_enum NOT NULL DEFAULT 'approved',
    merged_into_id              int4        REFERENCES public.drink_master(id) ON DELETE SET NULL,
    submitted_by_user_id        uuid        REFERENCES public."user"(id) ON DELETE SET NULL,
    approved_by                 int8        REFERENCES public.employee(id) ON DELETE SET NULL,
    usage_count                 int4        NOT NULL DEFAULT 0,
    created_at                  timestamptz NOT NULL DEFAULT now(),
    updated_at                  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_drink_name_kind UNIQUE (name_normalized, kind),
    CONSTRAINT chk_drink_caffeine CHECK (kind <> 'caffeinated' OR caffeine_mg_per_serving IS NOT NULL),
    CONSTRAINT chk_drink_abv      CHECK (kind <> 'alcoholic'  OR alcohol_abv_percent IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS idx_drink_kind_status ON public.drink_master (kind, status);
CREATE INDEX IF NOT EXISTS idx_drink_merged_into ON public.drink_master (merged_into_id);
CREATE INDEX IF NOT EXISTS idx_drink_submitted_by ON public.drink_master (submitted_by_user_id);

-- Medicines detected on a prescription, matched to the masters.
CREATE TABLE IF NOT EXISTS public.prescription_item (
    id                serial4 PRIMARY KEY,
    prescription_id   int4         NOT NULL REFERENCES public.prescriptions(id) ON DELETE CASCADE,
    drug_name_raw     varchar(255) NOT NULL,
    medicine_id       int4         REFERENCES public.medicine_master(id) ON DELETE SET NULL,
    drug_reference_id uuid         REFERENCES public.drug_reference(id) ON DELETE SET NULL,
    match_status      varchar(12)  NOT NULL DEFAULT 'unmatched',  -- matched / unmatched / ambiguous / manual
    match_confidence  numeric(4,3),
    strength          varchar(100),
    dosage_form       public.dosage_form_enum,
    frequency_per_day int2,
    duration_days     int2,
    instructions      varchar(255),
    is_duplicate      bool         NOT NULL DEFAULT false,
    tracking_id       int4         REFERENCES public.medicine_tracking(id) ON DELETE SET NULL,
    created_at        timestamptz  NOT NULL DEFAULT now(),
    updated_at        timestamptz  NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_presc_item_prescription ON public.prescription_item (prescription_id);
CREATE INDEX IF NOT EXISTS idx_presc_item_status       ON public.prescription_item (match_status);
CREATE INDEX IF NOT EXISTS idx_presc_item_medicine     ON public.prescription_item (medicine_id);

-- One row per extracted parameter, matched to THP and zoned. PHI table.
CREATE TABLE IF NOT EXISTS public.report_parameter_value (
    id               bigserial PRIMARY KEY,
    user_id          uuid         NOT NULL REFERENCES public."user"(id) ON DELETE CASCADE,
    section          public.resource_type_enum NOT NULL,   -- reports / scans_imaging
    record_id        int4         NOT NULL,
    run_item_id      uuid         REFERENCES public.ai_processing_run_items(id) ON DELETE SET NULL,
    thp_id           int4         REFERENCES public.traditional_health_parameters(id) ON DELETE SET NULL,  -- NULL = unmatched (R&D queue)
    test_name_raw    varchar(256) NOT NULL,
    value_raw        varchar(128),
    unit_raw         varchar(64),
    value_numeric    numeric(14,4),
    value_text       varchar(128),
    value_canonical  numeric(14,4),
    doc_range_low    numeric(14,4),
    doc_range_high   numeric(14,4),
    zone             varchar(16),                          -- warning_low / ideal / warning_high / unknown
    matched_via      varchar(12)  NOT NULL DEFAULT 'none', -- exact / alias / ai / manual / none
    match_confidence numeric(4,3),
    measured_on      date,
    created_at       timestamptz  NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_rpv_user_thp_date ON public.report_parameter_value (user_id, thp_id, measured_on DESC);
CREATE INDEX IF NOT EXISTS idx_rpv_section       ON public.report_parameter_value (section, record_id);
CREATE INDEX IF NOT EXISTS idx_rpv_raw_name      ON public.report_parameter_value (lower(test_name_raw));
CREATE INDEX IF NOT EXISTS idx_rpv_unmatched     ON public.report_parameter_value (created_at) WHERE thp_id IS NULL;
CREATE INDEX IF NOT EXISTS idx_rpv_run_item      ON public.report_parameter_value (run_item_id);

-- ============================================================================
-- 2. EXTENDED SPRING TABLES
-- ============================================================================

-- employee: single role reference, MFA, lock-out, session revocation, soft delete
ALTER TABLE public.employee
    ADD COLUMN IF NOT EXISTS role_id             int4 REFERENCES public.role(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS employee_code       varchar(20),
    ADD COLUMN IF NOT EXISTS image               varchar(500),
    ADD COLUMN IF NOT EXISTS mfa_enabled         bool NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS mfa_secret          text,
    ADD COLUMN IF NOT EXISTS must_reset_password bool NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS failed_login_count  int2 NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS last_login_at       timestamptz,
    ADD COLUMN IF NOT EXISTS token_version       int4 NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS created_by          int8 REFERENCES public.employee(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS deleted_at          timestamptz;
ALTER TABLE public.employee DROP CONSTRAINT IF EXISTS chk_employee_status;
ALTER TABLE public.employee ADD CONSTRAINT chk_employee_status
    CHECK (status IN ('ACTIVE', 'INACTIVE', 'LOCKED'));
CREATE UNIQUE INDEX IF NOT EXISTS uq_employee_code ON public.employee (employee_code) WHERE employee_code IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_employee_role_id ON public.employee (role_id);

-- traditional_health_parameters: code, category, value type, workflow, assignment, versioning
ALTER TABLE public.traditional_health_parameters
    ADD COLUMN IF NOT EXISTS code               varchar(32),
    ADD COLUMN IF NOT EXISTS category           varchar(8)  NOT NULL DEFAULT 'lab',    -- lab / scan / vital
    ADD COLUMN IF NOT EXISTS value_type         varchar(12) NOT NULL DEFAULT 'float',  -- float / boolean / categorical
    ADD COLUMN IF NOT EXISTS allow_manual_entry bool        NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS status             public.reference_status_enum NOT NULL DEFAULT 'draft',
    ADD COLUMN IF NOT EXISTS assigned_to        int8 REFERENCES public.employee(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS created_by         int8 REFERENCES public.employee(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS approved_by        int8 REFERENCES public.employee(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS approved_at        timestamptz,
    ADD COLUMN IF NOT EXISTS ai_integrated      bool        NOT NULL DEFAULT true,
    ADD COLUMN IF NOT EXISTS version            int4        NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS created_at         timestamptz NOT NULL DEFAULT now(),
    ADD COLUMN IF NOT EXISTS updated_at         timestamptz NOT NULL DEFAULT now(),
    ADD COLUMN IF NOT EXISTS deleted_at         timestamptz;
CREATE UNIQUE INDEX IF NOT EXISTS uq_thp_code ON public.traditional_health_parameters (code) WHERE code IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_thp_assigned_to ON public.traditional_health_parameters (assigned_to);

-- thp_age_range: sex dimension + approval + normal values for boolean/categorical
ALTER TABLE public.thp_age_range
    ADD COLUMN IF NOT EXISTS sex           varchar(8)  NOT NULL DEFAULT 'any',   -- any / male / female
    ADD COLUMN IF NOT EXISTS normal_values jsonb,
    ADD COLUMN IF NOT EXISTS source_note   varchar(255),
    ADD COLUMN IF NOT EXISTS status        public.reference_status_enum NOT NULL DEFAULT 'approved',
    ADD COLUMN IF NOT EXISTS version       int4        NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS created_by    int8 REFERENCES public.employee(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS approved_by   int8 REFERENCES public.employee(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS updated_at    timestamptz NOT NULL DEFAULT now();
DROP INDEX IF EXISTS idx_age_range_thp_bounds;
CREATE UNIQUE INDEX idx_age_range_thp_bounds ON public.thp_age_range (thp_id, sex, age_min, age_max);

-- medicine_master: search-first master with workflow + user submissions
ALTER TABLE public.medicine_master
    ADD COLUMN IF NOT EXISTS name_normalized           varchar(255),
    ADD COLUMN IF NOT EXISTS generic_name              varchar(255),
    ADD COLUMN IF NOT EXISTS default_frequency_per_day int2,
    ADD COLUMN IF NOT EXISTS default_slots             varchar(4),    -- M / A / E / N letters
    ADD COLUMN IF NOT EXISTS icon_key                  varchar(40),
    ADD COLUMN IF NOT EXISTS source                    varchar(16) NOT NULL DEFAULT 'seed',  -- seed / import / user_submission / staff
    ADD COLUMN IF NOT EXISTS status                    public.reference_status_enum NOT NULL DEFAULT 'approved',
    ADD COLUMN IF NOT EXISTS merged_into_id            int4 REFERENCES public.medicine_master(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS drug_reference_id         uuid REFERENCES public.drug_reference(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS submitted_by_user_id      uuid REFERENCES public."user"(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS approved_by               int8 REFERENCES public.employee(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS approved_at               timestamptz,
    ADD COLUMN IF NOT EXISTS usage_count               int4        NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS search_vector             tsvector,
    ADD COLUMN IF NOT EXISTS created_at                timestamptz NOT NULL DEFAULT now(),
    ADD COLUMN IF NOT EXISTS updated_at                timestamptz NOT NULL DEFAULT now(),
    ADD COLUMN IF NOT EXISTS deleted_at                timestamptz;
CREATE INDEX IF NOT EXISTS idx_medicine_master_search          ON public.medicine_master USING gin (search_vector);
CREATE INDEX IF NOT EXISTS idx_medicine_master_name_normalized ON public.medicine_master (name_normalized);
CREATE INDEX IF NOT EXISTS idx_medicine_master_status          ON public.medicine_master (status);
CREATE INDEX IF NOT EXISTS idx_medicine_master_merged_into     ON public.medicine_master (merged_into_id);
CREATE INDEX IF NOT EXISTS idx_medicine_master_drug_ref        ON public.medicine_master (drug_reference_id);
CREATE INDEX IF NOT EXISTS idx_medicine_master_submitted_by    ON public.medicine_master (submitted_by_user_id);

-- medical_condition: registry code, family link, allergy episodes, surgery tracking
ALTER TABLE public.medical_condition
    ADD COLUMN IF NOT EXISTS condition_code         varchar(32),
    ADD COLUMN IF NOT EXISTS is_family_linked       bool  NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS family_linked_relations jsonb,
    ADD COLUMN IF NOT EXISTS medication_for_allergy varchar(255),
    ADD COLUMN IF NOT EXISTS episodes               jsonb NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS scheduled_on           date,
    ADD COLUMN IF NOT EXISTS surgery_status         varchar(10),   -- upcoming / ongoing / recovering / completed
    ADD COLUMN IF NOT EXISTS recovery_started_on    date,
    ADD COLUMN IF NOT EXISTS recovery_actual_end    date,
    ADD COLUMN IF NOT EXISTS hospital               int4 REFERENCES public.hospital(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS surgeon_name           varchar(255),
    ADD COLUMN IF NOT EXISTS age_at_event           int2,
    ADD COLUMN IF NOT EXISTS source                 varchar(16) NOT NULL DEFAULT 'user',  -- user / family_member / report_ai / prescription_ai
    ADD COLUMN IF NOT EXISTS updated_at             timestamptz NOT NULL DEFAULT now(),
    ADD COLUMN IF NOT EXISTS deleted_at             timestamptz;
CREATE INDEX IF NOT EXISTS idx_medical_condition_code ON public.medical_condition (condition_code);
CREATE INDEX IF NOT EXISTS idx_medical_condition_hospital ON public.medical_condition (hospital);

-- lifestyle_log: drink reference + computed caffeine / alcohol snapshot
ALTER TABLE public.lifestyle_log
    ADD COLUMN IF NOT EXISTS drink_id      int4 REFERENCES public.drink_master(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS caffeine_mg   numeric(8,2),
    ADD COLUMN IF NOT EXISTS alcohol_units numeric(6,2),
    ADD COLUMN IF NOT EXISTS source        varchar(16) NOT NULL DEFAULT 'manual';  -- quick_add / search / manual / reminder_action
CREATE INDEX IF NOT EXISTS idx_lifestyle_log_drink ON public.lifestyle_log (drink_id);

-- prescriptions: digital vs handwritten path, drug validation, bill link
ALTER TABLE public.prescriptions
    ADD COLUMN IF NOT EXISTS source_file_id        int4 REFERENCES public.unclassified_files(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS name                  varchar(255),
    ADD COLUMN IF NOT EXISTS prescribed_by         varchar(255),
    ADD COLUMN IF NOT EXISTS duration_days         int2,
    ADD COLUMN IF NOT EXISTS is_digital            bool,
    ADD COLUMN IF NOT EXISTS validation_status     varchar(12) NOT NULL DEFAULT 'pending',  -- pending / validated / partial / needs_bill / skipped
    ADD COLUMN IF NOT EXISTS bill_id               int4 REFERENCES public.bills(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS meds_added_to_tracker bool NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS insights_requested    bool NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS insights_status       varchar(16) NOT NULL DEFAULT 'none',
    ADD COLUMN IF NOT EXISTS updated_at            timestamptz NOT NULL DEFAULT now(),
    ADD COLUMN IF NOT EXISTS deleted_at            timestamptz;
CREATE INDEX IF NOT EXISTS idx_prescriptions_bill ON public.prescriptions (bill_id);
CREATE INDEX IF NOT EXISTS idx_prescriptions_source_file ON public.prescriptions (source_file_id);

-- medicine_tracking: icon, reminder priority, source, prescription link, adherence
ALTER TABLE public.medicine_tracking
    ADD COLUMN IF NOT EXISTS icon_key              varchar(40),
    ADD COLUMN IF NOT EXISTS reminder_enabled      bool NOT NULL DEFAULT true,
    ADD COLUMN IF NOT EXISTS notification_priority public.notification_priority_enum NOT NULL DEFAULT 'priority',
    ADD COLUMN IF NOT EXISTS source                varchar(16) NOT NULL DEFAULT 'search',  -- search / manual / prescription / voice
    ADD COLUMN IF NOT EXISTS prescription_item_id  int4 REFERENCES public.prescription_item(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS adherence_rate_30d    numeric(5,2),
    ADD COLUMN IF NOT EXISTS deleted_at            timestamptz;
CREATE INDEX IF NOT EXISTS idx_medicine_tracking_presc_item ON public.medicine_tracking (prescription_item_id);

-- ============================================================================
-- 3. DATA MIGRATION
-- ============================================================================

-- Derive workflow status from the legacy approved flag BEFORE the mirror
-- trigger exists (existing approved rows go live untouched).
UPDATE public.traditional_health_parameters
SET status = CASE WHEN approved IS TRUE THEN 'approved'::public.reference_status_enum
                  ELSE 'draft'::public.reference_status_enum END;

-- J1 · aliases varchar[] -> thp_alias rows. The array stays read-only until
-- the AI matcher switches to thp_alias (open question Q1).
INSERT INTO public.thp_alias (thp_id, alias, source, status)
SELECT t.id, lower(trim(a)), 'migrated', 'approved'::public.reference_status_enum
FROM public.traditional_health_parameters t
CROSS JOIN LATERAL unnest(t.aliases) AS a
WHERE t.aliases IS NOT NULL AND trim(a) <> ''
ON CONFLICT (alias) DO NOTHING;

-- Backfill medicine_master search columns once (kept fresh by the trigger below).
UPDATE public.medicine_master
SET name_normalized = lower(regexp_replace(name, '[^a-zA-Z0-9]+', ' ', 'g')),
    search_vector   = to_tsvector('simple',
        coalesce(name, '') || ' ' ||
        coalesce(generic_name, '') || ' ' ||
        lower(regexp_replace(name, '[^a-zA-Z0-9]+', ' ', 'g')));

-- ============================================================================
-- 4. TRIGGERS
-- ============================================================================

-- J6 · keep the legacy approved flag equal to status = 'approved' so the
-- current matcher keeps working until it reads status.
CREATE OR REPLACE FUNCTION public.thp_status_mirror() RETURNS trigger AS $$
BEGIN
    NEW.approved := (NEW.status = 'approved');
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_thp_status_mirror ON public.traditional_health_parameters;
CREATE TRIGGER trg_thp_status_mirror
    BEFORE INSERT OR UPDATE ON public.traditional_health_parameters
    FOR EACH ROW EXECUTE FUNCTION public.thp_status_mirror();

-- J5 · maintain name_normalized + full-text search_vector on medicine_master.
-- (Composition from drug_reference is folded in by the nightly usage job, not
-- here — cross-table lookups in a row trigger are a lock hazard.)
CREATE OR REPLACE FUNCTION public.medicine_master_search() RETURNS trigger AS $$
BEGIN
    NEW.name_normalized := lower(regexp_replace(NEW.name, '[^a-zA-Z0-9]+', ' ', 'g'));
    NEW.search_vector := to_tsvector('simple',
        coalesce(NEW.name, '') || ' ' ||
        coalesce(NEW.generic_name, '') || ' ' ||
        coalesce(NEW.name_normalized, ''));
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_medicine_master_search ON public.medicine_master;
CREATE TRIGGER trg_medicine_master_search
    BEFORE INSERT OR UPDATE OF name, generic_name ON public.medicine_master
    FOR EACH ROW EXECUTE FUNCTION public.medicine_master_search();

-- ============================================================================
-- 5. VIEW — the Parameter Matching queue
-- ============================================================================

CREATE OR REPLACE VIEW public.v_unmatched_parameter AS
SELECT
    lower(rpv.test_name_raw)                             AS ocr_name,
    rpv.section                                          AS section,
    count(*)                                             AS occurrences,
    min(rpv.created_at)                                  AS first_seen,
    max(rpv.created_at)                                  AS last_seen,
    (array_agg(rpv.value_raw) FILTER (WHERE rpv.value_raw IS NOT NULL))[1:3] AS sample_values,
    (array_agg(DISTINCT rpv.unit_raw) FILTER (WHERE rpv.unit_raw IS NOT NULL))[1:3] AS sample_units
FROM public.report_parameter_value rpv
WHERE rpv.thp_id IS NULL
  AND NOT EXISTS (
      SELECT 1 FROM public.thp_alias a
      WHERE a.alias = lower(rpv.test_name_raw)
        AND a.status = 'rejected'::public.reference_status_enum
  )
GROUP BY lower(rpv.test_name_raw), rpv.section;

-- ============================================================================
-- PART 3 · SEED DATA
-- ============================================================================

INSERT INTO public.role (name, slug, description, is_system, permissions) VALUES
('Super Admin', 'super_admin', 'Everything, plus role seeding until the Admin dashboard exists', true,
 '["*"]'::jsonb),
('Medical R&D', 'medical_rnd', 'Maker: creates and edits reference data, submits for approval', true,
 '["thp.read","thp.create","thp.update","thp.assign","thp.alias.review","thp.range.manage",
   "parameter_match.read","parameter_match.decide",
   "mcp.read","mcp.create","mcp.update",
   "reference.read","reference.create","reference.update","reference.import",
   "rules.read","rules.update","approval.read","audit.reference_changes.read"]'::jsonb),
('Doctor R&D (Clinical Reviewer)', 'doctor_rnd', 'Checker: everything a maker can, plus approvals and merges', true,
 '["thp.read","thp.create","thp.update","thp.assign","thp.alias.review","thp.range.manage",
   "thp.approve","thp.archive",
   "parameter_match.read","parameter_match.decide",
   "mcp.read","mcp.create","mcp.update","mcp.approve","mcp.merge",
   "reference.read","reference.create","reference.update","reference.import",
   "reference.approve","reference.merge",
   "rules.read","rules.update","rules.approve",
   "approval.read","approval.decide","audit.reference_changes.read"]'::jsonb),
('Data & AI Ops', 'ai_ops', 'Read-only access to masters and queues for pipeline debugging', true,
 '["thp.read","parameter_match.read","mcp.read","reference.read","rules.read",
   "approval.read","audit.reference_changes.read"]'::jsonb)
ON CONFLICT (slug) DO NOTHING;

-- ============================================================================
-- 2. EMPLOYEE -> ROLE MAPPING (adjust the ILIKE patterns to your data)
-- ============================================================================

UPDATE public.employee e SET role_id = r.id
FROM public.role r
WHERE e.role_id IS NULL AND r.slug = 'super_admin'  AND e.role ILIKE '%admin%';

UPDATE public.employee e SET role_id = r.id
FROM public.role r
WHERE e.role_id IS NULL AND r.slug = 'doctor_rnd'   AND (e.role ILIKE '%doctor%' OR e.role ILIKE '%clinical%');

UPDATE public.employee e SET role_id = r.id
FROM public.role r
WHERE e.role_id IS NULL AND r.slug = 'medical_rnd'  AND (e.role ILIKE '%r&d%' OR e.role ILIKE '%research%' OR e.role ILIKE '%medical%');

UPDATE public.employee e SET role_id = r.id
FROM public.role r
WHERE e.role_id IS NULL AND r.slug = 'ai_ops'       AND (e.role ILIKE '%ops%' OR e.role ILIKE '%data%' OR e.role ILIKE '%ai%');

-- ============================================================================
-- 3. DRINKS (20)
-- ============================================================================

INSERT INTO public.drink_master
    (name, name_normalized, kind, category, caffeine_mg_per_serving, serving_size_ml, alcohol_abv_percent, standard_units_per_serving, status, icon_key)
VALUES
('Filter Coffee',          'filter coffee',          'caffeinated',     'coffee',       90,   150, NULL,  NULL, 'approved', 'coffee'),
('Instant Coffee',         'instant coffee',         'caffeinated',     'coffee',       60,   200, NULL,  NULL, 'approved', 'coffee'),
('Espresso (single)',      'espresso single',        'caffeinated',     'coffee',       63,    30, NULL,  NULL, 'approved', 'coffee'),
('Cappuccino',             'cappuccino',             'caffeinated',     'coffee',       75,   180, NULL,  NULL, 'approved', 'coffee'),
('Cold Coffee',            'cold coffee',            'caffeinated',     'coffee',       65,   250, NULL,  NULL, 'approved', 'coffee'),
('Masala Chai',            'masala chai',            'caffeinated',     'tea',          40,   150, NULL,  NULL, 'approved', 'cup-soda'),
('Green Tea',              'green tea',              'caffeinated',     'tea',          28,   200, NULL,  NULL, 'approved', 'cup-soda'),
('Black Tea',              'black tea',              'caffeinated',     'tea',          47,   200, NULL,  NULL, 'approved', 'cup-soda'),
('Red Bull',               'red bull',               'caffeinated',     'energy_drink', 80,   250, NULL,  NULL, 'approved', 'zap'),
('Monster Energy',         'monster energy',         'caffeinated',     'energy_drink', 160,  500, NULL,  NULL, 'approved', 'zap'),
('Coca-Cola',              'coca cola',              'caffeinated',     'soda',         34,   330, NULL,  NULL, 'approved', 'glass-water'),
('Chamomile Tea',          'chamomile tea',          'non_caffeinated', 'herbal_tea',   NULL, 200, NULL,  NULL, 'approved', 'cup-soda'),
('Fresh Lime Soda',        'fresh lime soda',        'non_caffeinated', 'soda',         NULL, 300, NULL,  NULL, 'approved', 'glass-water'),
('Tender Coconut Water',   'tender coconut water',   'non_caffeinated', 'juice',        NULL, 300, NULL,  NULL, 'approved', 'glass-water'),
('Buttermilk (Chaas)',     'buttermilk chaas',       'non_caffeinated', 'milk',         NULL, 200, NULL,  NULL, 'approved', 'milk'),
('Lager Beer (pint)',      'lager beer pint',        'alcoholic',       'beer',         NULL, 330, 5.0,   1.3,  'approved', 'beer'),
('Strong Beer (pint)',     'strong beer pint',       'alcoholic',       'beer',         NULL, 330, 8.0,   2.1,  'approved', 'beer'),
('Red Wine (glass)',       'red wine glass',         'alcoholic',       'wine',         NULL, 150, 13.5,  1.6,  'approved', 'wine'),
('Whisky (30 ml)',         'whisky 30 ml',           'alcoholic',       'spirits',      NULL,  30, 42.8,  1.0,  'approved', 'glass-water'),
('Vodka (30 ml)',          'vodka 30 ml',            'alcoholic',       'spirits',      NULL,  30, 40.0,  0.9,  'approved', 'glass-water')
ON CONFLICT (name_normalized, kind) DO NOTHING;

-- ============================================================================
-- PART 4 · AI-SERVICE-OWNED TABLES
-- ============================================================================

ALTER TABLE public.condition_registry
    ADD COLUMN IF NOT EXISTS kind                     varchar(12) NOT NULL DEFAULT 'condition',  -- condition / allergen
    ADD COLUMN IF NOT EXISTS icd10_code               varchar(10),
    ADD COLUMN IF NOT EXISTS category                 varchar(60),   -- body system, or allergy category for allergens
    ADD COLUMN IF NOT EXISTS description              text,
    ADD COLUMN IF NOT EXISTS is_hereditary            bool NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS status                   public.reference_status_enum NOT NULL DEFAULT 'approved',
    ADD COLUMN IF NOT EXISTS merged_into_code         varchar(32),
    ADD COLUMN IF NOT EXISTS submitted_by_user_id     uuid,          -- no FK: AI-side tables never FK "user"
    ADD COLUMN IF NOT EXISTS submitted_by_employee_id int8,
    ADD COLUMN IF NOT EXISTS submitted_at             timestamptz,
    ADD COLUMN IF NOT EXISTS approved_by              int8,
    ADD COLUMN IF NOT EXISTS approved_at              timestamptz,
    ADD COLUMN IF NOT EXISTS usage_count              int4 NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS updated_at               timestamptz NOT NULL DEFAULT now();
CREATE INDEX IF NOT EXISTS idx_condition_registry_kind_status ON public.condition_registry (kind, status);

-- risk_rules: maker-checker audit for rules edited from R&D
ALTER TABLE public.risk_rules
    ADD COLUMN IF NOT EXISTS created_by  int8,
    ADD COLUMN IF NOT EXISTS approved_by int8,
    ADD COLUMN IF NOT EXISTS approved_at timestamptz,
    ADD COLUMN IF NOT EXISTS created_at  timestamptz NOT NULL DEFAULT now(),
    ADD COLUMN IF NOT EXISTS updated_at  timestamptz NOT NULL DEFAULT now();

-- insight_templates: becomes the insight definition catalogue
ALTER TABLE public.insight_templates
    ADD COLUMN IF NOT EXISTS name                 varchar(120),
    ADD COLUMN IF NOT EXISTS scope                varchar(14),   -- condition / document / tracker / wearable / home / weekly_report
    ADD COLUMN IF NOT EXISTS tracker_key          varchar(12),
    ADD COLUMN IF NOT EXISTS cadence              varchar(10),   -- on_event / daily / weekly / monthly
    ADD COLUMN IF NOT EXISTS min_data_requirement jsonb,         -- e.g. {"nights": 7}
    ADD COLUMN IF NOT EXISTS tier_required        varchar(10),   -- basic / premium
    ADD COLUMN IF NOT EXISTS copy_guidelines      text,
    ADD COLUMN IF NOT EXISTS prompt_version       varchar(32),
    ADD COLUMN IF NOT EXISTS created_by           int8,
    ADD COLUMN IF NOT EXISTS approved_by          int8,
    ADD COLUMN IF NOT EXISTS approved_at          timestamptz,
    ADD COLUMN IF NOT EXISTS created_at           timestamptz NOT NULL DEFAULT now(),
    ADD COLUMN IF NOT EXISTS updated_at           timestamptz NOT NULL DEFAULT now();

-- ============================================================
-- V15__staff_admin_role.sql
-- ============================================================
-- V15__staff_admin_role.sql
-- Staff management (mhn-dashboards): Ops Admin role with the staff.* keys.
-- Admins create accounts for any role with a one-time default password;
-- the dashboard forces a password change + MFA enrolment at first login.
-- Super Admin already covers these keys via ["*"].
-- Idempotent (ON CONFLICT DO NOTHING).

INSERT INTO public.role (name, slug, description, is_system, permissions) VALUES
('Ops Admin', 'ops_admin',
 'Staff administration: create accounts for any role, reset passwords, manage status', true,
 '["staff.read","staff.create","staff.update",
   "thp.read","parameter_match.read","mcp.read","reference.read","rules.read",
   "approval.read","audit.reference_changes.read"]'::jsonb)
ON CONFLICT (slug) DO NOTHING;

-- ============================================================
-- V16__medicine_tracking.sql
-- ============================================================
-- V16__medicine_tracking.sql
-- Makes the medicine tables in V1 usable by the tracking module:
--   1. trigram search, so a typo or a partial brand name still finds the drug
--   2. a real uniqueness rule on the catalogue, so re-running an import cannot double it
--   3. dose_config becomes the only per-slot store (dose_time is dropped)
--   4. the constraint and the generated column V1 got wrong
--   5. medicine_unmatched — what users typed that the catalogue does not have
-- Idempotent throughout: every statement is IF NOT EXISTS or guarded.


-- =============================================================================
-- 1. Catalogue search
-- =============================================================================

-- Search was findByNameContainingIgnoreCase — an unanchored LIKE '%x%', which no
-- btree index can serve, so every keystroke was a sequential scan of the whole
-- catalogue. Worse for the feature than for the database: it matched substrings
-- exactly or not at all, so 'paracetomol' returned nothing while paracetamol sat
-- in the table, and the user concluded their medicine was missing and typed it in
-- by hand. A large share of medicine_unmatched below would have been that, not a
-- genuine gap, which is why this lands in the same migration.
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE INDEX IF NOT EXISTS idx_medicine_master_name_trgm
	ON public.medicine_master USING gin (name gin_trgm_ops);


-- =============================================================================
-- 2. Catalogue uniqueness
-- =============================================================================

-- medicine_master had no uniqueness rule at all, so importing the same source
-- twice silently doubled it and search returned each drug as many times as it had
-- been loaded.
--
-- A plain UNIQUE (name, strength, manufacturer) would not have helped: both
-- strength and manufacturer are nullable, and PostgreSQL treats NULLs as distinct,
-- so the rows most likely to be duplicated — a bare name with no strength and no
-- manufacturer, which is what the current import produces — would still have been
-- allowed through in unlimited numbers. Hence coalesce, and hence an expression
-- index rather than a table constraint.

-- Existing duplicates have to go first or the index cannot be built. Tracking rows
-- are repointed at the survivor rather than orphaned: the FK is ON DELETE SET NULL,
-- so deleting the loser directly would quietly cut a user's medicine loose from the
-- catalogue to fix a problem that was never theirs.
DO $$
DECLARE
	repointed int;
	removed   int;
BEGIN
	WITH ranked AS (
		SELECT id,
		       first_value(id) OVER (
		           PARTITION BY lower(trim(name)), coalesce(strength, ''), coalesce(manufacturer, 0)
		           ORDER BY id
		       ) AS keep_id
		FROM public.medicine_master
	)
	UPDATE public.medicine_tracking mt
	SET medicine_id = r.keep_id
	FROM ranked r
	WHERE mt.medicine_id = r.id
	  AND r.id <> r.keep_id;
	GET DIAGNOSTICS repointed = ROW_COUNT;

	WITH ranked AS (
		SELECT id,
		       first_value(id) OVER (
		           PARTITION BY lower(trim(name)), coalesce(strength, ''), coalesce(manufacturer, 0)
		           ORDER BY id
		       ) AS keep_id
		FROM public.medicine_master
	)
	DELETE FROM public.medicine_master m
	USING ranked r
	WHERE m.id = r.id AND r.id <> r.keep_id;
	GET DIAGNOSTICS removed = ROW_COUNT;

	IF removed > 0 THEN
		RAISE NOTICE 'medicine_master: removed % duplicate row(s), repointed % tracking row(s).',
			removed, repointed;
	END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS uq_medicine_master_identity
	ON public.medicine_master (lower(trim(name)), coalesce(strength, ''), coalesce(manufacturer, 0));


-- =============================================================================
-- 3. dose_config is the only per-slot store
-- =============================================================================

-- V1 shipped both dose_config and dose_time, two jsonb columns for the same thing,
-- and instructions/medicine.md documents only the first. Nothing has ever written
-- either — the module was catalogue search alone — so this is free now and would
-- not be once the materializer depends on one of them.
ALTER TABLE public.medicine_tracking DROP COLUMN IF EXISTS dose_time;

COMMENT ON COLUMN public.medicine_tracking.dose_config IS
	'Per-slot time and quantity, keyed by the slot character: '
	'{"M":{"time":"08:00","qty":2},"E":{"time":"20:00","qty":1}}. Keys must be a '
	'subset of schedule_pattern. A slot with no entry falls back to the default time '
	'for that slot (M 08:00, A 13:00, E 18:00, N 21:00) and qty 1.';


-- =============================================================================
-- 4. The constraint and the generated column V1 got wrong
-- =============================================================================

-- period_tracking has its equivalent; medicine_tracking never got one, so a course
-- ending before it starts was storable and would simply never generate a dose.
DO $$ BEGIN
	ALTER TABLE public.medicine_tracking
		ADD CONSTRAINT chk_medicine_tracking_end CHECK (ends_at IS NULL OR ends_at >= starts_at);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- effective_end was GENERATED ALWAYS AS (GREATEST(ends_at, extended_till)), and
-- GREATEST ignores NULLs rather than propagating them — it returns NULL only when
-- every argument is NULL. So on a chronic course, where ends_at IS NULL means
-- "indefinite", setting extended_till turned effective_end from NULL into a finite
-- date, and the materializer's (effective_end IS NULL OR effective_end >= day)
-- filter then stopped generating doses on it. Extending a course would have ended
-- it. An indefinite course stays indefinite here whatever extended_till says.
--
-- The column is dropped and re-added rather than altered because a generated
-- expression cannot be changed in place. idx_medicine_tracking_user_active goes
-- with it (DROP COLUMN takes dependent indexes) and is rebuilt below.
DO $$
DECLARE
	expression text;
BEGIN
	SELECT pg_get_expr(d.adbin, d.adrelid)
	INTO expression
	FROM pg_attrdef d
	JOIN pg_attribute a ON a.attrelid = d.adrelid AND a.attnum = d.adnum
	WHERE d.adrelid = 'public.medicine_tracking'::regclass
	  AND a.attname = 'effective_end';

	-- Only rewrite the broken form. A database already carrying the CASE is left alone.
	IF expression IS NOT NULL AND expression NOT ILIKE '%CASE%' THEN
		ALTER TABLE public.medicine_tracking DROP COLUMN effective_end;
	END IF;
END $$;

DO $$ BEGIN
	ALTER TABLE public.medicine_tracking
		ADD COLUMN effective_end date
			GENERATED ALWAYS AS (
				CASE WHEN ends_at IS NULL THEN NULL
				     ELSE GREATEST(ends_at, extended_till) END
			) STORED;
EXCEPTION WHEN duplicate_column THEN NULL; END $$;

CREATE INDEX IF NOT EXISTS idx_medicine_tracking_user_active
	ON public.medicine_tracking USING btree (user_id, starts_at, effective_end)
	WHERE stopped_at IS NULL AND is_prn = false;

COMMENT ON COLUMN public.medicine_tracking.effective_end IS
	'The real last day of the course, or NULL for an indefinite one. A NULL ends_at '
	'wins over extended_till: extending a course must never be what ends it.';


-- =============================================================================
-- 5. The slot uniqueness rule stops applying to as-needed doses
-- =============================================================================

-- uq_medicine_dose_log_slot (tracking_id, scheduled_date, slot) is what makes the
-- materializer re-runnable: its ON CONFLICT DO NOTHING folds on this, so a job that
-- crashes halfway and runs again creates no duplicates.
--
-- That is a statement about SCHEDULED doses, and it was applied to every row. An
-- as-needed course has no schedule to be idempotent about — the user taps "took it"
-- whenever they take it — and painkillers, inhalers and antacids are taken more than
-- once a day. With four slot values the constraint capped a PRN course at four doses
-- per day, and the second dose in any slot failed on a unique violation the user had
-- no way to understand.
--
-- So it becomes partial. Scheduled rows keep the guarantee the materializer needs;
-- PRN rows, which only ever arrive one user action at a time, are unconstrained.
ALTER TABLE public.medicine_dose_log DROP CONSTRAINT IF EXISTS uq_medicine_dose_log_slot;

CREATE UNIQUE INDEX IF NOT EXISTS uq_medicine_dose_log_slot
	ON public.medicine_dose_log USING btree (tracking_id, scheduled_date, slot)
	WHERE is_prn = false;


-- =============================================================================
-- 6. medicine_unmatched — the catalogue gaps, ranked by how often they are hit
-- =============================================================================

-- A medicine the catalogue does not have must never block the user: medicine_id is
-- nullable and medicine_tracking snapshots name, strength and dosage_form, so the
-- course, its schedule and its dose log all work untouched. That was already true
-- in V1. What was missing is any record that it happened, so nobody could find out
-- which drugs the catalogue lacks.
--
-- Deliberately NOT medicine_master rows created on the fly. That table is shared
-- reference data read by every user's search, and it carries clinical fields —
-- prescription_reqd, side_effects, used_for — that we would be leaving null or,
-- worse, guessing at for a drug nobody has verified. One user's typo would become
-- everyone's search result, permanently, with tracking rows pointing at it.
--
-- This is a staging area instead: rows accumulate with a count, curation goes down
-- the list in demand order, and promotion into medicine_master is a deliberate act.
CREATE TABLE IF NOT EXISTS public.medicine_unmatched (
	id bigserial NOT NULL,

	-- The dedupe key. Lowercased, punctuation and whitespace collapsed, and any
	-- trailing strength stripped, so 'Dolo 650', 'dolo-650' and 'DOLO650 mg' are one
	-- row with a count of three rather than three rows with a count of one each.
	-- Computed in Java (MedicineNameNormaliser) rather than by a SQL expression so
	-- that the same normalisation is available to the capture path without a round
	-- trip, and so changing the rule is a code change with a test rather than a
	-- migration.
	normalised_name varchar(255) NOT NULL,

	-- What the user actually typed, most recent wins. Kept verbatim because it is
	-- what a curator needs in order to recognise the drug — the normalised form
	-- throws away exactly the detail that makes it identifiable.
	raw_name varchar(255) NOT NULL,

	-- Whatever the user told us alongside the name. Nullable and advisory: it is
	-- unverified user input and must never be promoted into medicine_master as fact
	-- without a curator confirming it.
	strength varchar(100) NULL,
	dosage_form public."dosage_form_enum" NULL,

	-- How many courses have been started with this name. The whole point of the
	-- table: it turns "the catalogue is incomplete" into a worklist in priority order.
	occurrences int4 DEFAULT 1 NOT NULL,
	first_seen_at timestamptz DEFAULT now() NOT NULL,
	last_seen_at timestamptz DEFAULT now() NOT NULL,

	-- Set when a curator promotes this into the catalogue. The row is kept rather
	-- than deleted so the same name arriving again is recognised as already handled
	-- instead of reopening a gap that was closed.
	resolved_medicine_id int4 NULL,
	resolved_at timestamptz NULL,

	CONSTRAINT pk_medicine_unmatched PRIMARY KEY (id),
	-- The ON CONFLICT arbiter for the capture upsert.
	CONSTRAINT uq_medicine_unmatched_name UNIQUE (normalised_name),
	CONSTRAINT chk_medicine_unmatched_resolved CHECK (
		(resolved_medicine_id IS NULL AND resolved_at IS NULL)
		OR (resolved_medicine_id IS NOT NULL AND resolved_at IS NOT NULL)),
	CONSTRAINT fk_medicine_unmatched_medicine_master
		FOREIGN KEY (resolved_medicine_id) REFERENCES public.medicine_master(id) ON DELETE SET NULL
);

-- The curation worklist: still open, most demanded first.
CREATE INDEX IF NOT EXISTS idx_medicine_unmatched_worklist
	ON public.medicine_unmatched USING btree (occurrences DESC, last_seen_at DESC)
	WHERE resolved_medicine_id IS NULL;

-- ============================================================
-- V17__drink_catalogue.sql
-- ============================================================
-- V17__drink_catalogue.sql
-- Drink catalogue import (282 curated drinks for the Caffeine & Alcohol
-- trackers), deduplicated against existing rows via the
-- UNIQUE (name_normalized, kind) key — ON CONFLICT DO NOTHING makes this
-- idempotent and preserves any staff edits to rows that already exist.

INSERT INTO public.drink_master
    (name, name_normalized, kind, category, caffeine_mg_per_serving, serving_size_ml,
     alcohol_abv_percent, standard_units_per_serving, icon_key, synonyms, status, usage_count)
VALUES
('Espresso', 'espresso', 'caffeinated', 'coffee', 63, 30, NULL, NULL, 'espresso_cup', '["single espresso", "espresso shot", "short black"]'::jsonb, 'approved', 0),
('Double espresso', 'double espresso', 'caffeinated', 'coffee', 126, 60, NULL, NULL, 'espresso_cup', '["doppio"]'::jsonb, 'approved', 0),
('Ristretto', 'ristretto', 'caffeinated', 'coffee', 63, 22, NULL, NULL, 'espresso_cup', NULL, 'approved', 0),
('Lungo', 'lungo', 'caffeinated', 'coffee', 77, 60, NULL, NULL, 'espresso_cup', NULL, 'approved', 0),
('Americano', 'americano', 'caffeinated', 'coffee', 94, 240, NULL, NULL, 'coffee_cup', '["caffe americano", "long black"]'::jsonb, 'approved', 0),
('Brewed coffee (drip)', 'brewed coffee drip', 'caffeinated', 'coffee', 95, 240, NULL, NULL, 'coffee_cup', '["drip coffee", "pour over", "black coffee", "chemex", "v60", "batch brew"]'::jsonb, 'approved', 0),
('French press coffee', 'french press coffee', 'caffeinated', 'coffee', 107, 240, NULL, NULL, 'coffee_cup', '["plunger coffee", "cafetiere coffee"]'::jsonb, 'approved', 0),
('Instant coffee', 'instant coffee', 'caffeinated', 'coffee', 60, 240, NULL, NULL, 'coffee_cup', '["nescafe", "bru coffee"]'::jsonb, 'approved', 0),
('Decaf coffee', 'decaf coffee', 'non_caffeinated', 'coffee', 3, 240, NULL, NULL, 'coffee_cup', '["decaffeinated coffee", "decaf"]'::jsonb, 'approved', 0),
('Cold brew coffee', 'cold brew coffee', 'caffeinated', 'coffee', 155, 355, NULL, NULL, 'iced_coffee', '["cold brew"]'::jsonb, 'approved', 0),
('Nitro cold brew', 'nitro cold brew', 'caffeinated', 'coffee', 215, 355, NULL, NULL, 'iced_coffee', '["nitro coffee"]'::jsonb, 'approved', 0),
('Iced coffee', 'iced coffee', 'caffeinated', 'coffee', 120, 355, NULL, NULL, 'iced_coffee', '["iced americano"]'::jsonb, 'approved', 0),
('Iced latte', 'iced latte', 'caffeinated', 'coffee', 125, 355, NULL, NULL, 'iced_coffee', NULL, 'approved', 0),
('Latte', 'latte', 'caffeinated', 'coffee', 63, 240, NULL, NULL, 'coffee_cup', '["cafe latte", "caffe latte", "vanilla latte", "caramel latte", "flavored latte"]'::jsonb, 'approved', 0),
('Cappuccino', 'cappuccino', 'caffeinated', 'coffee', 63, 180, NULL, NULL, 'coffee_cup', '["cappucino"]'::jsonb, 'approved', 0),
('Flat white', 'flat white', 'caffeinated', 'coffee', 130, 160, NULL, NULL, 'coffee_cup', NULL, 'approved', 0),
('Cortado', 'cortado', 'caffeinated', 'coffee', 63, 60, NULL, NULL, 'espresso_cup', '["gibraltar"]'::jsonb, 'approved', 0),
('Macchiato', 'macchiato', 'caffeinated', 'coffee', 63, 60, NULL, NULL, 'espresso_cup', '["espresso macchiato", "caramel macchiato"]'::jsonb, 'approved', 0),
('Mocha', 'mocha', 'caffeinated', 'coffee', 90, 240, NULL, NULL, 'coffee_cup', '["caffe mocha", "mochaccino", "chocolate coffee"]'::jsonb, 'approved', 0),
('Turkish coffee', 'turkish coffee', 'caffeinated', 'coffee', 60, 60, NULL, NULL, 'espresso_cup', '["greek coffee", "cezve coffee"]'::jsonb, 'approved', 0),
('Vietnamese iced coffee', 'vietnamese iced coffee', 'caffeinated', 'coffee', 100, 180, NULL, NULL, 'iced_coffee', '["ca phe sua da", "vietnamese coffee"]'::jsonb, 'approved', 0),
('South Indian filter coffee', 'south indian filter coffee', 'caffeinated', 'coffee', 60, 120, NULL, NULL, 'coffee_cup', '["filter kaapi", "kaapi", "madras coffee", "degree coffee", "kumbakonam coffee"]'::jsonb, 'approved', 0),
('Cold coffee (blended)', 'cold coffee blended', 'caffeinated', 'coffee', 90, 350, NULL, NULL, 'milkshake', '["coffee milkshake", "frappe", "frappuccino", "blended coffee"]'::jsonb, 'approved', 0),
('Affogato', 'affogato', 'caffeinated', 'coffee', 63, 90, NULL, NULL, 'espresso_cup', NULL, 'approved', 0),
('Bottled iced coffee (RTD)', 'bottled iced coffee rtd', 'caffeinated', 'coffee', 75, 240, NULL, NULL, 'iced_coffee', '["ready to drink coffee", "bottled frappuccino"]'::jsonb, 'approved', 0),
('Butter coffee', 'butter coffee', 'caffeinated', 'coffee', 95, 240, NULL, NULL, 'coffee_cup', '["bulletproof coffee", "keto coffee"]'::jsonb, 'approved', 0),
('Chicory coffee substitute', 'chicory coffee substitute', 'non_caffeinated', 'coffee', NULL, 240, NULL, NULL, 'coffee_cup', '["barley coffee", "roasted chicory", "caro", "postum"]'::jsonb, 'approved', 0),
('Coffee (other)', 'coffee other', 'caffeinated', 'coffee', 80, 240, NULL, NULL, 'coffee_cup', NULL, 'approved', 0),
('Black tea', 'black tea', 'caffeinated', 'tea', 47, 240, NULL, NULL, 'tea_cup', '["assam tea", "darjeeling tea", "ceylon tea", "english breakfast", "orange pekoe"]'::jsonb, 'approved', 0),
('Earl grey tea', 'earl grey tea', 'caffeinated', 'tea', 45, 240, NULL, NULL, 'tea_cup', '["bergamot tea"]'::jsonb, 'approved', 0),
('Green tea', 'green tea', 'caffeinated', 'tea', 28, 240, NULL, NULL, 'tea_cup', '["sencha", "gunpowder tea", "kahwa", "kashmiri kahwa"]'::jsonb, 'approved', 0),
('Jasmine tea', 'jasmine tea', 'caffeinated', 'tea', 25, 240, NULL, NULL, 'tea_cup', '["jasmine green tea"]'::jsonb, 'approved', 0),
('White tea', 'white tea', 'caffeinated', 'tea', 20, 240, NULL, NULL, 'tea_cup', '["silver needle"]'::jsonb, 'approved', 0),
('Oolong tea', 'oolong tea', 'caffeinated', 'tea', 37, 240, NULL, NULL, 'tea_cup', '["wulong tea"]'::jsonb, 'approved', 0),
('Pu-erh tea', 'pu erh tea', 'caffeinated', 'tea', 40, 240, NULL, NULL, 'tea_cup', '["puerh", "pu erh"]'::jsonb, 'approved', 0),
('Hojicha', 'hojicha', 'caffeinated', 'tea', 20, 240, NULL, NULL, 'tea_cup', '["roasted green tea"]'::jsonb, 'approved', 0),
('Genmaicha', 'genmaicha', 'caffeinated', 'tea', 25, 240, NULL, NULL, 'tea_cup', '["brown rice tea"]'::jsonb, 'approved', 0),
('Matcha', 'matcha', 'caffeinated', 'tea', 70, 240, NULL, NULL, 'matcha_bowl', '["whisked matcha", "ceremonial matcha"]'::jsonb, 'approved', 0),
('Matcha latte', 'matcha latte', 'caffeinated', 'tea', 65, 355, NULL, NULL, 'matcha_bowl', '["iced matcha latte"]'::jsonb, 'approved', 0),
('Masala chai', 'masala chai', 'caffeinated', 'tea', 40, 180, NULL, NULL, 'tea_cup', '["chai", "adrak chai", "ginger chai", "cutting chai", "karak chai", "kadak chai", "milk chai"]'::jsonb, 'approved', 0),
('Chai latte (cafe style)', 'chai latte cafe style', 'caffeinated', 'tea', 70, 355, NULL, NULL, 'tea_cup', '["chai tea latte"]'::jsonb, 'approved', 0),
('Hong Kong milk tea', 'hong kong milk tea', 'caffeinated', 'tea', 50, 240, NULL, NULL, 'tea_cup', '["milk tea", "teh tarik", "silk stocking tea"]'::jsonb, 'approved', 0),
('Bubble tea', 'bubble tea', 'caffeinated', 'tea', 130, 500, NULL, NULL, 'boba_tea', '["boba", "boba tea", "pearl milk tea", "tapioca tea", "brown sugar milk tea"]'::jsonb, 'approved', 0),
('Taro milk tea', 'taro milk tea', 'caffeinated', 'tea', 50, 500, NULL, NULL, 'boba_tea', '["taro boba"]'::jsonb, 'approved', 0),
('Iced tea', 'iced tea', 'caffeinated', 'tea', 25, 355, NULL, NULL, 'iced_tea', '["lemon iced tea", "peach iced tea", "sweet tea", "lipton iced tea"]'::jsonb, 'approved', 0),
('Thai iced tea', 'thai iced tea', 'caffeinated', 'tea', 45, 240, NULL, NULL, 'iced_tea', '["cha yen"]'::jsonb, 'approved', 0),
('London fog', 'london fog', 'caffeinated', 'tea', 40, 355, NULL, NULL, 'tea_cup', '["earl grey latte"]'::jsonb, 'approved', 0),
('Yerba mate', 'yerba mate', 'caffeinated', 'tea', 80, 240, NULL, NULL, 'tea_cup', '["mate", "chimarrao", "terere"]'::jsonb, 'approved', 0),
('Kombucha', 'kombucha', 'caffeinated', 'tea', 15, 240, 0.3, NULL, 'kombucha_bottle', '["fermented tea", "booch"]'::jsonb, 'approved', 0),
('Tea (other)', 'tea other', 'caffeinated', 'tea', 40, 240, NULL, NULL, 'tea_cup', NULL, 'approved', 0),
('Chamomile tea', 'chamomile tea', 'non_caffeinated', 'herbal_tea', NULL, 240, NULL, NULL, 'herbal_tea', '["camomile tea"]'::jsonb, 'approved', 0),
('Peppermint tea', 'peppermint tea', 'non_caffeinated', 'herbal_tea', NULL, 240, NULL, NULL, 'herbal_tea', '["mint tea", "pudina tea"]'::jsonb, 'approved', 0),
('Ginger tea (herbal)', 'ginger tea herbal', 'non_caffeinated', 'herbal_tea', NULL, 240, NULL, NULL, 'herbal_tea', '["ginger infusion", "ginger lemon honey tea"]'::jsonb, 'approved', 0),
('Hibiscus tea', 'hibiscus tea', 'non_caffeinated', 'herbal_tea', NULL, 240, NULL, NULL, 'herbal_tea', '["karkade", "roselle tea"]'::jsonb, 'approved', 0),
('Rooibos tea', 'rooibos tea', 'non_caffeinated', 'herbal_tea', NULL, 240, NULL, NULL, 'herbal_tea', '["red bush tea"]'::jsonb, 'approved', 0),
('Tulsi tea', 'tulsi tea', 'non_caffeinated', 'herbal_tea', NULL, 240, NULL, NULL, 'herbal_tea', '["holy basil tea"]'::jsonb, 'approved', 0),
('Lemongrass tea', 'lemongrass tea', 'non_caffeinated', 'herbal_tea', NULL, 240, NULL, NULL, 'herbal_tea', NULL, 'approved', 0),
('Fennel tea', 'fennel tea', 'non_caffeinated', 'herbal_tea', NULL, 240, NULL, NULL, 'herbal_tea', '["saunf tea"]'::jsonb, 'approved', 0),
('Herbal tea (other)', 'herbal tea other', 'non_caffeinated', 'herbal_tea', NULL, 240, NULL, NULL, 'herbal_tea', '["tisane", "herbal infusion", "detox tea"]'::jsonb, 'approved', 0),
('Energy drink (standard)', 'energy drink standard', 'caffeinated', 'energy_drink', 80, 250, NULL, NULL, 'energy_can', '["generic energy drink"]'::jsonb, 'approved', 0),
('Red Bull', 'red bull', 'caffeinated', 'energy_drink', 80, 250, NULL, NULL, 'energy_can', '["redbull"]'::jsonb, 'approved', 0),
('Monster Energy', 'monster energy', 'caffeinated', 'energy_drink', 160, 473, NULL, NULL, 'energy_can', '["monster"]'::jsonb, 'approved', 0),
('Sting', 'sting', 'caffeinated', 'energy_drink', 72, 250, NULL, NULL, 'energy_can', '["sting energy"]'::jsonb, 'approved', 0),
('Celsius', 'celsius', 'caffeinated', 'energy_drink', 200, 355, NULL, NULL, 'energy_can', '["celsius energy"]'::jsonb, 'approved', 0),
('Bang', 'bang', 'caffeinated', 'energy_drink', 300, 473, NULL, NULL, 'energy_can', '["bang energy"]'::jsonb, 'approved', 0),
('Rockstar', 'rockstar', 'caffeinated', 'energy_drink', 160, 473, NULL, NULL, 'energy_can', '["rockstar energy"]'::jsonb, 'approved', 0),
('Energy shot', 'energy shot', 'caffeinated', 'energy_drink', 200, 57, NULL, NULL, 'energy_shot', '["5 hour energy", "five hour energy"]'::jsonb, 'approved', 0),
('Pre-workout drink', 'pre workout drink', 'caffeinated', 'energy_drink', 200, 350, NULL, NULL, 'energy_shot', '["pre workout", "c4"]'::jsonb, 'approved', 0),
('Cola', 'cola', 'caffeinated', 'soda', 34, 355, NULL, NULL, 'soda_can', '["coke", "coca cola", "pepsi", "thums up", "rc cola", "campa cola"]'::jsonb, 'approved', 0),
('Diet cola', 'diet cola', 'caffeinated', 'soda', 46, 355, NULL, NULL, 'soda_can', '["diet coke", "coke zero", "diet pepsi", "pepsi black", "coca cola zero"]'::jsonb, 'approved', 0),
('Caffeine-free cola', 'caffeine free cola', 'non_caffeinated', 'soda', NULL, 355, NULL, NULL, 'soda_can', '["caffeine free coke"]'::jsonb, 'approved', 0),
('Cherry cola', 'cherry cola', 'caffeinated', 'soda', 34, 355, NULL, NULL, 'soda_can', '["cherry coke"]'::jsonb, 'approved', 0),
('Dr Pepper', 'dr pepper', 'caffeinated', 'soda', 41, 355, NULL, NULL, 'soda_can', '["pepper soda"]'::jsonb, 'approved', 0),
('Mountain Dew', 'mountain dew', 'caffeinated', 'soda', 54, 355, NULL, NULL, 'soda_can', '["mtn dew", "dew"]'::jsonb, 'approved', 0),
('Lemon-lime soda', 'lemon lime soda', 'non_caffeinated', 'soda', NULL, 355, NULL, NULL, 'soda_can', '["sprite", "7up", "seven up", "limca", "sierra mist"]'::jsonb, 'approved', 0),
('Orange soda', 'orange soda', 'non_caffeinated', 'soda', NULL, 355, NULL, NULL, 'soda_can', '["fanta", "mirinda", "crush", "orangeade"]'::jsonb, 'approved', 0),
('Root beer', 'root beer', 'non_caffeinated', 'soda', NULL, 355, NULL, NULL, 'soda_can', '["sarsaparilla"]'::jsonb, 'approved', 0),
('Cream soda', 'cream soda', 'non_caffeinated', 'soda', NULL, 355, NULL, NULL, 'soda_can', '["ice cream soda", "vanilla soda"]'::jsonb, 'approved', 0),
('Ginger ale', 'ginger ale', 'non_caffeinated', 'soda', NULL, 355, NULL, NULL, 'soda_can', NULL, 'approved', 0),
('Ginger beer (non-alcoholic)', 'ginger beer non alcoholic', 'non_caffeinated', 'soda', NULL, 355, NULL, NULL, 'soda_can', NULL, 'approved', 0),
('Club soda', 'club soda', 'non_caffeinated', 'soda', NULL, 355, NULL, NULL, 'soda_can', '["soda water", "charged water", "plain soda"]'::jsonb, 'approved', 0),
('Tonic water', 'tonic water', 'non_caffeinated', 'soda', NULL, 355, NULL, NULL, 'soda_can', '["indian tonic water"]'::jsonb, 'approved', 0),
('Masala soda', 'masala soda', 'non_caffeinated', 'soda', NULL, 300, NULL, NULL, 'soda_can', '["jeera soda", "jeera masala soda", "banta", "goli soda", "masala lemon soda"]'::jsonb, 'approved', 0),
('Guarana soda', 'guarana soda', 'caffeinated', 'soda', 30, 355, NULL, NULL, 'soda_can', '["guarana antarctica"]'::jsonb, 'approved', 0),
('Fruit soda (other)', 'fruit soda other', 'non_caffeinated', 'soda', NULL, 355, NULL, NULL, 'soda_can', '["grape soda", "strawberry soda", "appy fizz"]'::jsonb, 'approved', 0),
('Soda (other)', 'soda other', 'non_caffeinated', 'soda', NULL, 355, NULL, NULL, 'soda_can', '["soft drink", "pop", "fizzy drink", "cold drink"]'::jsonb, 'approved', 0),
('Orange juice', 'orange juice', 'non_caffeinated', 'juice', NULL, 240, NULL, NULL, 'juice_glass', '["oj", "santra juice"]'::jsonb, 'approved', 0),
('Apple juice', 'apple juice', 'non_caffeinated', 'juice', NULL, 240, NULL, NULL, 'juice_glass', '["apple drink"]'::jsonb, 'approved', 0),
('Grape juice', 'grape juice', 'non_caffeinated', 'juice', NULL, 240, NULL, NULL, 'juice_glass', NULL, 'approved', 0),
('Cranberry juice', 'cranberry juice', 'non_caffeinated', 'juice', NULL, 240, NULL, NULL, 'juice_glass', NULL, 'approved', 0),
('Pineapple juice', 'pineapple juice', 'non_caffeinated', 'juice', NULL, 240, NULL, NULL, 'juice_glass', NULL, 'approved', 0),
('Mango juice', 'mango juice', 'non_caffeinated', 'juice', NULL, 240, NULL, NULL, 'juice_glass', '["frooti", "maaza", "slice", "aam ras", "mango nectar", "mango drink"]'::jsonb, 'approved', 0),
('Pomegranate juice', 'pomegranate juice', 'non_caffeinated', 'juice', NULL, 240, NULL, NULL, 'juice_glass', '["anar juice"]'::jsonb, 'approved', 0),
('Watermelon juice', 'watermelon juice', 'non_caffeinated', 'juice', NULL, 240, NULL, NULL, 'juice_glass', '["tarbooz juice"]'::jsonb, 'approved', 0),
('Sweet lime juice', 'sweet lime juice', 'non_caffeinated', 'juice', NULL, 240, NULL, NULL, 'juice_glass', '["mosambi juice", "mausambi juice", "sweet lemon juice"]'::jsonb, 'approved', 0),
('Grapefruit juice', 'grapefruit juice', 'non_caffeinated', 'juice', NULL, 240, NULL, NULL, 'juice_glass', NULL, 'approved', 0),
('Guava juice', 'guava juice', 'non_caffeinated', 'juice', NULL, 240, NULL, NULL, 'juice_glass', '["amrood juice", "guava nectar"]'::jsonb, 'approved', 0),
('Lychee juice', 'lychee juice', 'non_caffeinated', 'juice', NULL, 240, NULL, NULL, 'juice_glass', '["litchi juice"]'::jsonb, 'approved', 0),
('Mixed fruit juice', 'mixed fruit juice', 'non_caffeinated', 'juice', NULL, 240, NULL, NULL, 'juice_glass', '["fruit cocktail juice"]'::jsonb, 'approved', 0),
('Tomato juice', 'tomato juice', 'non_caffeinated', 'juice', NULL, 240, NULL, NULL, 'juice_glass', NULL, 'approved', 0),
('Carrot juice', 'carrot juice', 'non_caffeinated', 'juice', NULL, 240, NULL, NULL, 'juice_glass', '["gajar juice"]'::jsonb, 'approved', 0),
('Beetroot juice', 'beetroot juice', 'non_caffeinated', 'juice', NULL, 240, NULL, NULL, 'juice_glass', '["beet juice"]'::jsonb, 'approved', 0),
('Amla juice', 'amla juice', 'non_caffeinated', 'juice', NULL, 240, NULL, NULL, 'juice_glass', '["indian gooseberry juice"]'::jsonb, 'approved', 0),
('Aloe vera juice', 'aloe vera juice', 'non_caffeinated', 'juice', NULL, 240, NULL, NULL, 'juice_glass', NULL, 'approved', 0),
('Green juice', 'green juice', 'non_caffeinated', 'juice', NULL, 240, NULL, NULL, 'juice_glass', '["celery juice", "spinach juice", "abc juice"]'::jsonb, 'approved', 0),
('Wheatgrass shot', 'wheatgrass shot', 'non_caffeinated', 'juice', NULL, 30, NULL, NULL, 'juice_glass', NULL, 'approved', 0),
('Sugarcane juice', 'sugarcane juice', 'non_caffeinated', 'juice', NULL, 300, NULL, NULL, 'juice_glass', '["ganne ka ras", "ganna juice", "karumbu juice"]'::jsonb, 'approved', 0),
('Prune juice', 'prune juice', 'non_caffeinated', 'juice', NULL, 240, NULL, NULL, 'juice_glass', NULL, 'approved', 0),
('Agua fresca', 'agua fresca', 'non_caffeinated', 'juice', NULL, 355, NULL, NULL, 'juice_glass', '["aguas frescas"]'::jsonb, 'approved', 0),
('Juice (other)', 'juice other', 'non_caffeinated', 'juice', NULL, 240, NULL, NULL, 'juice_glass', '["fresh juice", "fruit juice"]'::jsonb, 'approved', 0),
('Water', 'water', 'non_caffeinated', 'water', NULL, 250, NULL, NULL, 'water_glass', '["still water", "mineral water", "tap water", "plain water"]'::jsonb, 'approved', 0),
('Sparkling water', 'sparkling water', 'non_caffeinated', 'water', NULL, 250, NULL, NULL, 'sparkling_water', '["seltzer", "carbonated water", "perrier", "san pellegrino"]'::jsonb, 'approved', 0),
('Flavored sparkling water', 'flavored sparkling water', 'non_caffeinated', 'water', NULL, 355, NULL, NULL, 'sparkling_water', '["flavored seltzer"]'::jsonb, 'approved', 0),
('Coconut water', 'coconut water', 'non_caffeinated', 'water', NULL, 300, NULL, NULL, 'coconut', '["nariyal pani", "tender coconut water", "elaneer", "daab"]'::jsonb, 'approved', 0),
('Barley water', 'barley water', 'non_caffeinated', 'water', NULL, 240, NULL, NULL, 'water_glass', '["lemon barley water"]'::jsonb, 'approved', 0),
('Nimbu pani', 'nimbu pani', 'non_caffeinated', 'sherbet', NULL, 250, NULL, NULL, 'sherbet_glass', '["shikanji", "shikanjvi", "lemon water", "lime water", "indian lemonade"]'::jsonb, 'approved', 0),
('Lemonade', 'lemonade', 'non_caffeinated', 'sherbet', NULL, 355, NULL, NULL, 'sherbet_glass', '["pink lemonade", "limeade", "fresh lime soda"]'::jsonb, 'approved', 0),
('Jaljeera', 'jaljeera', 'non_caffeinated', 'sherbet', NULL, 250, NULL, NULL, 'sherbet_glass', '["jal jeera", "cumin cooler"]'::jsonb, 'approved', 0),
('Aam panna', 'aam panna', 'non_caffeinated', 'sherbet', NULL, 250, NULL, NULL, 'sherbet_glass', '["aam jhora", "raw mango cooler"]'::jsonb, 'approved', 0),
('Kokum sherbet', 'kokum sherbet', 'non_caffeinated', 'sherbet', NULL, 250, NULL, NULL, 'sherbet_glass', '["kokum sharbat", "kokum juice"]'::jsonb, 'approved', 0),
('Sol kadhi', 'sol kadhi', 'non_caffeinated', 'sherbet', NULL, 200, NULL, NULL, 'sherbet_glass', '["solkadhi"]'::jsonb, 'approved', 0),
('Rose sherbet', 'rose sherbet', 'non_caffeinated', 'sherbet', NULL, 250, NULL, NULL, 'sherbet_glass', '["rooh afza", "rose sharbat", "gulab sharbat"]'::jsonb, 'approved', 0),
('Khus sherbet', 'khus sherbet', 'non_caffeinated', 'sherbet', NULL, 250, NULL, NULL, 'sherbet_glass', '["khus sharbat", "vetiver sharbat"]'::jsonb, 'approved', 0),
('Bael sherbet', 'bael sherbet', 'non_caffeinated', 'sherbet', NULL, 250, NULL, NULL, 'sherbet_glass', '["bel sharbat", "bael ka sharbat", "wood apple juice"]'::jsonb, 'approved', 0),
('Sattu drink', 'sattu drink', 'non_caffeinated', 'sherbet', NULL, 300, NULL, NULL, 'sherbet_glass', '["sattu sharbat", "sattu ka ghol"]'::jsonb, 'approved', 0),
('Sherbet (other)', 'sherbet other', 'non_caffeinated', 'sherbet', NULL, 250, NULL, NULL, 'sherbet_glass', '["sharbat", "badam sharbat"]'::jsonb, 'approved', 0),
('Milk (whole)', 'milk whole', 'non_caffeinated', 'milk', NULL, 240, NULL, NULL, 'milk_glass', '["full cream milk", "full fat milk", "buffalo milk", "doodh"]'::jsonb, 'approved', 0),
('Milk (skim/toned)', 'milk skim toned', 'non_caffeinated', 'milk', NULL, 240, NULL, NULL, 'milk_glass', '["skimmed milk", "toned milk", "double toned milk", "low fat milk", "2 percent milk"]'::jsonb, 'approved', 0),
('Buttermilk', 'buttermilk', 'non_caffeinated', 'milk', NULL, 240, NULL, NULL, 'yogurt_drink', '["chaas", "chhach", "mattha", "majjiga", "neer mor"]'::jsonb, 'approved', 0),
('Sweet lassi', 'sweet lassi', 'non_caffeinated', 'milk', NULL, 250, NULL, NULL, 'yogurt_drink', '["lassi", "punjabi lassi"]'::jsonb, 'approved', 0),
('Mango lassi', 'mango lassi', 'non_caffeinated', 'milk', NULL, 250, NULL, NULL, 'yogurt_drink', NULL, 'approved', 0),
('Salted lassi', 'salted lassi', 'non_caffeinated', 'milk', NULL, 250, NULL, NULL, 'yogurt_drink', '["namkeen lassi"]'::jsonb, 'approved', 0),
('Kefir', 'kefir', 'non_caffeinated', 'milk', NULL, 240, NULL, NULL, 'yogurt_drink', '["milk kefir"]'::jsonb, 'approved', 0),
('Probiotic dairy drink', 'probiotic dairy drink', 'non_caffeinated', 'milk', NULL, 65, NULL, NULL, 'yogurt_drink', '["yakult"]'::jsonb, 'approved', 0),
('Drinking yogurt', 'drinking yogurt', 'non_caffeinated', 'milk', NULL, 240, NULL, NULL, 'yogurt_drink', '["ayran", "doogh", "laban"]'::jsonb, 'approved', 0),
('Badam milk', 'badam milk', 'non_caffeinated', 'milk', NULL, 200, NULL, NULL, 'milk_glass', '["badam doodh", "kesar badam milk"]'::jsonb, 'approved', 0),
('Rose milk', 'rose milk', 'non_caffeinated', 'milk', NULL, 240, NULL, NULL, 'milk_glass', '["rose doodh"]'::jsonb, 'approved', 0),
('Thandai', 'thandai', 'non_caffeinated', 'milk', NULL, 200, NULL, NULL, 'milk_glass', '["sardai"]'::jsonb, 'approved', 0),
('Golden milk', 'golden milk', 'non_caffeinated', 'milk', NULL, 240, NULL, NULL, 'milk_glass', '["haldi doodh", "turmeric milk", "turmeric latte"]'::jsonb, 'approved', 0),
('Hot chocolate', 'hot chocolate', 'non_caffeinated', 'milk', 5, 240, NULL, NULL, 'hot_chocolate', '["cocoa", "hot cocoa", "drinking chocolate"]'::jsonb, 'approved', 0),
('Chocolate milk', 'chocolate milk', 'non_caffeinated', 'milk', 5, 240, NULL, NULL, 'milk_glass', '["chocolate flavored milk"]'::jsonb, 'approved', 0),
('Malted milk drink', 'malted milk drink', 'non_caffeinated', 'milk', 4, 240, NULL, NULL, 'hot_chocolate', '["horlicks", "bournvita", "boost", "complan", "milo", "ovaltine", "malt drink"]'::jsonb, 'approved', 0),
('Milkshake (vanilla)', 'milkshake vanilla', 'non_caffeinated', 'milk', NULL, 300, NULL, NULL, 'milkshake', '["vanilla shake", "thick shake", "milkshake"]'::jsonb, 'approved', 0),
('Chocolate milkshake', 'chocolate milkshake', 'non_caffeinated', 'milk', 5, 300, NULL, NULL, 'milkshake', '["chocolate shake", "oreo shake", "brownie shake"]'::jsonb, 'approved', 0),
('Strawberry milkshake', 'strawberry milkshake', 'non_caffeinated', 'milk', NULL, 300, NULL, NULL, 'milkshake', '["strawberry shake"]'::jsonb, 'approved', 0),
('Banana shake', 'banana shake', 'non_caffeinated', 'milk', NULL, 300, NULL, NULL, 'milkshake', '["banana milkshake"]'::jsonb, 'approved', 0),
('Mango shake', 'mango shake', 'non_caffeinated', 'milk', NULL, 300, NULL, NULL, 'milkshake', '["mango milkshake"]'::jsonb, 'approved', 0),
('Falooda', 'falooda', 'non_caffeinated', 'milk', NULL, 300, NULL, NULL, 'milkshake', '["faluda", "royal falooda"]'::jsonb, 'approved', 0),
('Eggnog (non-alcoholic)', 'eggnog non alcoholic', 'non_caffeinated', 'milk', NULL, 150, NULL, NULL, 'milk_glass', '["egg nog"]'::jsonb, 'approved', 0),
('Horchata', 'horchata', 'non_caffeinated', 'milk', NULL, 240, NULL, NULL, 'milk_glass', NULL, 'approved', 0),
('Soy milk', 'soy milk', 'non_caffeinated', 'milk', NULL, 240, NULL, NULL, 'milk_glass', '["soya milk"]'::jsonb, 'approved', 0),
('Almond milk', 'almond milk', 'non_caffeinated', 'milk', NULL, 240, NULL, NULL, 'milk_glass', '["unsweetened almond milk"]'::jsonb, 'approved', 0),
('Oat milk', 'oat milk', 'non_caffeinated', 'milk', NULL, 240, NULL, NULL, 'milk_glass', NULL, 'approved', 0),
('Coconut milk drink', 'coconut milk drink', 'non_caffeinated', 'milk', NULL, 240, NULL, NULL, 'coconut', '["coconut milk beverage"]'::jsonb, 'approved', 0),
('Rice milk', 'rice milk', 'non_caffeinated', 'milk', NULL, 240, NULL, NULL, 'milk_glass', NULL, 'approved', 0),
('Fruit smoothie', 'fruit smoothie', 'non_caffeinated', 'smoothie', NULL, 350, NULL, NULL, 'smoothie_cup', '["berry smoothie", "banana smoothie"]'::jsonb, 'approved', 0),
('Green smoothie', 'green smoothie', 'non_caffeinated', 'smoothie', NULL, 350, NULL, NULL, 'smoothie_cup', '["spinach smoothie", "kale smoothie"]'::jsonb, 'approved', 0),
('Protein shake', 'protein shake', 'non_caffeinated', 'smoothie', NULL, 350, NULL, NULL, 'smoothie_cup', '["whey shake", "protein smoothie"]'::jsonb, 'approved', 0),
('Meal replacement shake', 'meal replacement shake', 'non_caffeinated', 'smoothie', NULL, 350, NULL, NULL, 'smoothie_cup', '["ensure", "huel", "slim fast"]'::jsonb, 'approved', 0),
('Sports drink', 'sports drink', 'non_caffeinated', 'sports_drink', NULL, 500, NULL, NULL, 'sports_bottle', '["gatorade", "powerade", "enerzal", "isotonic drink"]'::jsonb, 'approved', 0),
('Electrolyte drink', 'electrolyte drink', 'non_caffeinated', 'sports_drink', NULL, 250, NULL, NULL, 'sports_bottle', '["ors", "electral", "oral rehydration solution", "hydration mix", "lmnt"]'::jsonb, 'approved', 0),
('Vitamin water', 'vitamin water', 'non_caffeinated', 'sports_drink', NULL, 500, NULL, NULL, 'sports_bottle', '["enhanced water"]'::jsonb, 'approved', 0),
('Glucose drink', 'glucose drink', 'non_caffeinated', 'sports_drink', NULL, 250, NULL, NULL, 'sports_bottle', '["glucon d", "glucose d"]'::jsonb, 'approved', 0),
('Virgin mojito', 'virgin mojito', 'non_caffeinated', 'mocktail', NULL, 300, NULL, NULL, 'mocktail_glass', '["mint cooler", "virgin mint mojito"]'::jsonb, 'approved', 0),
('Shirley Temple', 'shirley temple', 'non_caffeinated', 'mocktail', NULL, 240, NULL, NULL, 'mocktail_glass', NULL, 'approved', 0),
('Virgin pina colada', 'virgin pina colada', 'non_caffeinated', 'mocktail', NULL, 300, NULL, NULL, 'mocktail_glass', NULL, 'approved', 0),
('Fruit punch', 'fruit punch', 'non_caffeinated', 'mocktail', NULL, 300, NULL, NULL, 'mocktail_glass', '["tropical punch"]'::jsonb, 'approved', 0),
('Mocktail (other)', 'mocktail other', 'non_caffeinated', 'mocktail', NULL, 300, NULL, NULL, 'mocktail_glass', '["virgin cocktail", "blue lagoon mocktail"]'::jsonb, 'approved', 0),
('Lager', 'lager', 'alcoholic', 'beer', NULL, 330, 5.0, 1.3, 'beer_mug', '["pale lager", "kingfisher", "heineken", "budweiser", "corona", "carlsberg", "tuborg"]'::jsonb, 'approved', 0),
('Strong lager', 'strong lager', 'alcoholic', 'beer', NULL, 330, 8.0, 2.08, 'beer_mug', '["strong beer", "kingfisher strong", "haywards 5000", "knock out", "tuborg strong"]'::jsonb, 'approved', 0),
('Pilsner', 'pilsner', 'alcoholic', 'beer', NULL, 330, 4.8, 1.25, 'beer_mug', '["pils"]'::jsonb, 'approved', 0),
('Light beer', 'light beer', 'alcoholic', 'beer', NULL, 330, 4.0, 1.04, 'beer_mug', '["bud light", "miller lite", "coors light"]'::jsonb, 'approved', 0),
('Non-alcoholic beer', 'non alcoholic beer', 'non_caffeinated', 'beer', NULL, 330, 0.5, NULL, 'beer_mug', '["zero beer", "na beer", "heineken 0 0", "budweiser zero"]'::jsonb, 'approved', 0),
('IPA', 'ipa', 'alcoholic', 'beer', NULL, 330, 6.5, 1.69, 'beer_mug', '["india pale ale", "hazy ipa", "neipa", "session ipa"]'::jsonb, 'approved', 0),
('Double IPA', 'double ipa', 'alcoholic', 'beer', NULL, 330, 8.5, 2.21, 'beer_mug', '["dipa", "imperial ipa", "triple ipa"]'::jsonb, 'approved', 0),
('Pale ale', 'pale ale', 'alcoholic', 'beer', NULL, 330, 5.5, 1.43, 'beer_mug', '["apa", "american pale ale", "bira blonde"]'::jsonb, 'approved', 0),
('Wheat beer', 'wheat beer', 'alcoholic', 'beer', NULL, 330, 5.2, 1.35, 'beer_mug', '["hefeweizen", "witbier", "weissbier", "hoegaarden", "bira white"]'::jsonb, 'approved', 0),
('Stout', 'stout', 'alcoholic', 'beer', NULL, 330, 4.5, 1.17, 'beer_mug', '["dry stout", "guinness", "irish stout"]'::jsonb, 'approved', 0),
('Imperial stout', 'imperial stout', 'alcoholic', 'beer', NULL, 330, 10.0, 2.6, 'beer_mug', '["russian imperial stout", "pastry stout"]'::jsonb, 'approved', 0),
('Porter', 'porter', 'alcoholic', 'beer', NULL, 330, 5.5, 1.43, 'beer_mug', NULL, 'approved', 0),
('Sour beer', 'sour beer', 'alcoholic', 'beer', NULL, 330, 4.5, 1.17, 'beer_mug', '["gose", "berliner weisse", "lambic", "fruited sour"]'::jsonb, 'approved', 0),
('Amber ale', 'amber ale', 'alcoholic', 'beer', NULL, 330, 5.5, 1.43, 'beer_mug', '["red ale", "brown ale", "irish red ale"]'::jsonb, 'approved', 0),
('Belgian ale', 'belgian ale', 'alcoholic', 'beer', NULL, 330, 9.0, 2.34, 'beer_mug', '["tripel", "dubbel", "quadrupel", "belgian strong ale"]'::jsonb, 'approved', 0),
('Radler', 'radler', 'alcoholic', 'beer', NULL, 330, 2.5, 0.65, 'beer_mug', '["shandy"]'::jsonb, 'approved', 0),
('Beer (other)', 'beer other', 'alcoholic', 'beer', NULL, 330, 5.0, 1.3, 'beer_mug', '["craft beer", "draft beer", "draught beer"]'::jsonb, 'approved', 0),
('Hard cider', 'hard cider', 'alcoholic', 'cider', NULL, 330, 4.5, 1.17, 'cider_glass', '["cider", "apple cider alcoholic", "perry", "pear cider", "somersby"]'::jsonb, 'approved', 0),
('Hard seltzer', 'hard seltzer', 'alcoholic', 'rtd', NULL, 355, 5.0, 1.4, 'seltzer_can', '["white claw", "truly", "spiked seltzer"]'::jsonb, 'approved', 0),
('RTD cooler / alcopop', 'rtd cooler alcopop', 'alcoholic', 'rtd', NULL, 275, 4.8, 1.04, 'seltzer_can', '["breezer", "bacardi breezer", "smirnoff ice", "hard lemonade", "wine cooler", "vodka cruiser"]'::jsonb, 'approved', 0),
('Hard kombucha', 'hard kombucha', 'alcoholic', 'rtd', 10, 355, 5.0, 1.4, 'kombucha_bottle', NULL, 'approved', 0),
('Red wine', 'red wine', 'alcoholic', 'wine', NULL, 150, 13.5, 1.6, 'wine_glass_red', '["cabernet sauvignon", "merlot", "shiraz", "syrah", "pinot noir", "malbec", "zinfandel"]'::jsonb, 'approved', 0),
('White wine', 'white wine', 'alcoholic', 'wine', NULL, 150, 12.5, 1.48, 'wine_glass_white', '["chardonnay", "sauvignon blanc", "riesling", "pinot grigio", "chenin blanc"]'::jsonb, 'approved', 0),
('Rose wine', 'rose wine', 'alcoholic', 'wine', NULL, 150, 12.5, 1.48, 'wine_glass_white', '["blush wine", "white zinfandel"]'::jsonb, 'approved', 0),
('Sparkling wine', 'sparkling wine', 'alcoholic', 'wine', NULL, 125, 12.0, 1.18, 'champagne_flute', '["champagne", "prosecco", "cava", "asti", "sekt", "sparkling brut"]'::jsonb, 'approved', 0),
('Dessert wine', 'dessert wine', 'alcoholic', 'wine', NULL, 90, 12.0, 0.85, 'wine_glass_white', '["late harvest wine", "ice wine", "moscato", "sauternes"]'::jsonb, 'approved', 0),
('Port', 'port', 'alcoholic', 'wine', NULL, 90, 20.0, 1.42, 'fortified_glass', '["tawny port", "ruby port", "porto"]'::jsonb, 'approved', 0),
('Sherry', 'sherry', 'alcoholic', 'wine', NULL, 90, 17.5, 1.24, 'fortified_glass', '["fino", "oloroso", "amontillado", "madeira", "marsala"]'::jsonb, 'approved', 0),
('Vermouth', 'vermouth', 'alcoholic', 'wine', NULL, 90, 16.5, 1.17, 'fortified_glass', '["sweet vermouth", "dry vermouth", "martini rosso"]'::jsonb, 'approved', 0),
('Sangria', 'sangria', 'alcoholic', 'wine', NULL, 200, 9.0, 1.42, 'wine_glass_red', '["red sangria", "white sangria"]'::jsonb, 'approved', 0),
('Mulled wine', 'mulled wine', 'alcoholic', 'wine', NULL, 180, 11.0, 1.56, 'wine_glass_red', '["gluhwein", "glogg", "vin chaud"]'::jsonb, 'approved', 0),
('Sake', 'sake', 'alcoholic', 'wine', NULL, 90, 15.0, 1.07, 'sake_cup', '["rice wine", "nihonshu", "junmai"]'::jsonb, 'approved', 0),
('Mead', 'mead', 'alcoholic', 'wine', NULL, 150, 11.0, 1.3, 'wine_glass_white', '["honey wine"]'::jsonb, 'approved', 0),
('Palm toddy', 'palm toddy', 'alcoholic', 'wine', NULL, 250, 5.0, 0.99, 'cider_glass', '["toddy", "kallu", "tadi", "palm wine"]'::jsonb, 'approved', 0),
('Wine (other)', 'wine other', 'alcoholic', 'wine', NULL, 150, 13.0, 1.54, 'wine_glass_red', '["house wine", "table wine"]'::jsonb, 'approved', 0),
('Vodka', 'vodka', 'alcoholic', 'spirits', NULL, 30, 40.0, 0.95, 'shot_glass', '["plain vodka", "flavored vodka", "magic moments"]'::jsonb, 'approved', 0),
('Gin', 'gin', 'alcoholic', 'spirits', NULL, 30, 40.0, 0.95, 'shot_glass', '["london dry gin", "bombay sapphire", "tanqueray", "craft gin"]'::jsonb, 'approved', 0),
('White rum', 'white rum', 'alcoholic', 'spirits', NULL, 30, 40.0, 0.95, 'shot_glass', '["light rum", "bacardi"]'::jsonb, 'approved', 0),
('Dark rum', 'dark rum', 'alcoholic', 'spirits', NULL, 30, 40.0, 0.95, 'shot_glass', '["old monk", "aged rum", "black rum"]'::jsonb, 'approved', 0),
('Spiced rum', 'spiced rum', 'alcoholic', 'spirits', NULL, 30, 35.0, 0.83, 'shot_glass', '["captain morgan"]'::jsonb, 'approved', 0),
('Whisky', 'whisky', 'alcoholic', 'spirits', NULL, 30, 40.0, 0.95, 'whiskey_glass', '["whiskey", "scotch", "single malt", "blended whisky", "japanese whisky", "irish whiskey", "jameson", "royal stag", "mcdowells no 1", "blenders pride"]'::jsonb, 'approved', 0),
('Bourbon', 'bourbon', 'alcoholic', 'spirits', NULL, 30, 45.0, 1.07, 'whiskey_glass', '["kentucky bourbon", "tennessee whiskey", "jack daniels", "rye whiskey"]'::jsonb, 'approved', 0),
('Tequila', 'tequila', 'alcoholic', 'spirits', NULL, 30, 40.0, 0.95, 'shot_glass', '["tequila blanco", "reposado", "anejo", "tequila shot"]'::jsonb, 'approved', 0),
('Mezcal', 'mezcal', 'alcoholic', 'spirits', NULL, 30, 42.0, 0.99, 'shot_glass', NULL, 'approved', 0),
('Brandy', 'brandy', 'alcoholic', 'spirits', NULL, 30, 40.0, 0.95, 'whiskey_glass', '["cognac", "armagnac", "vsop", "xo", "calvados"]'::jsonb, 'approved', 0),
('Absinthe', 'absinthe', 'alcoholic', 'spirits', NULL, 30, 60.0, 1.42, 'shot_glass', '["green fairy"]'::jsonb, 'approved', 0),
('Cachaca', 'cachaca', 'alcoholic', 'spirits', NULL, 30, 40.0, 0.95, 'shot_glass', NULL, 'approved', 0),
('Pisco', 'pisco', 'alcoholic', 'spirits', NULL, 30, 40.0, 0.95, 'shot_glass', NULL, 'approved', 0),
('Soju', 'soju', 'alcoholic', 'spirits', NULL, 50, 17.0, 0.67, 'shot_glass', '["jinro", "chamisul"]'::jsonb, 'approved', 0),
('Shochu', 'shochu', 'alcoholic', 'spirits', NULL, 60, 25.0, 1.18, 'shot_glass', NULL, 'approved', 0),
('Baijiu', 'baijiu', 'alcoholic', 'spirits', NULL, 30, 52.0, 1.23, 'shot_glass', '["moutai", "maotai"]'::jsonb, 'approved', 0),
('Grappa', 'grappa', 'alcoholic', 'spirits', NULL, 30, 42.0, 0.99, 'shot_glass', NULL, 'approved', 0),
('Ouzo', 'ouzo', 'alcoholic', 'spirits', NULL, 30, 40.0, 0.95, 'shot_glass', '["raki", "arak", "pastis"]'::jsonb, 'approved', 0),
('Aquavit', 'aquavit', 'alcoholic', 'spirits', NULL, 30, 40.0, 0.95, 'shot_glass', '["akvavit"]'::jsonb, 'approved', 0),
('Schnapps', 'schnapps', 'alcoholic', 'spirits', NULL, 30, 40.0, 0.95, 'shot_glass', '["fruit brandy", "obstler", "palinka", "rakia", "slivovitz"]'::jsonb, 'approved', 0),
('Moonshine', 'moonshine', 'alcoholic', 'spirits', NULL, 30, 50.0, 1.18, 'shot_glass', '["white lightning", "hooch"]'::jsonb, 'approved', 0),
('Feni', 'feni', 'alcoholic', 'spirits', NULL, 30, 43.0, 1.02, 'shot_glass', '["cashew feni", "coconut feni", "goan feni"]'::jsonb, 'approved', 0),
('Arrack', 'arrack', 'alcoholic', 'spirits', NULL, 30, 40.0, 0.95, 'shot_glass', '["coconut arrack", "ceylon arrack"]'::jsonb, 'approved', 0),
('Country liquor', 'country liquor', 'alcoholic', 'spirits', NULL, 30, 30.0, 0.71, 'shot_glass', '["desi daru", "desi sharab", "santra", "tharra", "mahua"]'::jsonb, 'approved', 0),
('Spirit (other)', 'spirit other', 'alcoholic', 'spirits', NULL, 30, 40.0, 0.95, 'shot_glass', '["hard liquor", "neat spirit"]'::jsonb, 'approved', 0),
('Irish cream', 'irish cream', 'alcoholic', 'liqueur', NULL, 30, 17.0, 0.4, 'liqueur_glass', '["baileys"]'::jsonb, 'approved', 0),
('Coffee liqueur', 'coffee liqueur', 'alcoholic', 'liqueur', 5, 30, 20.0, 0.47, 'liqueur_glass', '["kahlua", "tia maria"]'::jsonb, 'approved', 0),
('Amaretto', 'amaretto', 'alcoholic', 'liqueur', NULL, 30, 28.0, 0.66, 'liqueur_glass', '["disaronno", "almond liqueur"]'::jsonb, 'approved', 0),
('Orange liqueur', 'orange liqueur', 'alcoholic', 'liqueur', NULL, 30, 40.0, 0.95, 'liqueur_glass', '["triple sec", "cointreau", "grand marnier", "curacao"]'::jsonb, 'approved', 0),
('Campari', 'campari', 'alcoholic', 'liqueur', NULL, 30, 25.0, 0.59, 'liqueur_glass', '["italian bitter"]'::jsonb, 'approved', 0),
('Aperol', 'aperol', 'alcoholic', 'liqueur', NULL, 30, 11.0, 0.26, 'liqueur_glass', NULL, 'approved', 0),
('Jagermeister', 'jagermeister', 'alcoholic', 'liqueur', NULL, 30, 35.0, 0.83, 'liqueur_glass', '["jager", "jaeger", "herbal liqueur"]'::jsonb, 'approved', 0),
('Limoncello', 'limoncello', 'alcoholic', 'liqueur', NULL, 30, 28.0, 0.66, 'liqueur_glass', '["lemon liqueur"]'::jsonb, 'approved', 0),
('Sambuca', 'sambuca', 'alcoholic', 'liqueur', NULL, 30, 38.0, 0.9, 'liqueur_glass', '["anise liqueur"]'::jsonb, 'approved', 0),
('Chartreuse', 'chartreuse', 'alcoholic', 'liqueur', NULL, 30, 55.0, 1.3, 'liqueur_glass', '["green chartreuse", "yellow chartreuse"]'::jsonb, 'approved', 0),
('Drambuie', 'drambuie', 'alcoholic', 'liqueur', NULL, 30, 40.0, 0.95, 'liqueur_glass', NULL, 'approved', 0),
('Elderflower liqueur', 'elderflower liqueur', 'alcoholic', 'liqueur', NULL, 30, 20.0, 0.47, 'liqueur_glass', '["st germain"]'::jsonb, 'approved', 0),
('Amaro', 'amaro', 'alcoholic', 'liqueur', NULL, 30, 30.0, 0.71, 'liqueur_glass', '["fernet", "fernet branca", "montenegro", "averna", "digestif"]'::jsonb, 'approved', 0),
('Liqueur (other)', 'liqueur other', 'alcoholic', 'liqueur', NULL, 30, 25.0, 0.59, 'liqueur_glass', '["cream liqueur", "fruit liqueur"]'::jsonb, 'approved', 0),
('Margarita', 'margarita', 'alcoholic', 'cocktail', NULL, 150, 17.0, 2.01, 'cocktail_glass', '["frozen margarita", "spicy margarita"]'::jsonb, 'approved', 0),
('Mojito', 'mojito', 'alcoholic', 'cocktail', NULL, 240, 10.0, 1.89, 'cocktail_glass', NULL, 'approved', 0),
('Martini', 'martini', 'alcoholic', 'cocktail', NULL, 90, 30.0, 2.13, 'cocktail_glass', '["dry martini", "dirty martini", "vodka martini", "gibson"]'::jsonb, 'approved', 0),
('Old fashioned', 'old fashioned', 'alcoholic', 'cocktail', NULL, 90, 32.0, 2.27, 'cocktail_glass', NULL, 'approved', 0),
('Negroni', 'negroni', 'alcoholic', 'cocktail', NULL, 90, 24.0, 1.7, 'cocktail_glass', '["boulevardier"]'::jsonb, 'approved', 0),
('Manhattan', 'manhattan', 'alcoholic', 'cocktail', NULL, 90, 30.0, 2.13, 'cocktail_glass', '["rob roy"]'::jsonb, 'approved', 0),
('Whiskey sour', 'whiskey sour', 'alcoholic', 'cocktail', NULL, 120, 18.0, 1.7, 'cocktail_glass', '["whisky sour", "amaretto sour", "pisco sour"]'::jsonb, 'approved', 0),
('Daiquiri', 'daiquiri', 'alcoholic', 'cocktail', NULL, 100, 20.0, 1.58, 'cocktail_glass', '["strawberry daiquiri", "frozen daiquiri"]'::jsonb, 'approved', 0),
('Pina colada', 'pina colada', 'alcoholic', 'cocktail', NULL, 240, 10.0, 1.89, 'cocktail_glass', NULL, 'approved', 0),
('Mai tai', 'mai tai', 'alcoholic', 'cocktail', NULL, 150, 17.0, 2.01, 'cocktail_glass', NULL, 'approved', 0),
('Cosmopolitan', 'cosmopolitan', 'alcoholic', 'cocktail', NULL, 120, 20.0, 1.89, 'cocktail_glass', '["cosmo"]'::jsonb, 'approved', 0),
('Gin and tonic', 'gin and tonic', 'alcoholic', 'cocktail', NULL, 240, 8.0, 1.51, 'cocktail_glass', '["g and t", "gin tonic"]'::jsonb, 'approved', 0),
('Vodka soda', 'vodka soda', 'alcoholic', 'cocktail', NULL, 240, 8.0, 1.51, 'cocktail_glass', '["vodka tonic", "vodka lime soda"]'::jsonb, 'approved', 0),
('Rum and coke', 'rum and coke', 'alcoholic', 'cocktail', 10, 240, 8.0, 1.51, 'cocktail_glass', '["cuba libre", "rum coke", "whisky cola"]'::jsonb, 'approved', 0),
('Whisky highball', 'whisky highball', 'alcoholic', 'cocktail', NULL, 240, 8.0, 1.51, 'cocktail_glass', '["highball", "whisky soda", "scotch and soda", "whisky water"]'::jsonb, 'approved', 0),
('Moscow mule', 'moscow mule', 'alcoholic', 'cocktail', NULL, 240, 9.0, 1.7, 'cocktail_glass', '["vodka mule", "ginger mule"]'::jsonb, 'approved', 0),
('Dark and stormy', 'dark and stormy', 'alcoholic', 'cocktail', NULL, 240, 9.0, 1.7, 'cocktail_glass', '["dark n stormy"]'::jsonb, 'approved', 0),
('Bloody Mary', 'bloody mary', 'alcoholic', 'cocktail', NULL, 240, 8.0, 1.51, 'cocktail_glass', '["bloody maria", "red snapper"]'::jsonb, 'approved', 0),
('Screwdriver', 'screwdriver', 'alcoholic', 'cocktail', NULL, 240, 10.0, 1.89, 'cocktail_glass', '["vodka orange juice", "vodka oj"]'::jsonb, 'approved', 0),
('Tequila sunrise', 'tequila sunrise', 'alcoholic', 'cocktail', NULL, 200, 10.0, 1.58, 'cocktail_glass', NULL, 'approved', 0),
('Sex on the beach', 'sex on the beach', 'alcoholic', 'cocktail', NULL, 200, 11.0, 1.74, 'cocktail_glass', NULL, 'approved', 0),
('Paloma', 'paloma', 'alcoholic', 'cocktail', NULL, 240, 8.0, 1.51, 'cocktail_glass', NULL, 'approved', 0),
('Caipirinha', 'caipirinha', 'alcoholic', 'cocktail', NULL, 120, 22.0, 2.08, 'cocktail_glass', NULL, 'approved', 0),
('Michelada', 'michelada', 'alcoholic', 'cocktail', NULL, 355, 4.0, 1.12, 'cocktail_glass', '["chelada", "beer cocktail"]'::jsonb, 'approved', 0),
('Mint julep', 'mint julep', 'alcoholic', 'cocktail', NULL, 120, 25.0, 2.37, 'cocktail_glass', NULL, 'approved', 0),
('Sidecar', 'sidecar', 'alcoholic', 'cocktail', NULL, 100, 25.0, 1.97, 'cocktail_glass', NULL, 'approved', 0),
('Gimlet', 'gimlet', 'alcoholic', 'cocktail', NULL, 90, 25.0, 1.78, 'cocktail_glass', '["gin gimlet", "vodka gimlet"]'::jsonb, 'approved', 0),
('French 75', 'french 75', 'alcoholic', 'cocktail', NULL, 150, 15.0, 1.78, 'cocktail_glass', NULL, 'approved', 0),
('Tom Collins', 'tom collins', 'alcoholic', 'cocktail', NULL, 240, 9.0, 1.7, 'cocktail_glass', '["john collins"]'::jsonb, 'approved', 0),
('Hurricane', 'hurricane', 'alcoholic', 'cocktail', NULL, 240, 14.0, 2.65, 'cocktail_glass', NULL, 'approved', 0),
('Mimosa', 'mimosa', 'alcoholic', 'cocktail', NULL, 150, 6.0, 0.71, 'cocktail_glass', '["bellini", "kir royale"]'::jsonb, 'approved', 0),
('Aperol spritz', 'aperol spritz', 'alcoholic', 'cocktail', NULL, 200, 8.0, 1.26, 'cocktail_glass', '["spritz", "hugo spritz"]'::jsonb, 'approved', 0),
('Long Island iced tea', 'long island iced tea', 'alcoholic', 'cocktail', NULL, 240, 18.0, 3.41, 'cocktail_glass', '["liit", "long island"]'::jsonb, 'approved', 0),
('Espresso martini', 'espresso martini', 'alcoholic', 'cocktail', 65, 120, 15.0, 1.42, 'cocktail_glass', NULL, 'approved', 0),
('White Russian', 'white russian', 'alcoholic', 'cocktail', 5, 120, 15.0, 1.42, 'cocktail_glass', NULL, 'approved', 0),
('Black Russian', 'black russian', 'alcoholic', 'cocktail', 7, 90, 23.0, 1.63, 'cocktail_glass', NULL, 'approved', 0),
('Irish coffee', 'irish coffee', 'alcoholic', 'cocktail', 90, 240, 7.5, 1.42, 'cocktail_glass', '["gaelic coffee"]'::jsonb, 'approved', 0),
('Hot toddy', 'hot toddy', 'alcoholic', 'cocktail', NULL, 200, 10.0, 1.58, 'cocktail_glass', '["whisky toddy"]'::jsonb, 'approved', 0),
('Jagerbomb', 'jagerbomb', 'alcoholic', 'cocktail', 80, 300, 7.0, 1.66, 'cocktail_glass', '["jager bomb", "vodka red bull", "vodka energy"]'::jsonb, 'approved', 0),
('Cocktail (other)', 'cocktail other', 'alcoholic', 'cocktail', NULL, 150, 15.0, 1.78, 'cocktail_glass', '["mixed drink", "house cocktail"]'::jsonb, 'approved', 0)
ON CONFLICT (name_normalized, kind) DO NOTHING;

-- ============================================================
-- V18__thp_catalogue.sql
-- ============================================================
-- V18__thp_catalogue.sql
-- Imports the curated lab-parameter catalogue into the dashboard THP tables:
--   193 parameters, 1184 aliases, 277 age/sex ranges.
-- Idempotent: every insert is ON CONFLICT DO NOTHING; staff edits are never
-- overwritten. Aliases and ranges resolve their parameter by unique name, so
-- the ids stay environment-independent. Danger bounds are pinned to the graph
-- bounds (the app's warning-only model); ideal is the midpoint of the ideal
-- band. Source age groups are kept in source_note. Smoker-specific ranges are
-- folded into the baseline row's note (unique bounds per parameter+sex).

INSERT INTO public.traditional_health_parameters
  (name, description, units, category, value_type, allow_manual_entry, status, visible)
VALUES
('A/G ratio', NULL, 'Ratio', 'lab', 'float', false, 'approved', true),
('Absolute Basophil Count', NULL, '10³/mm³', 'lab', 'float', false, 'approved', true),
('Absolute Eosinophil Count', NULL, '10³/mm³', 'lab', 'float', false, 'approved', true),
('Absolute Lymphocyte Count', NULL, '10³/mm³', 'lab', 'float', false, 'approved', true),
('Absolute Monocyte Count', NULL, '10³/mm³', 'lab', 'float', false, 'approved', true),
('Absolute Neutrophil Count', NULL, '10³/mm³', 'lab', 'float', false, 'approved', true),
('AFP', NULL, 'ng/mL', 'lab', 'float', false, 'approved', true),
('Albumin', NULL, 'g/dL', 'lab', 'float', false, 'approved', true),
('Alkaline Phosphatase', NULL, 'U/L', 'lab', 'float', false, 'approved', true),
('ALT(SGPT)', NULL, 'U/L', 'lab', 'float', false, 'approved', true),
('AMH ( Anti - Mullerian Hormone )', NULL, 'ng/mL', 'lab', 'float', false, 'draft', true),
('Amylase', NULL, 'U/L', 'lab', 'float', false, 'approved', true),
('Anti-HBc (Total)', NULL, 'qualitative', 'lab', 'float', false, 'draft', true),
('Anti-HBs', NULL, 'mIU/mL', 'lab', 'float', false, 'draft', true),
('Anti-TG', NULL, 'IU/mL', 'lab', 'float', false, 'approved', true),
('Anti-TPO', NULL, 'IU/mL', 'lab', 'float', false, 'approved', true),
('ApoB/ApoA1 Ratio', NULL, 'ratio', 'lab', 'float', false, 'approved', true),
('Apolipoprotein A1', NULL, 'mg/dL', 'lab', 'float', false, 'approved', true),
('Apolipoprotein B', NULL, 'mg/dL', 'lab', 'float', false, 'approved', true),
('Appearance', NULL, 'No Unit (Descriptive)', 'lab', 'float', false, 'draft', true),
('APTT', NULL, 'seconds', 'lab', 'float', false, 'approved', true),
('AST(SGOT)', NULL, 'U/L', 'lab', 'float', false, 'approved', true),
('AST/ALT Ratio', NULL, 'ratio', 'lab', 'float', false, 'approved', true),
('Average Blood Glucose', NULL, 'mg/dL', 'lab', 'float', false, 'approved', true),
('Basophils', NULL, '%', 'lab', 'float', false, 'approved', true),
('Bicarbonate ( HCO3⁻)', NULL, 'mmol/L', 'lab', 'float', false, 'approved', true),
('Blood (Urine)', NULL, 'Qualitative', 'lab', 'float', false, 'draft', false),
('Blood Urea', NULL, 'mg/dL', 'lab', 'float', false, 'approved', true),
('BNP', NULL, 'pg/mL', 'lab', 'float', false, 'approved', true),
('BUN', NULL, 'mg/dL', 'lab', 'float', false, 'approved', true),
('BUN/Creatinine Ratio', NULL, 'ratio', 'lab', 'float', false, 'approved', true),
('C-Peptide', NULL, 'ng/mL', 'lab', 'float', false, 'approved', true),
('CA 15-3', NULL, 'U/mL', 'lab', 'float', false, 'approved', true),
('CA 19-9', NULL, 'U/mL', 'lab', 'float', false, 'approved', true),
('CA-125', NULL, 'U/mL', 'lab', 'float', false, 'approved', true),
('Calcium', NULL, 'mg/ dL', 'lab', 'float', false, 'approved', true),
('Casts', NULL, '/lpf', 'lab', 'float', false, 'draft', true),
('CEA', 'Ideal and warning high from old spread sheet
Graph max and min from 20% buffer', 'ng/mL', 'lab', 'float', false, 'approved', true),
('Chloride (Cl⁻)', NULL, 'mmol/L', 'lab', 'float', false, 'approved', true),
('CHOL/HDL ratio', NULL, 'Ratio', 'lab', 'float', false, 'approved', true),
('CK-MB', NULL, 'U/L', 'lab', 'float', false, 'approved', true),
('Color', NULL, 'No Unit (Descriptive)', 'lab', 'float', false, 'draft', true),
('Cortisol', NULL, 'µg/dL', 'lab', 'float', false, 'draft', true),
('CPK (Total CK)', NULL, 'U/L', 'lab', 'float', false, 'approved', true),
('CRP', NULL, 'mg/L', 'lab', 'float', false, 'approved', true),
('Crystals', NULL, '/lpf', 'lab', 'float', false, 'draft', true),
('Cystatin C', NULL, 'mg/L', 'lab', 'float', false, 'approved', true),
('Dengue NS1 Antigen', NULL, 'qualitative', 'lab', 'float', false, 'draft', true),
('DHEA-S', NULL, 'µg/dL', 'lab', 'float', false, 'approved', true),
('Direct-Bilirubin', NULL, 'mg/dL', 'lab', 'float', false, 'approved', true),
('Eosinophils', NULL, '%', 'lab', 'float', false, 'approved', true),
('Epithelial Cells', NULL, 'cells/hpf', 'lab', 'float', false, 'approved', true),
('ESR', NULL, 'mm/hr', 'lab', 'float', false, 'approved', true),
('Estimated Average Glucose (eAG)', NULL, 'mg/dL', 'lab', 'float', false, 'approved', true),
('Estimated Glomerular Filtration Rate (eGFR)', NULL, 'mL/min/1.73 m²', 'lab', 'float', false, 'approved', true),
('Estrogen', NULL, 'pg/mL', 'lab', 'float', false, 'draft', true),
('Fasting Blood Sugar', NULL, 'mg/dL', 'lab', 'float', false, 'approved', true),
('Follicle Stimulating Hormone (FSH)', NULL, 'mIU/mL', 'lab', 'float', false, 'draft', true),
('Free PSA', NULL, 'ng/mL', 'lab', 'float', false, 'draft', true),
('Free T3', NULL, 'pg/mL', 'lab', 'float', false, 'approved', true),
('Free T4', NULL, 'ng/dL', 'lab', 'float', false, 'approved', true),
('Free Testosterone', NULL, 'pg/mL', 'lab', 'float', false, 'draft', true),
('GGT', NULL, 'U/L', 'lab', 'float', false, 'approved', true),
('Globulin', NULL, 'g/dL', 'lab', 'float', false, 'approved', true),
('Glycated Hemoglobin (HbA1c)', NULL, '%', 'lab', 'float', false, 'approved', true),
('Growth Hormone', NULL, 'ng/mL', 'lab', 'float', false, 'approved', true),
('HBsAg', NULL, 'qualitative', 'lab', 'float', false, 'draft', true),
('HCV Antibody', NULL, 'qualitative', 'lab', 'float', false, 'draft', true),
('HDL Cholesterol', NULL, 'mg/dL', 'lab', 'float', false, 'approved', true),
('HDL/LDL Ratio', NULL, 'ratio', 'lab', 'float', false, 'draft', true),
('Hematocrit', NULL, '%', 'lab', 'float', false, 'approved', true),
('Hemoglobin', NULL, 'g/dL', 'lab', 'float', false, 'approved', true),
('HIV 1&2 Antibodies', NULL, 'qualitative', 'lab', 'float', false, 'draft', true),
('HOMA-IR', NULL, 'index', 'lab', 'float', false, 'approved', true),
('Homocysteine', NULL, 'µmol/L', 'lab', 'float', false, 'approved', true),
('hs-CRP', NULL, 'mg/L', 'lab', 'float', false, 'draft', true),
('hs-Troponin I', NULL, 'ng/L', 'lab', 'float', false, 'approved', true),
('hs-Troponin T', NULL, 'ng/L', 'lab', 'float', false, 'approved', true),
('IGF-1', NULL, 'ng/mL', 'lab', 'float', false, 'approved', true),
('Immature Granulocytes %', NULL, '%', 'lab', 'float', false, 'approved', true),
('Indirect-Bilirubin', NULL, 'mg/dL', 'lab', 'float', false, 'approved', true),
('INR', NULL, 'ratio', 'lab', 'float', false, 'approved', true),
('Insulin', NULL, 'µIU/mL', 'lab', 'float', false, 'approved', true),
('LDH', NULL, 'U/L', 'lab', 'float', false, 'approved', true),
('LDL Cholesterol', NULL, 'mg/dL', 'lab', 'float', false, 'approved', true),
('LDL/HDL ratio', NULL, 'Ratio', 'lab', 'float', false, 'approved', true),
('Lipase', NULL, 'U/L', 'lab', 'float', false, 'approved', true),
('Lipoprotein(a)', NULL, 'mg/dL', 'lab', 'float', false, 'approved', true),
('Luteinizing Hormone (LH)', NULL, 'mIU/mL', 'lab', 'float', false, 'draft', true),
('Lymphocytes', NULL, '%', 'lab', 'float', false, 'approved', true),
('Magnesium', NULL, 'mg/dL', 'lab', 'float', false, 'approved', true),
('Malaria Antigen', NULL, 'qualitative', 'lab', 'float', false, 'draft', true),
('MCH', NULL, 'pg', 'lab', 'float', false, 'approved', true),
('MCHC', NULL, 'g/dL', 'lab', 'float', false, 'approved', true),
('MCV', NULL, 'fL', 'lab', 'float', false, 'approved', true),
('Mentzer Index', NULL, 'index', 'lab', 'float', false, 'draft', true),
('Microalbumin (Urine)', NULL, 'mg/g creatinine', 'lab', 'float', false, 'draft', true),
('Monocytes', NULL, '%', 'lab', 'float', false, 'approved', true),
('MPV', NULL, 'fL', 'lab', 'float', false, 'approved', true),
('Neutrophils', NULL, '%', 'lab', 'float', false, 'approved', true),
('Nitrite', NULL, 'Qualitative', 'lab', 'float', false, 'draft', true),
('Non HDL Cholesterol', NULL, 'mg/dL', 'lab', 'float', false, 'approved', true),
('NT-proBNP', NULL, 'pg/mL', 'lab', 'float', false, 'approved', true),
('PDW', NULL, 'fL', 'lab', 'float', false, 'approved', true),
('Phosphorus', NULL, 'mg/dL', 'lab', 'float', false, 'approved', true),
('Platelet Count', NULL, '10³/mm³', 'lab', 'float', false, 'approved', true),
('Plateletcrit', NULL, '%', 'lab', 'float', false, 'approved', true),
('Postprandial Blood Sugar', NULL, 'mg/dL', 'lab', 'float', true, 'approved', true),
('Potassium (K⁺)', NULL, 'mmol/L', 'lab', 'float', false, 'approved', true),
('Progesterone', NULL, 'ng/mL', 'lab', 'float', false, 'draft', true),
('Prolactin', NULL, 'ng/mL', 'lab', 'float', false, 'approved', true),
('Prothrombin Time (PT)', NULL, 'seconds', 'lab', 'float', false, 'approved', true),
('PSA (Total)', NULL, 'ng/mL', 'lab', 'float', false, 'draft', true),
('Pus Cells', NULL, 'cells/hpf', 'lab', 'float', false, 'approved', true),
('Random Blood Glucose', NULL, 'mg/dL', 'lab', 'float', true, 'approved', true),
('RBC', NULL, 'mill/mm³', 'lab', 'float', false, 'approved', true),
('RBC Urine', NULL, 'cells/hpf', 'lab', 'float', false, 'draft', true),
('RDW', NULL, '%', 'lab', 'float', false, 'approved', true),
('RDW-CV', NULL, '%', 'lab', 'float', false, 'approved', false),
('RDW-SD', NULL, 'fL', 'lab', 'float', false, 'approved', false),
('RDWI', NULL, 'index', 'lab', 'float', false, 'draft', true),
('Serum Aluminium', NULL, 'µg/L', 'lab', 'float', false, 'approved', true),
('Serum Antimony', NULL, 'µg/L', 'lab', 'float', false, 'approved', true),
('Serum Arsenic', NULL, 'µg/L', 'lab', 'float', false, 'approved', true),
('Serum Barium', NULL, 'µg/L', 'lab', 'float', false, 'approved', true),
('Serum Beryllium', NULL, 'µg/L', 'lab', 'float', false, 'draft', true),
('Serum Bismuth', NULL, 'µg/L', 'lab', 'float', false, 'approved', true),
('Serum Cadmium', NULL, 'µg/L', 'lab', 'float', false, 'approved', true),
('Serum Caesium', NULL, 'µg/L', 'lab', 'float', false, 'approved', true),
('Serum Chromium', NULL, 'µg/L', 'lab', 'float', false, 'approved', true),
('Serum Cobalt', NULL, 'µg/L', 'lab', 'float', false, 'approved', true),
('Serum Copper', NULL, 'µg/dL', 'lab', 'float', false, 'approved', true),
('Serum Creatinine', NULL, 'mg/dL', 'lab', 'float', false, 'approved', true),
('Serum Ferritin', NULL, 'ng/mL', 'lab', 'float', false, 'approved', true),
('Serum Iron', NULL, 'µg/dL', 'lab', 'float', false, 'approved', true),
('Serum Lead', NULL, 'µg/L', 'lab', 'float', false, 'approved', true),
('Serum Manganese', NULL, 'µg/L', 'lab', 'float', false, 'approved', true),
('Serum Mercury', NULL, 'µg/L', 'lab', 'float', false, 'approved', true),
('Serum Molybdenum', NULL, 'µg/L', 'lab', 'float', false, 'approved', true),
('Serum Nickel', NULL, 'µg/L', 'lab', 'float', false, 'approved', true),
('Serum Selenium', NULL, 'µg/L', 'lab', 'float', false, 'approved', true),
('Serum Silver', NULL, 'µg/L', 'lab', 'float', false, 'approved', true),
('Serum Strontium', NULL, 'µg/L', 'lab', 'float', false, 'approved', true),
('Serum Thallium', NULL, 'µg/L', 'lab', 'float', false, 'approved', true),
('Serum Tin', NULL, 'µg/L', 'lab', 'float', false, 'approved', true),
('Serum Uranium', NULL, 'µg/L', 'lab', 'float', false, 'approved', true),
('Serum Uric Acid', NULL, 'mg/dL', 'lab', 'float', false, 'approved', true),
('Serum Vanadium', NULL, 'µg/L', 'lab', 'float', false, 'approved', true),
('Serum Zinc', NULL, 'µg/dL', 'lab', 'float', false, 'approved', true),
('Sodium ( Na+)', NULL, 'mmol/L', 'lab', 'float', false, 'approved', true),
('Testosterone', NULL, 'ng/dL', 'lab', 'float', false, 'draft', true),
('Thyroid Stimulating Hormone (TSH)', NULL, 'µIU/mL', 'lab', 'float', false, 'approved', true),
('TIBC (Total Iron Binding Capacity)', NULL, 'µg/dL', 'lab', 'float', false, 'approved', true),
('Total Bilirubin', NULL, 'mg/dL', 'lab', 'float', false, 'approved', true),
('Total Cholesterol', NULL, 'mg/dL', 'lab', 'float', false, 'approved', true),
('Total Protein', NULL, 'g/dL', 'lab', 'float', false, 'approved', true),
('Total Thyroxine (T4)', NULL, 'µg/dL', 'lab', 'float', false, 'approved', true),
('Total Triiodothyronine (T3)', NULL, 'ng/dL', 'lab', 'float', false, 'approved', true),
('TPHA', NULL, 'qualitative', 'lab', 'float', false, 'draft', true),
('Transferrin Saturation (%)', NULL, '%', 'lab', 'float', false, 'approved', true),
('Transferrin Serum', NULL, 'mg/dL', 'lab', 'float', false, 'approved', true),
('Trig/HDL Ratio', NULL, 'ratio', 'lab', 'float', false, 'approved', true),
('Triglycerides', NULL, 'mg/dL', 'lab', 'float', false, 'approved', true),
('Troponin I', NULL, 'ng/mL', 'lab', 'float', false, 'draft', true),
('Troponin T', NULL, 'ng/mL', 'lab', 'float', false, 'draft', true),
('UIBC (Unsaturated Iron Binding Capacity)', NULL, 'IU/mL', 'lab', 'float', false, 'approved', true),
('Urine Bilirubin', NULL, 'Qualitative', 'lab', 'float', false, 'draft', true),
('Urine Glucose', NULL, 'Qualitative', 'lab', 'float', false, 'draft', true),
('Urine Ketones', NULL, 'Qualitative', 'lab', 'float', false, 'draft', true),
('Urine Leukocyte Esterase', NULL, 'qualitative', 'lab', 'float', false, 'draft', true),
('Urine Odor', NULL, '', 'lab', 'float', false, 'draft', true),
('Urine pH', NULL, 'pH', 'lab', 'float', false, 'approved', true),
('Urine Protein', NULL, 'Qualitative', 'lab', 'float', false, 'draft', true),
('Urine Specific Gravity', NULL, 'ratio', 'lab', 'float', false, 'approved', true),
('Urine Volume', NULL, 'mL', 'lab', 'float', false, 'draft', true),
('Urobilinogen', NULL, 'EU/dL', 'lab', 'float', false, 'pending', true),
('VDRL', NULL, 'qualitative', 'lab', 'float', false, 'draft', true),
('Vitamin A', NULL, 'µg/dL', 'lab', 'float', false, 'approved', true),
('Vitamin B1 (Thiamin)', NULL, 'ng/mL', 'lab', 'float', false, 'approved', true),
('Vitamin B12', NULL, 'pg/mL', 'lab', 'float', false, 'approved', true),
('Vitamin B2 (Riboflavin)', NULL, 'ng/mL', 'lab', 'float', false, 'approved', true),
('Vitamin B3 (Niacin)', NULL, 'ng/mL', 'lab', 'float', false, 'approved', true),
('Vitamin B5 (Pantothenic)', NULL, 'ng/mL', 'lab', 'float', false, 'approved', true),
('Vitamin B6 (P5P)', NULL, 'ng/mL', 'lab', 'float', false, 'approved', true),
('Vitamin B7 (Biotin)', NULL, 'ng/mL', 'lab', 'float', false, 'approved', true),
('Vitamin B9', NULL, 'ng/mL', 'lab', 'float', false, 'approved', true),
('Vitamin D', NULL, 'ng/mL', 'lab', 'float', false, 'approved', true),
('Vitamin D2', NULL, 'ng/mL', 'lab', 'float', false, 'draft', true),
('Vitamin D3', NULL, 'ng/mL', 'lab', 'float', false, 'draft', true),
('Vitamin E', NULL, 'ng/mL', 'lab', 'float', false, 'approved', true),
('Vitamin K', NULL, 'ng/mL', 'lab', 'float', false, 'approved', true),
('VLDL Cholesterol', NULL, 'mg/dL', 'lab', 'float', false, 'approved', true),
('WBC', NULL, '10³/mm³', 'lab', 'float', false, 'approved', true)
ON CONFLICT (name) DO NOTHING;

INSERT INTO public.thp_alias (thp_id, alias, source, status)
SELECT t.id, v.alias, 'migrated', 'approved'
FROM (VALUES
('A/G ratio', 'Albumin Globulin Ratio'),
('A/G ratio', 'AG Ratio'),
('A/G ratio', 'albumin/globulin ratio'),
('Absolute Basophil Count', 'ABC'),
('Absolute Basophil Count', 'Absolute Basophils'),
('Absolute Basophil Count', 'Absolute Basophil Count (ABC)'),
('Absolute Basophil Count', 'Absolute Basophil'),
('Absolute Basophil Count', 'Absolute Basophils Count (ABC)'),
('Absolute Basophil Count', 'abs basophils'),
('Absolute Basophil Count', 'basophils abs'),
('Absolute Basophil Count', 'basophils abs.'),
('Absolute Basophil Count', 'basophils absolute count'),
('Absolute Eosinophil Count', 'AEC'),
('Absolute Eosinophil Count', 'Eosinophil Absolute Count'),
('Absolute Eosinophil Count', 'Eosinophils Absolute'),
('Absolute Eosinophil Count', 'Absolute Eosinophil'),
('Absolute Eosinophil Count', 'Absolute Eosinophils'),
('Absolute Eosinophil Count', 'Absolute Eosinophil Count (AEC)'),
('Absolute Eosinophil Count', 'Eosinophil absolute'),
('Absolute Eosinophil Count', 'Absolute Eosinophils Count'),
('Absolute Eosinophil Count', 'Absolute Eosinophils Count (AEC)'),
('Absolute Eosinophil Count', 'abs eosinophils'),
('Absolute Eosinophil Count', 'eosinophils abs'),
('Absolute Eosinophil Count', 'eosinophils abs.'),
('Absolute Eosinophil Count', 'eosinophils absolute count'),
('Absolute Lymphocyte Count', 'ALC'),
('Absolute Lymphocyte Count', 'Absolute Lymphocytes Count'),
('Absolute Lymphocyte Count', 'abs lymphocytes'),
('Absolute Lymphocyte Count', 'lymphocytes abs'),
('Absolute Lymphocyte Count', 'lymphocytes abs.'),
('Absolute Lymphocyte Count', 'lymphocytes absolute count'),
('Absolute Monocyte Count', 'AMC'),
('Absolute Monocyte Count', 'Absolute Monocytes'),
('Absolute Monocyte Count', 'Absolute Monocyte Count (AMC)'),
('Absolute Monocyte Count', 'Absolute Monocyte'),
('Absolute Monocyte Count', 'Absolute Monocytes Count'),
('Absolute Monocyte Count', 'Absolute Monocytes Count (AMC)'),
('Absolute Monocyte Count', 'abs monocytes'),
('Absolute Monocyte Count', 'monocytes abs'),
('Absolute Monocyte Count', 'monocytes abs.'),
('Absolute Monocyte Count', 'monocytes absolute count'),
('Absolute Monocyte Count', 'monocytes - absolute count'),
('Absolute Neutrophil Count', 'ANC'),
('Absolute Neutrophil Count', 'Absolute Neutrophils'),
('Absolute Neutrophil Count', 'Absolute Neutrophil Count (ANC)'),
('Absolute Neutrophil Count', 'ANC Calculated'),
('Absolute Neutrophil Count', 'Absolute Neutrophil'),
('Absolute Neutrophil Count', 'Absolutes Neutrophils Count'),
('Absolute Neutrophil Count', 'Absolute Neutrophils Count (ANC)'),
('Absolute Neutrophil Count', 'Absolute Neutrophils Count Calculated'),
('Absolute Neutrophil Count', 'Absolute Neutrophil Count Calculated'),
('Absolute Neutrophil Count', 'abs neutrophils'),
('Absolute Neutrophil Count', 'neutrophils abs'),
('Absolute Neutrophil Count', 'neutrophils abs.'),
('Absolute Neutrophil Count', 'neutrophils absolute count'),
('AFP', 'alpha fetoprotein'),
('AFP', 'alpha-fetoprotein'),
('Albumin', 'Serum Albumin'),
('Albumin', 'ALBUMIN - SERUM'),
('Alkaline Phosphatase', 'ALP'),
('Alkaline Phosphatase', 'ALKP'),
('Alkaline Phosphatase', 'Alkaline Phosphatase (ALKP)'),
('Alkaline Phosphatase', 'alkaline phosphatase (alp)'),
('ALT(SGPT)', 'Alanine Aminotransferase'),
('ALT(SGPT)', 'SGPT'),
('ALT(SGPT)', 'ALT'),
('ALT(SGPT)', 'Alanine Aminotransferase (Serum Glutamic Pyruvic Transaminase)'),
('ALT(SGPT)', 'ALANINE TRANSAMINASE (SGPT)'),
('ALT(SGPT)', 'alt (sgpt)'),
('ALT(SGPT)', 'alanine transaminase'),
('ALT(SGPT)', 'alanine aminotransferase (alt/sgpt)'),
('AMH ( Anti - Mullerian Hormone )', 'AMH'),
('AMH ( Anti - Mullerian Hormone )', 'Anti Mullerian Hormone'),
('AMH ( Anti - Mullerian Hormone )', 'AMH Serum'),
('AMH ( Anti - Mullerian Hormone )', 'Serum AMH'),
('AMH ( Anti - Mullerian Hormone )', 'Mullerian Inhibiting Substance (MIS)'),
('Amylase', 'serum amylase'),
('Amylase', 'pancreatic amylase'),
('Anti-HBc (Total)', 'anti-hbc'),
('Anti-HBc (Total)', 'hepatitis b core antibody'),
('Anti-HBs', 'hepatitis b surface antibody'),
('Anti-TG', 'thyroglobulin antibodies'),
('Anti-TG', 'anti-thyroglobulin'),
('Anti-TG', 'anti thyroglobulin antibodies'),
('Anti-TPO', 'tpo antibodies'),
('Anti-TPO', 'thyroid peroxidase antibodies'),
('Anti-TPO', 'anti-thyroid peroxidase'),
('ApoB/ApoA1 Ratio', 'apo b/apo a1 ratio'),
('ApoB/ApoA1 Ratio', 'APO B / APO A1 RATIO (APO B/A1)'),
('Apolipoprotein A1', 'apolipoprotein a-1'),
('Apolipoprotein A1', 'apo a1'),
('Apolipoprotein A1', 'apo-a1'),
('Apolipoprotein A1', 'APOLIPOPROTEIN - A1 (APO-A1)'),
('Apolipoprotein B', 'apolipoprotein b100'),
('Apolipoprotein B', 'apo b'),
('Apolipoprotein B', 'apo-b'),
('Apolipoprotein B', 'APOLIPOPROTEIN - B (APO-B)'),
('Appearance', 'Urine Appearance'),
('Appearance', 'Urine Transparency'),
('Appearance', 'Urine Clarity'),
('Appearance', 'Urine Physical Appearance'),
('APTT', 'activated partial thromboplastin time'),
('APTT', 'ptt'),
('AST(SGOT)', 'Aspartate Aminotransferase'),
('AST(SGOT)', 'AST'),
('AST(SGOT)', 'SGOT'),
('AST(SGOT)', 'Aspartate Aminotransferase(Serum Glutamic-Oxaloacetic Transaminase)'),
('AST(SGOT)', 'ASPARTATE AMINOTRANSFERASE (SGOT )'),
('AST(SGOT)', 'ast (sgot)'),
('AST(SGOT)', 'aspartate aminotransferase (ast/sgot)'),
('AST(SGOT)', 'ASPARTATE AMINOTRANSFERASE (SGOT)'),
('AST/ALT Ratio', 'sgot/sgpt ratio'),
('AST/ALT Ratio', 'de ritis ratio'),
('Average Blood Glucose', 'abg'),
('Average Blood Glucose', 'Average Blood Glucose (ABG)'),
('Basophils', 'Basophil'),
('Basophils', 'Basophil %'),
('Basophils', 'Basophils %'),
('Basophils', 'Baso %'),
('Basophils', 'Blood Basophils'),
('Basophils', 'Blood Basophil'),
('Bicarbonate ( HCO3⁻)', 'Bicarbonate'),
('Bicarbonate ( HCO3⁻)', 'HCO3'),
('Bicarbonate ( HCO3⁻)', 'HCO3⁻'),
('Bicarbonate ( HCO3⁻)', 'Total CO2'),
('Bicarbonate ( HCO3⁻)', 'TCO2'),
('Bicarbonate ( HCO3⁻)', 'Bicarbonate Serum'),
('Bicarbonate ( HCO3⁻)', 'serum bicarbonate'),
('Blood (Urine)', 'Occult Blood Urine'),
('Blood (Urine)', 'Hemoglobin Urine'),
('Blood (Urine)', 'Hematuria'),
('Blood (Urine)', 'Blood Urine'),
('Blood (Urine)', 'urine blood'),
('Blood Urea', 'Urea'),
('Blood Urea', 'Serum Urea'),
('Blood Urea', 'Sr. Urea'),
('Blood Urea', 'S. Urea'),
('Blood Urea', 'urea nitrogen'),
('Blood Urea', 'urea (calculated)'),
('BNP', 'brain natriuretic peptide'),
('BNP', 'b-type natriuretic peptide'),
('BUN', 'blood urea nitrogen'),
('BUN', 'blood urea nitrogen (bun)'),
('BUN/Creatinine Ratio', 'bun creatinine ratio'),
('BUN/Creatinine Ratio', 'BUN / SR.CREATININE RATIO'),
('C-Peptide', 'c peptide'),
('CA 15-3', 'ca15-3'),
('CA 19-9', 'ca19-9'),
('CA 19-9', 'cancer antigen 19-9'),
('CA-125', 'ca 125'),
('CA-125', 'ca125'),
('CA-125', 'cancer antigen 125'),
('Calcium', 'Serum Calcium'),
('Calcium', 'Total Calcium'),
('Calcium', 'Blood Calcium'),
('Calcium', 'Ca'),
('Calcium', 'Ca2+'),
('Calcium', 'Serum Total Calcium'),
('Calcium', 'Calcium Level'),
('Calcium', 'S. Calcium'),
('Calcium', 'Sr. Calcium'),
('Calcium', 'calcium (total)'),
('Calcium', 'calcium - serum'),
('Calcium', 'calcium serum'),
('Casts', 'Urine Casts'),
('Casts', 'Urinary Casts'),
('Casts', 'Renal Casts'),
('Casts', 'Sediment Casts'),
('CEA', 'carcino embryonic antigen'),
('CEA', 'carcinoembryonic antigen'),
('Chloride (Cl⁻)', 'Serum Chloride'),
('Chloride (Cl⁻)', 'Chloride'),
('Chloride (Cl⁻)', 'Cl'),
('Chloride (Cl⁻)', 'Cl⁻'),
('Chloride (Cl⁻)', 'Chloride Serum'),
('CHOL/HDL ratio', 'TC/HDL Ratio'),
('CHOL/HDL ratio', 'Cholesterol/HDL Ratio'),
('CHOL/HDL ratio', 'Total Cholesterol/HDL Ratio'),
('CHOL/HDL ratio', 'TC : HDL'),
('CHOL/HDL ratio', 'tc / hdl cholesterol ratio'),
('CHOL/HDL ratio', 'tc/hdl'),
('CK-MB', 'creatine kinase mb'),
('CK-MB', 'cpk-mb'),
('Color', 'Urine Colour'),
('Color', 'Urine Color'),
('Color', 'Colour of Urine'),
('Color', 'Urine Appearance Colour'),
('Cortisol', 'serum cortisol'),
('Cortisol', 'cortisol fasting'),
('CPK (Total CK)', 'cpk'),
('CPK (Total CK)', 'creatine kinase'),
('CPK (Total CK)', 'creatine phosphokinase'),
('CRP', 'C-Reactive Protein'),
('CRP', 'Serum CRP'),
('CRP', 'C Reactive Protein'),
('CRP', 'C-REACTIVE PROTEIN (CRP)'),
('Crystals', 'Urine Crystals'),
('Crystals', 'Urinary Crystals'),
('Crystals', 'Crystal Examination'),
('Crystals', 'Crystalluria'),
('Cystatin C', 'serum cystatin c'),
('Dengue NS1 Antigen', 'dengue ns1'),
('DHEA-S', 'dheas'),
('DHEA-S', 'dehydroepiandrosterone sulfate'),
('DHEA-S', 'dhea sulfate'),
('Direct-Bilirubin', 'Direct Bilirubin'),
('Direct-Bilirubin', 'Conjugated Bilirubin'),
('Direct-Bilirubin', 'bilirubin direct'),
('Direct-Bilirubin', 'bilirubin – direct'),
('Direct-Bilirubin', 'db'),
('Eosinophils', 'Eosinophil %'),
('Eosinophils', 'Eosinophils %'),
('Eosinophils', 'Eosinophil percentage'),
('Eosinophils', 'Eos %'),
('Eosinophils', 'Blood Eosinophils'),
('Eosinophils', 'Eosinophil'),
('Eosinophils', 'Eosinophils percentage'),
('Eosinophils', 'Blood Eosinophil'),
('Epithelial Cells', 'Epithelial Cells Urine'),
('Epithelial Cells', 'Urine Epithelial Cells'),
('Epithelial Cells', 'Squamous Epithelial Cells'),
('Epithelial Cells', 'urinary epithelial cells'),
('ESR', 'Erythrocyte Sedimentation Rate'),
('ESR', 'Sed Rat'),
('ESR', 'sed rate'),
('ESR', 'westergren esr'),
('Estimated Average Glucose (eAG)', 'A1c-Derived Average Glucose'),
('Estimated Average Glucose (eAG)', 'Calculated Average Glucose'),
('Estimated Average Glucose (eAG)', 'Mean Plasma Glucose (derived)'),
('Estimated Average Glucose (eAG)', 'Average Blood Glucose (from HbA1c)'),
('Estimated Glomerular Filtration Rate (eGFR)', 'Estimated GFR'),
('Estimated Glomerular Filtration Rate (eGFR)', 'eGFR'),
('Estimated Glomerular Filtration Rate (eGFR)', 'eGFR Calculated'),
('Estimated Glomerular Filtration Rate (eGFR)', 'Estimated Glomerular Filtration Rate'),
('Estimated Glomerular Filtration Rate (eGFR)', 'EST. GLOMERULAR FILTRATION RATE (eGFR)'),
('Estimated Glomerular Filtration Rate (eGFR)', 'gfr estimated'),
('Estimated Glomerular Filtration Rate (eGFR)', 'gfr'),
('Estrogen', 'Estradiol'),
('Estrogen', 'Estradiol (E2)'),
('Estrogen', 'Serum Estradiol'),
('Estrogen', 'E2'),
('Estrogen', 'Estradiol Serum'),
('Estrogen', 'oestradiol'),
('Fasting Blood Sugar', 'Fasting Glucose'),
('Fasting Blood Sugar', 'Fasting Blood Glucose'),
('Fasting Blood Sugar', 'FBG'),
('Fasting Blood Sugar', 'FBS'),
('Fasting Blood Sugar', 'Glucose Fasting'),
('Fasting Blood Sugar', 'Glucose Fasting (FBS)'),
('Fasting Blood Sugar', 'blood glucose fasting'),
('Fasting Blood Sugar', 'fasting blood sugar (glucose)'),
('Fasting Blood Sugar', 'glucose - fasting'),
('Fasting Blood Sugar', 'blood sugar fasting'),
('Fasting Blood Sugar', 'bsl fasting'),
('Follicle Stimulating Hormone (FSH)', 'Serum FSH'),
('Follicle Stimulating Hormone (FSH)', 'FSH Serum'),
('Follicle Stimulating Hormone (FSH)', 'Follicle Stimulating Hormone'),
('Follicle Stimulating Hormone (FSH)', 'fsh'),
('Free PSA', 'psa free'),
('Free T3', 'FT3'),
('Free T3', 'Free Triiodothyronine'),
('Free T3', 'Unbound T3'),
('Free T3', 'Serum free T3'),
('Free T3', 'triiodothyronine free'),
('Free T4', 'FT4'),
('Free T4', 'free thyroxine'),
('Free T4', 'free t4 (ft4)'),
('Free Testosterone', 'testosterone free'),
('GGT', 'gamma gt'),
('GGT', 'gamma-glutamyl transferase'),
('GGT', 'gamma glutamyl transferase'),
('GGT', 'ggtp'),
('GGT', 'GAMMA GLUTAMYL TRANSFERASE (GGT)'),
('Globulin', 'Serum Globulin'),
('Glycated Hemoglobin (HbA1c)', 'HbA1c'),
('Glycated Hemoglobin (HbA1c)', 'Glycated Hemoglobin'),
('Glycated Hemoglobin (HbA1c)', 'Glycosylated Hemoglobin'),
('Glycated Hemoglobin (HbA1c)', 'A1c'),
('Glycated Hemoglobin (HbA1c)', 'Hemoglobin A1c'),
('Glycated Hemoglobin (HbA1c)', 'Glycated Haemoglobin'),
('Glycated Hemoglobin (HbA1c)', 'Glycosylated Haemoglobin'),
('Glycated Hemoglobin (HbA1c)', 'Haemoglobin A1c'),
('Glycated Hemoglobin (HbA1c)', 'Glycated Haemoglobin (HbA1c)'),
('Glycated Hemoglobin (HbA1c)', 'hgba1c'),
('Glycated Hemoglobin (HbA1c)', 'glycohaemoglobin'),
('Glycated Hemoglobin (HbA1c)', 'HbA1c (HPLC)'),
('Growth Hormone', 'gh'),
('HBsAg', 'hepatitis b surface antigen'),
('HBsAg', 'hepatitis b surface antigen (hbsag)'),
('HCV Antibody', 'anti-hcv'),
('HCV Antibody', 'hepatitis c antibody'),
('HDL Cholesterol', 'HDL-C'),
('HDL Cholesterol', 'HDL'),
('HDL Cholesterol', 'High Density Lipoprotein'),
('HDL Cholesterol', 'High Density Lipoprotein Cholesterol'),
('HDL Cholesterol', 'cholesterol - hdl'),
('HDL Cholesterol', 'hdl cholesterol - direct'),
('HDL Cholesterol', 'hdl cholesterol – direct'),
('HDL/LDL Ratio', 'hdl / ldl ratio'),
('Hematocrit', 'PCV'),
('Hematocrit', 'HCT'),
('Hematocrit', 'Packed Cell Volume'),
('Hematocrit', 'Haematocrit'),
('Hematocrit', 'Blood Hematocrit'),
('Hematocrit', 'Whole Blood Hematocrit'),
('Hematocrit', 'HCT level'),
('Hematocrit', 'PCV Value'),
('Hematocrit', 'HCT Value'),
('Hematocrit', 'PCV level'),
('Hematocrit', 'hematocrit (pcv)'),
('Hemoglobin', 'Haemoglobin'),
('Hemoglobin', 'Hb'),
('Hemoglobin', 'Hgb'),
('Hemoglobin', 'Whole Blood Hemoglobin'),
('Hemoglobin', 'Blood Hemoglobin'),
('Hemoglobin', 'Hemoglobin level'),
('Hemoglobin', 'EDTA Blood Hemoglobin'),
('Hemoglobin', 'Hb level'),
('Hemoglobin', 'Hgb level'),
('Hemoglobin', 'Haemoglobin level'),
('Hemoglobin', 'Blood Haemoglobin'),
('Hemoglobin', 'Whole Blood Haemoglobin'),
('Hemoglobin', 'EDTA Blood Haemoglobin'),
('HIV 1&2 Antibodies', 'hiv 1&2'),
('HIV 1&2 Antibodies', 'hiv 1 & 2 antibody'),
('HIV 1&2 Antibodies', 'hiv 1& 2 antibodies'),
('HIV 1&2 Antibodies', 'anti-hiv'),
('HIV 1&2 Antibodies', 'hiv antibody'),
('HOMA-IR', 'homa ir'),
('Homocysteine', 'serum homocysteine'),
('hs-CRP', 'high sensitivity crp'),
('hs-CRP', 'hs crp'),
('hs-CRP', 'hs-c reactive protein'),
('hs-CRP', 'HIGH SENSITIVITY C-REACTIVE PROTEIN (HS-CRP)'),
('hs-Troponin I', 'hs troponin i'),
('hs-Troponin I', 'high sensitivity troponin i'),
('hs-Troponin T', 'hs troponin t'),
('hs-Troponin T', 'high sensitivity troponin t'),
('IGF-1', 'insulin-like growth factor 1'),
('IGF-1', 'somatomedin c'),
('Immature Granulocytes %', 'ig%'),
('Immature Granulocytes %', 'immature granulocytes'),
('Immature Granulocytes %', 'immature granulocyte percent'),
('Immature Granulocytes %', 'IMMATURE GRANULOCYTE PERCENTAGE(IG%)'),
('Indirect-Bilirubin', 'Unconjugated Bilirubin'),
('Indirect-Bilirubin', 'Indirect Bilirubin'),
('Indirect-Bilirubin', 'bilirubin indirect'),
('Indirect-Bilirubin', 'bilirubin – indirect'),
('INR', 'pt inr'),
('INR', 'international normalised ratio'),
('Insulin', 'Serum Insulin'),
('Insulin', 'Fasting Insulin'),
('Insulin', 'Insulin Serum'),
('Insulin', 'insulin fasting'),
('LDH', 'lactate dehydrogenase'),
('LDH', 'lactic dehydrogenase'),
('LDL Cholesterol', 'LDL'),
('LDL Cholesterol', 'Low Density Lipoprotein'),
('LDL Cholesterol', 'LDL-C'),
('LDL Cholesterol', 'Low Density Lipoprotein Cholesterol'),
('LDL Cholesterol', 'cholesterol - ldl'),
('LDL Cholesterol', 'ldl cholesterol - direct'),
('LDL Cholesterol', 'ldl cholesterol – direct'),
('LDL/HDL ratio', 'LDL : HDL Ratio'),
('LDL/HDL ratio', 'LDL/HDL'),
('LDL/HDL ratio', 'LDL-C/HDL-C Ratio'),
('LDL/HDL ratio', 'LDL to HDL Ratio'),
('LDL/HDL ratio', 'Cholesterol Ratio (LDL/HDL)'),
('LDL/HDL ratio', 'Atherogenic Ratio (LDL/HDL)'),
('LDL/HDL ratio', 'ldl / hdl ratio'),
('Lipase', 'serum lipase'),
('Lipoprotein(a)', 'lipoprotein (a)'),
('Lipoprotein(a)', 'lipoprotein a'),
('Lipoprotein(a)', 'lp(a)'),
('Lipoprotein(a)', 'LIPOPROTEIN (A) [LPA]'),
('Lipoprotein(a)', 'LIPOPROTEIN (A) [LP(A)]'),
('Luteinizing Hormone (LH)', 'Luteinising Hormone'),
('Luteinizing Hormone (LH)', 'LH'),
('Luteinizing Hormone (LH)', 'Serum LH'),
('Luteinizing Hormone (LH)', 'LH Serum'),
('Luteinizing Hormone (LH)', 'luteinizing hormone'),
('Lymphocytes', 'Lymphocyte Percentage'),
('Lymphocytes', 'Lymph %'),
('Lymphocytes', 'Blood Lymphocytes'),
('Lymphocytes', 'Lymphocytes Percentage'),
('Lymphocytes', 'Blood Lymphocyte'),
('Lymphocytes', 'lymphocytes %'),
('Lymphocytes', 'lymphocyte count'),
('Lymphocytes', 'LYMPHOCYTE'),
('Magnesium', 'Serum Magnesium'),
('Magnesium', 'Mg'),
('Magnesium', 'Mg2+'),
('Magnesium', 'Serum Mg'),
('Magnesium', 'Magnesium Level'),
('Magnesium', 'S. Magnesium'),
('Magnesium', 'Sr. Magnesium'),
('Malaria Antigen', 'malarial antigen'),
('MCH', 'Mean Corpuscular Hemoglobin'),
('MCH', 'Mean Corpuscular Haemoglobin'),
('MCH', 'RBC MCH'),
('MCH', 'MEAN CORPUSCULAR HEMOGLOBIN(MCH)'),
('MCHC', 'Mean Corpuscular Hemoglobin Concentration'),
('MCHC', 'Mean Corpuscular Haemoglobin Concentration'),
('MCHC', 'RBC MCHC'),
('MCHC', 'MEAN CORP.HEMO.CONC(MCHC)'),
('MCV', 'Mean Corpuscular Volume'),
('MCV', 'Mean Cell Volume'),
('MCV', 'RBC MCV'),
('MCV', 'MEAN CORPUSCULAR VOLUME(MCV)'),
('Microalbumin (Urine)', 'microalbumin'),
('Microalbumin (Urine)', 'urine microalbumin'),
('Microalbumin (Urine)', 'microalbuminuria'),
('Monocytes', 'Monocyte'),
('Monocytes', 'Monocyte %'),
('Monocytes', 'Monocytes %'),
('Monocytes', 'Monocyte percentage'),
('Monocytes', 'Monocyte percent'),
('Monocytes', 'Monocyte relative count'),
('Monocytes', 'Mono %'),
('Monocytes', 'Blood Monocytes'),
('Monocytes', 'Monocyte Relative Perecentage'),
('Monocytes', 'Monocytes Relative Percentage'),
('Monocytes', 'Monocytes Relative %'),
('Monocytes', 'Monocyte Relative %'),
('Monocytes', 'Monocytes Percentage'),
('Monocytes', 'Monocytes percent'),
('Monocytes', 'monocyte count'),
('MPV', 'mean platelet volume'),
('MPV', 'MEAN PLATELET VOLUME(MPV)'),
('Neutrophils', 'Polymorphs (PMNs)'),
('Neutrophils', 'Neutrophil'),
('Neutrophils', 'Neutrophil Percentage'),
('Neutrophils', 'Segmented Neutrophils'),
('Neutrophils', 'Neut %'),
('Neutrophils', 'Blood Neutrophils'),
('Neutrophils', 'NEU %'),
('Neutrophils', 'NEU'),
('Neutrophils', 'neutrophils %'),
('Neutrophils', 'neutrophil count'),
('Neutrophils', 'polymorphs'),
('Neutrophils', 'polymorphonuclear cells'),
('Neutrophils', 'seg'),
('Nitrite', 'Urine Nitrite'),
('Nitrite', 'Nitrites Urine'),
('Nitrite', 'Nitrite Test'),
('Nitrite', 'Nitrite (Dipstick)'),
('Non HDL Cholesterol', 'Non-HDL'),
('Non HDL Cholesterol', 'Non-HDL-C'),
('Non HDL Cholesterol', 'Non-High-Density Lipoprotein Cholesterol'),
('Non HDL Cholesterol', 'Non-High-Density Lipoprotein'),
('Non HDL Cholesterol', 'non-hdl cholesterol'),
('NT-proBNP', 'n-terminal probnp'),
('NT-proBNP', 'proBNP'),
('PDW', 'platelet distribution width'),
('PDW', 'PLATELET DISTRIBUTION WIDTH(PDW)'),
('Phosphorus', 'phosphate'),
('Phosphorus', 'serum phosphorus'),
('Phosphorus', 'phosphorus serum'),
('Phosphorus', 'inorganic phosphate'),
('Platelet Count', 'Thrombocyte Count'),
('Platelet Count', 'Platelet'),
('Platelet Count', 'PLT'),
('Platelet Count', 'Platelets'),
('Platelet Count', 'Blood Platelet Count'),
('Platelet Count', 'Whole Blood Platelet Count'),
('Platelet Count', 'Platelets count'),
('Plateletcrit', 'plateletcrit (pct)'),
('Plateletcrit', 'pct'),
('Postprandial Blood Sugar', 'Postprandial Glucose'),
('Postprandial Blood Sugar', 'PPBS'),
('Postprandial Blood Sugar', 'Glucose (Post Meal)'),
('Postprandial Blood Sugar', 'PP Glucose'),
('Postprandial Blood Sugar', 'PLBS'),
('Postprandial Blood Sugar', 'glucose post prandial'),
('Postprandial Blood Sugar', 'post prandial glucose'),
('Postprandial Blood Sugar', 'glucose post meal'),
('Postprandial Blood Sugar', '2hr pp glucose'),
('Postprandial Blood Sugar', '2 hour pp blood sugar'),
('Potassium (K⁺)', 'Potassium'),
('Potassium (K⁺)', 'Serum Potassium'),
('Potassium (K⁺)', 'K'),
('Potassium (K⁺)', 'K⁺'),
('Potassium (K⁺)', 'Potassium Serum'),
('Progesterone', 'Serum Progesterone'),
('Progesterone', 'Progesterone Serum'),
('Progesterone', 'P4'),
('Progesterone', 'Progesterone Level'),
('Prolactin', 'Serum Prolactin'),
('Prolactin', 'Prolactin Serum'),
('Prolactin', 'PRL'),
('Prolactin', 'PRL Level'),
('Prothrombin Time (PT)', 'prothrombin time'),
('Prothrombin Time (PT)', 'pt'),
('Prothrombin Time (PT)', 'pt time'),
('PSA (Total)', 'psa'),
('PSA (Total)', 'total psa'),
('PSA (Total)', 'prostate specific antigen'),
('PSA (Total)', 'psa total'),
('Pus Cells', 'WBC Urine'),
('Pus Cells', 'Leukocytes Urine'),
('Pus Cells', 'Urine WBC Count'),
('Pus Cells', 'urine pus cells'),
('Pus Cells', 'leucocytes (urine)'),
('Random Blood Glucose', 'Random Glucose'),
('Random Blood Glucose', 'Random Sugar'),
('Random Blood Glucose', 'RBS'),
('Random Blood Glucose', 'RBG'),
('Random Blood Glucose', 'glucose random'),
('Random Blood Glucose', 'random blood sugar'),
('Random Blood Glucose', 'bsl random'),
('RBC', 'Red Blood Cell Count'),
('RBC', 'RBC Count'),
('RBC', 'Erythrocyte Count'),
('RBC', 'Red Cell Count'),
('RBC', 'R.B.C'),
('RBC', 'Total RBC Count'),
('RBC', 'Red Blood Cell'),
('RBC', 'total rbc'),
('RBC Urine', 'Red Blood Cells Urine'),
('RBC Urine', 'Urinary RBC'),
('RBC Urine', 'Erythrocytes Urine'),
('RDW', 'Red Cell Distribution Width'),
('RDW', 'RBC RDW'),
('Serum Aluminium', 'aluminium'),
('Serum Aluminium', 'aluminum'),
('Serum Aluminium', 'serum aluminum'),
('Serum Aluminium', 'al (blood)'),
('Serum Antimony', 'antimony'),
('Serum Antimony', 'sb (blood)'),
('Serum Arsenic', 'arsenic'),
('Serum Arsenic', 'as (blood)'),
('Serum Barium', 'barium'),
('Serum Barium', 'ba (blood)'),
('Serum Beryllium', 'beryllium'),
('Serum Beryllium', 'be (blood)'),
('Serum Bismuth', 'bismuth'),
('Serum Bismuth', 'bi (blood)'),
('Serum Cadmium', 'cadmium'),
('Serum Cadmium', 'cd (blood)'),
('Serum Caesium', 'caesium'),
('Serum Caesium', 'cesium'),
('Serum Caesium', 'cs (blood)'),
('Serum Chromium', 'chromium'),
('Serum Cobalt', 'cobalt'),
('Serum Cobalt', 'co (blood)'),
('Serum Copper', 'copper serum'),
('Serum Copper', 'copper'),
('Serum Creatinine', 'Creatinine Serum'),
('Serum Creatinine', 'S Creatinine'),
('Serum Creatinine', 'Sr Creatinine'),
('Serum Creatinine', 'SCr'),
('Serum Creatinine', 'Creatinine Level'),
('Serum Creatinine', 'CREA'),
('Serum Creatinine', 'CREAT'),
('Serum Creatinine', 'Blood Creatinine'),
('Serum Creatinine', 'Creatinine'),
('Serum Creatinine', 'creatinine - serum'),
('Serum Creatinine', 's. creatinine'),
('Serum Ferritin', 'Ferritin Serum'),
('Serum Ferritin', 'Ferritin'),
('Serum Iron', 'Fe'),
('Serum Iron', 'Iron'),
('Serum Iron', 'Serum Fe'),
('Serum Iron', 'iron serum'),
('Serum Lead', 'lead'),
('Serum Manganese', 'manganese'),
('Serum Manganese', 'mn (blood)'),
('Serum Mercury', 'mercury'),
('Serum Mercury', 'total mercury'),
('Serum Molybdenum', 'molybdenum'),
('Serum Molybdenum', 'mo (blood)'),
('Serum Nickel', 'nickel'),
('Serum Nickel', 'ni (blood)'),
('Serum Selenium', 'selenium'),
('Serum Selenium', 'se (blood)'),
('Serum Silver', 'silver'),
('Serum Silver', 'ag (blood)'),
('Serum Strontium', 'strontium'),
('Serum Strontium', 'sr (blood)'),
('Serum Thallium', 'thallium'),
('Serum Thallium', 'tl (blood)'),
('Serum Tin', 'tin'),
('Serum Tin', 'sn (blood)'),
('Serum Uranium', 'uranium'),
('Serum Uranium', 'u (blood)'),
('Serum Uric Acid', 'Uric Acid'),
('Serum Uric Acid', 'Sr. Uric Acid'),
('Serum Uric Acid', 'S. Uric Acid'),
('Serum Uric Acid', 'Blood Uric Acid'),
('Serum Uric Acid', 'URIC'),
('Serum Uric Acid', 'UA'),
('Serum Uric Acid', 'urate'),
('Serum Vanadium', 'vanadium'),
('Serum Vanadium', 'v (blood)'),
('Serum Zinc', 'zinc serum'),
('Serum Zinc', 'zinc'),
('Sodium ( Na+)', 'Serum Sodium'),
('Sodium ( Na+)', 'Na'),
('Sodium ( Na+)', 'Na+'),
('Sodium ( Na+)', 'Sodium'),
('Sodium ( Na+)', 'Na Serum'),
('Testosterone', 'Total Testosterone'),
('Testosterone', 'Serum Testosterone'),
('Testosterone', 'Testosterone Serum'),
('Testosterone', 'Testosterone Total'),
('Testosterone', 'TST'),
('Testosterone', 'T'),
('Thyroid Stimulating Hormone (TSH)', 'TSH'),
('Thyroid Stimulating Hormone (TSH)', 'Thyrotropin'),
('Thyroid Stimulating Hormone (TSH)', 'Thyroid Stimulating Hormone'),
('Thyroid Stimulating Hormone (TSH)', 'tsh - ultrasensitive'),
('Thyroid Stimulating Hormone (TSH)', 'tsh – ultrasensitive'),
('TIBC (Total Iron Binding Capacity)', 'TIBC'),
('TIBC (Total Iron Binding Capacity)', 'Total Iron Binding Capacity'),
('TIBC (Total Iron Binding Capacity)', 'TOTAL IRON BINDING CAPACITY (TIBC)'),
('Total Bilirubin', 'Bilirubin Total'),
('Total Bilirubin', 'TB'),
('Total Bilirubin', 't. bili'),
('Total Bilirubin', 't.bilirubin'),
('Total Bilirubin', 'bilirubin – total'),
('Total Cholesterol', 'Cholesterol'),
('Total Cholesterol', 'Serum Cholesterol'),
('Total Cholesterol', 'TC'),
('Total Cholesterol', 'cholesterol total'),
('Total Cholesterol', 'cholesterol - total'),
('Total Protein', 'Serum Protein'),
('Total Protein', 'protein total'),
('Total Protein', 'serum total protein'),
('Total Protein', 'protein – total'),
('Total Thyroxine (T4)', 'T4'),
('Total Thyroxine (T4)', 'Total T4'),
('Total Thyroxine (T4)', 'Thyroxine'),
('Total Thyroxine (T4)', 'Total Thyroxine'),
('Total Thyroxine (T4)', 'Thyroxine (T4)'),
('Total Thyroxine (T4)', 't4 total'),
('Total Triiodothyronine (T3)', 'Triiodothyronine'),
('Total Triiodothyronine (T3)', 'T3'),
('Total Triiodothyronine (T3)', 'Total T3'),
('Total Triiodothyronine (T3)', 'Triiodothyronine (T3)'),
('Total Triiodothyronine (T3)', 't3 total'),
('TPHA', 'treponema pallidum hemagglutination'),
('Transferrin Saturation (%)', '% Saturation'),
('Transferrin Saturation (%)', 'Transferrin Saturation'),
('Transferrin Saturation (%)', 'Iron Saturation'),
('Transferrin Saturation (%)', '% Iron Saturation'),
('Transferrin Saturation (%)', '% transferrin saturation'),
('Transferrin Serum', 'serum transferrin'),
('Transferrin Serum', 'transferrin'),
('Trig/HDL Ratio', 'trig / hdl ratio'),
('Trig/HDL Ratio', 'triglyceride/hdl ratio'),
('Triglycerides', 'Triglyceride'),
('Triglycerides', 'TG'),
('Triglycerides', 'trigs'),
('Triglycerides', 'serum triglycerides'),
('Triglycerides', 'triacylglycerol'),
('Troponin I', 'cardiac troponin i'),
('Troponin T', 'cardiac troponin t'),
('UIBC (Unsaturated Iron Binding Capacity)', 'UIBC'),
('UIBC (Unsaturated Iron Binding Capacity)', 'Unsaturated Iron Binding Capacity'),
('UIBC (Unsaturated Iron Binding Capacity)', 'UNSAT IRON-BINDING CAPACITY (UIBC)'),
('UIBC (Unsaturated Iron Binding Capacity)', 'UNSAT.IRON-BINDING CAPACITY(UIBC)'),
('Urine Bilirubin', 'Urinary Bilirubin'),
('Urine Bilirubin', 'Bile Pigments'),
('Urine Bilirubin', 'Bilirubin Urine'),
('Urine Bilirubin', 'bilirubin (urine)'),
('Urine Glucose', 'Urinary Glucose'),
('Urine Glucose', 'Sugar Urine'),
('Urine Glucose', 'Glycosuria'),
('Urine Glucose', 'glucose (urine)'),
('Urine Ketones', 'Ketone Bodies Urine'),
('Urine Ketones', 'Urinary Ketones'),
('Urine Ketones', 'Acetone Urine'),
('Urine Ketones', 'ketone'),
('Urine Ketones', 'ketones (urine)'),
('Urine Leukocyte Esterase', 'leucocyte esterase'),
('Urine Odor', 'Odor Urine'),
('Urine Odor', 'Urinary Odor'),
('Urine Odor', 'Urine Smell'),
('Urine Odor', 'Urinary Smell'),
('Urine Odor', 'Odour of Urine'),
('Urine Odor', 'Urine Odour'),
('Urine Odor', 'Urine Smell Examination'),
('Urine Odor', 'Urine Odor Examination'),
('Urine pH', 'urinary ph'),
('Urine Protein', 'Protein Urine'),
('Urine Protein', 'Urinary Protein'),
('Urine Protein', 'Urine Albumin'),
('Urine Protein', 'Proteinuria'),
('Urine Protein', 'protein (urine)'),
('Urine Specific Gravity', 'Urinary Specific Gravity'),
('Urine Specific Gravity', 'SG'),
('Urine Specific Gravity', 'Sp. Gravity'),
('Urine Specific Gravity', 'Urine SG'),
('Urine Specific Gravity', 'Specific Gravity'),
('Urine Volume', 'Sample Volume'),
('Urine Volume', 'Urine Quantity'),
('Urobilinogen', 'Urine Urobilinogen'),
('Urobilinogen', 'Urinary Urobilinogen'),
('Urobilinogen', 'UBG'),
('Urobilinogen', 'Urobilinogen Urine'),
('VDRL', 'vdrl test'),
('Vitamin A', 'Serum Vitamin A'),
('Vitamin A', 'Vit A'),
('Vitamin A', 'Retinol'),
('Vitamin A', 'Serum Retinol'),
('Vitamin A', 'Vitamin A (Retinol)'),
('Vitamin A', 'Retinol/ Vitamin A'),
('Vitamin B1 (Thiamin)', 'vitamin b1'),
('Vitamin B1 (Thiamin)', 'thiamin'),
('Vitamin B1 (Thiamin)', 'thiamine'),
('Vitamin B1 (Thiamin)', 'vitamin b1 / thiamin'),
('Vitamin B12', 'Vit B12'),
('Vitamin B12', 'Cobalamin'),
('Vitamin B12', 'Serum Vitamin B12'),
('Vitamin B12', 'Vitamin B12 (Cobalamin)'),
('Vitamin B12', 'Cyanocobalamin'),
('Vitamin B12', 'Serum Cobalamin'),
('Vitamin B12', 'Vitamin B12 (Serum)'),
('Vitamin B12', 'VITAMIN B-12'),
('Vitamin B12', 'b12'),
('Vitamin B2 (Riboflavin)', 'vitamin b2'),
('Vitamin B2 (Riboflavin)', 'riboflavin'),
('Vitamin B2 (Riboflavin)', 'vitamin b2 / riboflavin'),
('Vitamin B3 (Niacin)', 'vitamin b3'),
('Vitamin B3 (Niacin)', 'nicotinic acid'),
('Vitamin B3 (Niacin)', 'niacin'),
('Vitamin B3 (Niacin)', 'vitamin b3 / nicotinic acid'),
('Vitamin B5 (Pantothenic)', 'vitamin b5'),
('Vitamin B5 (Pantothenic)', 'pantothenic acid'),
('Vitamin B5 (Pantothenic)', 'pantothenate'),
('Vitamin B5 (Pantothenic)', 'vitamin b5 / pantothenic'),
('Vitamin B6 (P5P)', 'vitamin b6'),
('Vitamin B6 (P5P)', 'pyridoxal-5-phosphate'),
('Vitamin B6 (P5P)', 'vitamin b6 / pyridoxal-5-phosphate'),
('Vitamin B7 (Biotin)', 'vitamin b7'),
('Vitamin B7 (Biotin)', 'biotin'),
('Vitamin B7 (Biotin)', 'vitamin b7 / biotin'),
('Vitamin B9', 'Folate'),
('Vitamin B9', 'Serum Folate'),
('Vitamin B9', 'Folic Acid'),
('Vitamin B9', 'Vitamin B9 (Folate)'),
('Vitamin B9', 'Serum Folic Acid'),
('Vitamin B9', 'vitamin b9 / folic acid'),
('Vitamin D', 'Vit D'),
('Vitamin D', 'Vitamin D (25 Hydroxy)'),
('Vitamin D', '25-OH Vitamin D'),
('Vitamin D', 'Vitamin D Total'),
('Vitamin D', '25-Hydroxy Vitamin D'),
('Vitamin D', '25(OH) Vitamin D'),
('Vitamin D', '25-Hydroxycholecalciferol'),
('Vitamin D', '25-OH VITAMIN D (TOTAL)'),
('Vitamin D', 'vitamin d (25-oh)'),
('Vitamin D', '25 hydroxyvitamin d'),
('Vitamin D', 'calciferol'),
('Vitamin D', '25-hydroxyvitamin d3'),
('Vitamin D2', 'ergocalciferol'),
('Vitamin D3', 'cholecalciferol'),
('Vitamin E', 'alpha-tocopherol'),
('Vitamin E', 'tocopherol'),
('VLDL Cholesterol', 'VLDL'),
('VLDL Cholesterol', 'Very Low Density Lipoprotein'),
('VLDL Cholesterol', 'cholesterol vldl'),
('WBC', 'White Blood Cell Count'),
('WBC', 'Total Leucocyte Count'),
('WBC', 'Total WBC Count'),
('WBC', 'WBC Count'),
('WBC', 'TLC'),
('WBC', 'Leukocyte Count'),
('WBC', 'Blood WBC Count'),
('WBC', 'Whole Blood WBC Count'),
('WBC', 'Total WBC'),
('WBC', 'Total Leukocyte Count'),
('WBC', 'TOTAL LEUCOCYTES COUNT (WBC)'),
('WBC', 'leucocyte count'),
('Absolute Basophil Count', 'Basophil Count Absolute'),
('Absolute Basophil Count', 'Count Basophil Absolute'),
('Absolute Basophil Count', 'Baso. Abs.'),
('Absolute Basophil Count', 'Abs. Baso.'),
('Absolute Basophil Count', 'ABC Count'),
('Absolute Basophil Count', 'Whole Blood Absolute Basophil Count'),
('Absolute Basophil Count', 'Blood Absolute Basophils'),
('Absolute Eosinophil Count', 'Count Eosinophil Absolute'),
('Absolute Eosinophil Count', 'Absolute Count Eosinophils'),
('Absolute Eosinophil Count', 'Abs. Eos.'),
('Absolute Eosinophil Count', 'Eos. Abs.'),
('Absolute Eosinophil Count', 'AEC Count'),
('Absolute Eosinophil Count', 'S. Eosinophil Absolute Count'),
('Absolute Eosinophil Count', 'Blood Absolute Eosinophil Count'),
('Absolute Lymphocyte Count', 'Count Lymphocytes Absolute'),
('Absolute Lymphocyte Count', 'Absolute Count Lymphocyte'),
('Absolute Lymphocyte Count', 'Abs. Lymphs.'),
('Absolute Lymphocyte Count', 'Lymphs. Abs.'),
('Absolute Lymphocyte Count', 'ALC Count'),
('Absolute Lymphocyte Count', 'Serum Lymphocyte Absolute'),
('Absolute Lymphocyte Count', 'Blood Absolute Lymphocyte Count'),
('Absolute Monocyte Count', 'Count Monocyte Absolute'),
('Absolute Monocyte Count', 'Absolute Count Monocytes'),
('Absolute Monocyte Count', 'Abs. Monos.'),
('Absolute Monocyte Count', 'Monos. Abs.'),
('Absolute Monocyte Count', 'AMC Count'),
('Absolute Monocyte Count', 'Blood Absolute Monocyte Count'),
('Absolute Monocyte Count', 'Serum Monocyte Absolute'),
('Absolute Neutrophil Count', 'Count Neutrophils Absolute'),
('Absolute Neutrophil Count', 'Absolute Count Neutrophil'),
('Absolute Neutrophil Count', 'Abs. Neuts.'),
('Absolute Neutrophil Count', 'Neuts. Abs.'),
('Absolute Neutrophil Count', 'ANC Count'),
('Absolute Neutrophil Count', 'Blood Absolute Neutrophil Count'),
('Absolute Neutrophil Count', 'Serum Neutrophils Absolute'),
('A/G ratio', 'Globulin Albumin Ratio'),
('A/G ratio', 'Ratio Albumin Globulin'),
('A/G ratio', 'Alb/Glob Ratio'),
('A/G ratio', 'A:G Ratio'),
('A/G ratio', 'Alb Glob Ratio'),
('A/G ratio', 'Serum A/G Ratio'),
('A/G ratio', 'Serum Albumin/Globulin Ratio'),
('A/G ratio', 'Albumin Globulin Ratio (Calculated)'),
('AFP', 'Fetoprotein Alpha'),
('AFP', 'Alpha-Fetoprot.'),
('AFP', 'AFP-Tumor Marker'),
('AFP', 'Serum Alpha Fetoprotein'),
('AFP', 'S. Alpha-Fetoprotein'),
('Albumin', 'S. Albumin'),
('Albumin', 'Alb. Serum'),
('Albumin', 'S. Alb'),
('Alkaline Phosphatase', 'Phosphatase Alkaline'),
('Alkaline Phosphatase', 'Alk. Phos.'),
('Alkaline Phosphatase', 'ALP-Total'),
('Alkaline Phosphatase', 'S. Alkaline Phosphatase'),
('ALT(SGPT)', 'Aminotransferase Alanine'),
('ALT(SGPT)', 'Transaminase Alanine'),
('ALT(SGPT)', 'SGPT (Alanine Aminotransferase)'),
('ALT(SGPT)', 'Serum Alanine Aminotransferase'),
('ALT(SGPT)', 'S. Alanine Transaminase'),
('AMH ( Anti - Mullerian Hormone )', 'Hormone Anti-Mullerian'),
('AMH ( Anti - Mullerian Hormone )', 'Mullerian Anti Hormone'),
('AMH ( Anti - Mullerian Hormone )', 'Inhibiting Substance Mullerian'),
('AMH ( Anti - Mullerian Hormone )', 'M.I.S.'),
('AMH ( Anti - Mullerian Hormone )', 'AMH Count'),
('AMH ( Anti - Mullerian Hormone )', 'Blood Anti-Mullerian Hormone'),
('Amylase', 'Amylase Serum'),
('Amylase', 'Amylase Pancreatic'),
('Amylase', 'S. Amylase'),
('Amylase', 'P. Amylase'),
('Amylase', 'Amy.'),
('Amylase', 'Blood Amylase'),
('APTT', 'Partial Thromboplastin Time Activated'),
('APTT', 'Thromboplastin Time Partial Activated'),
('APTT', 'Blood PTT'),
('AST/ALT Ratio', 'ALT/AST Ratio'),
('AST/ALT Ratio', 'Ratio AST ALT.'),
('AST/ALT Ratio', 'SGPT/SGOT Ratio.'),
('AST/ALT Ratio', 'Serum AST/ALT Ratio'),
('Anti-HBc (Total)', 'Total Anti-HBc'),
('Anti-HBc (Total)', 'Hepatitis B Core Antibody Total'),
('Anti-HBc (Total)', 'HBcAb Total'),
('Anti-HBc (Total)', 'Serum Anti-HBc Total'),
('Anti-HBc (Total)', 'Anti-HBc Tot.'),
('Anti-HBs', 'HBs Antibody'),
('Anti-HBs', 'Surface Antibody (Hepatitis B)'),
('Anti-HBs', 'HBsAb'),
('Anti-HBs', 'Anti-HBs Count'),
('Anti-HBs', 'Hepatitis B Surf Ab'),
('Anti-HBs', 'Serum Anti-HBs'),
('Anti-TG', 'TG Antibody Anti'),
('Anti-TG', 'S. Anti-Thyroglobulin'),
('ApoB/ApoA1 Ratio', 'Ratio ApoB ApoA1'),
('ApoB/ApoA1 Ratio', 'B:A1 Ratio.'),
('ApoB/ApoA1 Ratio', 'Apolipoprotein B/A1'),
('Apolipoprotein A1', 'Serum Apolipoprotein A1'),
('Apolipoprotein B', 'B Apolipoprotein'),
('Apolipoprotein B', 'Serum Apolipoprotein B'),
('Average Blood Glucose', 'Blood Glucose Average'),
('Average Blood Glucose', 'Glucose Average Blood'),
('Average Blood Glucose', 'Avg. Glucose'),
('Average Blood Glucose', 'Serum Average Glucose'),
('BNP', 'Serum BNP'),
('BNP', 'Natriuretic Peptide B-Type'),
('BNP', 'Serum B-Type Natriuretic Peptide'),
('BUN', 'Serum BUN'),
('BUN/Creatinine Ratio', 'Ratio BUN Creatinine'),
('BUN/Creatinine Ratio', 'BUN/Creatinine Ratio (Calculated)'),
('Basophils', 'Serum Basophil'),
('Bicarbonate ( HCO3⁻)', 'Total Bicarbonate'),
('Bicarbonate ( HCO3⁻)', 'Bicarbonate Total'),
('Bicarbonate ( HCO3⁻)', 'CO2 Total'),
('Bicarbonate ( HCO3⁻)', 'Bicarb'),
('Vitamin K', 'serum vitamin K'),
('C-Peptide', 'C-Pep'),
('C-Peptide', 'Serum C-Peptide'),
('CA 15-3', '15-3 CA'),
('CA 19-9', '19-9 CA'),
('Vitamin E', 'serum vitamin E'),
('CA-125', 'Antigen Cancer 125'),
('Vitamin E', 'Serum Alpha-tocopherol'),
('CA-125', '125 CA'),
('Vitamin E', 'Vitamin E (Serum)'),
('CA-125', 'Cancer Ant. 125'),
('Vitamin D', '25 - OH cholecalciferol'),
('Vitamin D', '25 hydroxycholecalciferol'),
('Vitamin D3', 'serum vitamin D3'),
('Vitamin D2', 'vitamin D2 ( Ergocalciferol )'),
('CEA', 'Antigen Carcinoembryonic'),
('CPK (Total CK)', 'Serum CPK'),
('CPK (Total CK)', 'S. CPK'),
('CRP', 'S. CRP'),
('Chloride (Cl⁻)', 'S. Chlolride'),
('Total Cholesterol', 'T. Chol'),
('Total Cholesterol', 'CHOL. Total'),
('Total Cholesterol', 'S. Cholesterol'),
('VLDL Cholesterol', 'Serum VLDL Cholesterol'),
('VLDL Cholesterol', 'VLDL Cholesterol Serum'),
('Cortisol', 'Cortisol Serum'),
('Cortisol', 'S. Cortisol'),
('Serum Creatinine', 'Creat.'),
('Cystatin C', 'S. Cystatin C'),
('DHEA-S', 'Serum DHEA-S'),
('Dengue NS1 Antigen', 'Dengue NS1 Ag'),
('Dengue NS1 Antigen', 'NS1 Dengue Antigen'),
('Direct-Bilirubin', 'D. Bili'),
('Epithelial Cells', 'Cells Epithelial'),
('Follicle Stimulating Hormone (FSH)', 'Hormone Follicle Stimulating'),
('Free PSA', 'fPSA'),
('Vitamin B2 (Riboflavin)', 'Vitamin B- 2'),
('Free T3', 'T3 Free'),
('Free T4', 't4 free'),
('Free Testosterone', 'fTesto'),
('Globulin', 'Globulin (Calculated)'),
('Growth Hormone', 'Hormone Growth'),
('Growth Hormone', 'hGH'),
('HBsAg', 'Antigen Hepatitis B Surface'),
('HCV Antibody', 'Antibody HCV'),
('HDL/LDL Ratio', 'Ratio hdl/ldl'),
('HIV 1&2 Antibodies', 'Antibodies HIV 1&2'),
('Vitamin B1 (Thiamin)', 'Thiamine ( vitamin B1 )'),
('HOMA-IR', 'HOMA Index'),
('Glycated Hemoglobin (HbA1c)', 'A1c Hemoglobin'),
('Hemoglobin', 'Hemoglobin (Hb)'),
('Epithelial Cells', 'Epithelial Cell Count'),
('Homocysteine', 'S. Homocysteine'),
('IGF-1', 'Growth Factor Insulin-like 1'),
('Immature Granulocytes %', 'Granulocytes Immature %.'),
('Indirect-Bilirubin', 'Ind. Bilirubin'),
('Indirect-Bilirubin', 'I. Bili'),
('Urobilinogen', 'URO'),
('Insulin', 'F. Insulin'),
('Urine Volume', 'Urinary volume'),
('Urine Volume', 'Urine Amount'),
('Luteinizing Hormone (LH)', 'Hormone Luteinizing'),
('Urine Volume', 'Volume ( Urine )'),
('Lipase', 'S. Lipase'),
('Lymphocytes', 'Lymphs'),
('Lymphocytes', 'lymph'),
('Urine Specific Gravity', 'Specific Gravity ( Urine )'),
('MCV', 'Volume Corpuscular Mean'),
('MPV', 'Volume Mean Platelet'),
('MPV', 'Platelet mean Volume'),
('Urine Protein', 'Protein'),
('Magnesium', 'Magnesium Serum'),
('Malaria Antigen', 'Antigen Malaria'),
('Malaria Antigen', 'Malaria Ag'),
('Crystals', 'Urine sediment crystals'),
('Mentzer Index', 'Index Mentzer'),
('Microalbumin (Urine)', 'Microalb'),
('Monocytes', 'Monos.'),
('Monocytes', 'Mono'),
('NT-proBNP', 'NT-BNP'),
('NT-proBNP', 'proBNP NT'),
('Casts', 'Casts ( Urine )'),
('Neutrophils', 'NEUTS'),
('Non HDL Cholesterol', 'Cholesterol Non-HDL'),
('Non HDL Cholesterol', 'NHDL-C'),
('PDW', 'P.D.W.'),
('Urine pH', 'Ur pH'),
('Urine pH', 'pH ( U )'),
('Urine Odor', 'Smell'),
('Urine Odor', 'Aroma'),
('Urine Leukocyte Esterase', 'Leukocyte Esterase ( Urine )'),
('Urine Ketones', 'Acetoacetate'),
('Urine Glucose', 'Urine Sugar'),
('Urine Glucose', 'Sugar ( Urine )'),
('Urine Glucose', 'Glucose - Urine'),
('Urine Bilirubin', 'Bile Pigments ( Urine )'),
('RBC Urine', 'Urine RBC'),
('RBC Urine', 'RBC Count ( Urine )'),
('Nitrite', 'Nitrite (NIT)'),
('Nitrite', 'NIT'),
('Thyroid Stimulating Hormone (TSH)', 'TSH ( Serum )'),
('Thyroid Stimulating Hormone (TSH)', 'Serum Thyrotropin'),
('Testosterone', 'Testosterone level'),
('Free Testosterone', 'FT'),
('Free Testosterone', 'Free T'),
('Free Testosterone', 'Free Serum Testosterone'),
('Progesterone', 'Prog'),
('Luteinizing Hormone (LH)', 'LH Level'),
('Follicle Stimulating Hormone (FSH)', 'Follicle - Stimulating Hormone'),
('Follicle Stimulating Hormone (FSH)', 'Serum Follicle Stimulating Hormone'),
('Estrogen', 'Estrogen Serum'),
('Cortisol', 'Total Cortisol'),
('Cortisol', 'Cortisol Level'),
('Sodium ( Na+)', 'Sodium (Na⁺)'),
('Sodium ( Na+)', 'Sodium level'),
('Potassium (K⁺)', 'K (Serum)'),
('Potassium (K⁺)', 'K+ (S)'),
('Calcium', 'T. Calcium'),
('Serum Uric Acid', 'SUA'),
('Estimated Glomerular Filtration Rate (eGFR)', 'Glomerular Filtration Rate'),
('Estimated Glomerular Filtration Rate (eGFR)', 'Filtration Rate Estimated Glomerular'),
('Phosphorus', 'Serum Inorganic Phosphate'),
('Phosphorus', 'Serum Phosphate'),
('CPK (Total CK)', 'Total CK'),
('Microalbumin (Urine)', 'MAU'),
('Malaria Antigen', 'Malaria RDT'),
('Malaria Antigen', 'PfHRP2'),
('Malaria Antigen', 'pLDH'),
('Malaria Antigen', 'Malaria Rapid Test'),
('Troponin I', 'cTnI'),
('Troponin T', 'cTnT'),
('UIBC (Unsaturated Iron Binding Capacity)', 'Latent Iron Binding Capacity (LIBC)'),
('Lipase', 'Pancreatic Lipase'),
('Lipase', 'LPS'),
('VDRL', 'Treponema Test'),
('TPHA', 'Syphilis TPHA'),
('Serum Manganese', 'S. Manganese'),
('Serum Vanadium', 'Blood Vanadium'),
('Serum Vanadium', 'Whole Blood Vanadium'),
('Serum Vanadium', 'Serum Vanadium Level'),
('Serum Vanadium', 'S. Vanadium'),
('Serum Uranium', 'Serum Uranium Test'),
('Serum Uranium', 'Serum Uranium Level'),
('Serum Thallium', 'Serum Thallium Level'),
('Serum Thallium', 'Thallium Level (Blood)'),
('Serum Thallium', 'Thallium – Whole Blood'),
('Serum Thallium', 'S. Thallium'),
('Serum Selenium', 'Serum Selenium Level'),
('Serum Selenium', 'Serum Selenium Test'),
('Serum Selenium', 'Selenium, Serum'),
('CPK (Total CK)', 'Total Creatine Kinase'),
('Triglycerides', 'S. Triglycerides'),
('Triglycerides', 'Serum TG'),
('hs-Troponin I', 'Troponin I - hs'),
('hs-Troponin T', 'Troponin T High Sensitivity'),
('hs-Troponin T', 'hs Troponin T High Sensitivity'),
('PSA (Total)', 'tPSA'),
('Total Thyroxine (T4)', 'S. T4'),
('Total Thyroxine (T4)', 'Serum T4'),
('Total Triiodothyronine (T3)', 'Serum T3'),
('Transferrin Saturation (%)', 'TSAT'),
('Transferrin Saturation (%)', 'Transferrin Sat'),
('Total Bilirubin', 'S. Bilirubin Total'),
('Total Bilirubin', 'Serum Bilirubin'),
('Total Bilirubin', 'Serum Bilirubin T'),
('Total Bilirubin', 'Total Serum Bilirubin'),
('Serum Zinc', 'Zn'),
('Serum Aluminium', 'S. Aluminium'),
('Serum Copper', 'S. Copper'),
('Serum Copper', 'Total Copper'),
('Anti-TPO', 'TPO antibody'),
('Anti-TPO', 'Anti-TPO Ab'),
('Vitamin B9', 'Folacin'),
('Serum Chromium', 'Serum Cr'),
('Serum Chromium', 'Chromium – Serum'),
('Serum Antimony', 'Antimony – Blood'),
('Serum Antimony', 'Antimony (Serum)'),
('Serum Beryllium', 'Blood Beryllium'),
('Serum Beryllium', 'Serum Be'),
('Serum Cadmium', 'Cd (Whole Blood)'),
('Serum Iron', 'S. Iron'),
('Blood (Urine)', 'Hb (Urine)'),
('Serum Silver', 'Serum Ag'),
('Prolactin', 'S.Prolactin'),
('Prolactin', 'S.PRL'),
('Growth Hormone', 'Serum GH'),
('Growth Hormone', 'hGH ( Human Growth Hormone )'),
('Chloride (Cl⁻)', 'S. Chloride'),
('Magnesium', 'Mag  ( serum )'),
('Magnesium', 'Magnesium Total'),
('Total Protein', 'S. Total Protein'),
('Total Protein', 'TP'),
('Transferrin Serum', 'S. Transferrin'),
('Transferrin Serum', 'TRF'),
('Average Blood Glucose', 'Mean Blood Glucose'),
('INR', 'Prothrombin INR'),
('Pus Cells', 'Urinary leukocytes'),
('Pus Cells', 'Leukocyte Count ( Urine )'),
('NT-proBNP', 'NT-proBNP (Serum)'),
('LDH', 'LDH Total'),
('LDH', 'S. LDH'),
('Mentzer Index', 'Mentzer Ratio (Calculated)'),
('VDRL', 'RPR'),
('VDRL', 'Syphilis Serology'),
('Serum Manganese', 'Manganese (Serum)'),
('Serum Manganese', 'Serum Mn level'),
('Serum Uranium', 'Blood Uranium'),
('Serum Uranium', 'S. Uranium'),
('Serum Strontium', 'Strontium (Serum)'),
('Serum Strontium', 'Sr – Serum level'),
('Serum Strontium', 'S. Strontium'),
('DHEA-S', 'DHEA-S (Serum)'),
('DHEA-S', 'DHEA Sulphate – Serum'),
('DHEA-S', 'DHEAS Level'),
('Serum Tin', 'Tin,Serum'),
('Serum Tin', 'Sn,Serum'),
('Serum Tin', 'Tin Total'),
('Serum Tin', 'Sn ( Tin )'),
('Serum Lead', 'B-Pb'),
('Serum Lead', 'Pb Level'),
('CK-MB', 'CK-MB (Serum)'),
('CK-MB', 'CK-MB Mass'),
('CK-MB', 'Creatine Kinase MB Isoenzyme'),
('Plateletcrit', 'plateletocrit'),
('Plateletcrit', 'Thrombocrit'),
('Fasting Blood Sugar', 'B. Sugar (F)'),
('Fasting Blood Sugar', 'Glucose (F)'),
('hs-CRP', 'S. hs-CRP (Serum prefix)'),
('hs-CRP', 'CRP (High Sensitivity)'),
('Lipoprotein(a)', 'A Lipoprotein'),
('Lipoprotein(a)', 'Serum Lipoprotein A'),
('Prothrombin Time (PT)', 'PT (Seconds)'),
('Prothrombin Time (PT)', 'Time Prothrombin'),
('RDWI', 'Red Cell Distribution Width Index'),
('RDWI', 'RDWI (Calculated)'),
('Total Triiodothyronine (T3)', 'S. T3'),
('TIBC (Total Iron Binding Capacity)', 'S. TIBC'),
('Serum Ferritin', 'S. Ferritin'),
('Serum Ferritin', 'Iron Stores'),
('Appearance', 'Visual appearance'),
('Appearance', 'Macroscopic appearance'),
('PDW', 'PDW-SD'),
('Vitamin B7 (Biotin)', 'Serum Biotin'),
('Vitamin A', 'Total Vitamin A'),
('Vitamin A', 'Retinol (Vitamin A)'),
('Serum Caesium', 'Caesium (Serum)'),
('Serum Molybdenum', 'Serum Mo'),
('Serum Molybdenum', 'Molybdenum (Serum)'),
('Serum Nickel', 'Nickel (Serum)'),
('Serum Arsenic', 'Arsenic (Blood)'),
('Serum Arsenic', 'Arsenic (Serum)'),
('Serum Bismuth', 'Bi (Serum)'),
('Serum Bismuth', 'Bismuth (Serum)'),
('Serum Bismuth', 'Serum Bi'),
('BUN/Creatinine Ratio', 'Serum Urea / Creatinine Ratio'),
('Serum Barium', 'Barium (Serum)'),
('Serum Cobalt', 'Cobalt (Blood)'),
('Serum Cadmium', 'Cadmium Urine (L)*'),
('Serum Cadmium', 'CADMIUM, RANDOM URINE'),
('Serum Cadmium', 'Whole Blood Cadmium'),
('Serum Cadmium', 'Random Urine Cadmium'),
('Serum Cadmium', 'S. Cadmium'),
('Serum Cadmium', 'U. Cadmium'),
('Trig/HDL Ratio', 'TG:HDL Ratio'),
('Trig/HDL Ratio', 'Triglycerides/HDL-C'),
('Trig/HDL Ratio', 'Triglycerides/HDL-C ratio'),
('Trig/HDL Ratio', 'TG:HDL'),
('Trig/HDL Ratio', 'Trig/HDL-C'),
('Trig/HDL Ratio', 'Trig/HDL-C Ratio'),
('Plateletcrit', 'Platelet hematocrit'),
('Plateletcrit', 'PCT %'),
('CPK (Total CK)', 'CK total'),
('CPK (Total CK)', 'Total creatinine phosphokinase'),
('CPK (Total CK)', 'Creatine kinase total'),
('hs-Troponin I', 'hs-cTnl'),
('hs-Troponin I', 'High sensitive cardiac troponin I'),
('hs-Troponin I', 'hs Tnl'),
('Estrogen', 'Estrogen (E2)'),
('Estrogen', 'Oestradiol (E2)'),
('RDW-CV', 'Red Cell Distribution Width CV'),
('RDW-CV', 'Red Blood Cell Distribution Width CV'),
('RDW-CV', 'RBC Distribution Width CV'),
('RDW-CV', 'RDW Coefficient of Variation'),
('RDW-CV', 'Red Cell Distribution Width (CV)'),
('RDW-CV', 'Erythrocyte Distribution Width CV'),
('RDW-CV', 'RDW CV'),
('RDW-SD', 'RDW SD'),
('RDW-SD', 'Red Cell Distribution Width SD'),
('RDW-SD', 'Red Blood Cell Distribution Width SD'),
('RDW-SD', 'Erythrocyte Distribution Width SD'),
('RDW-SD', 'RDW Standard Deviation'),
('RDW-SD', 'RBC Distribution Width SD'),
('Serum Lead', 'S.Pb'),
('Serum Lead', 'Serum Pb'),
('Potassium (K⁺)', 'Serum K'),
('RDW-CV', 'RDW%'),
('Serum Barium', 'Se Barium')
) AS v(pname, alias)
JOIN public.traditional_health_parameters t ON t.name = v.pname
ON CONFLICT (alias) DO NOTHING;

INSERT INTO public.thp_age_range
  (thp_id, sex, age_min, age_max, min, low_danger, low_warn, ideal, high_warn, high_danger, max, source_note)
SELECT t.id, v.sex, v.age_min, v.age_max, v.gmin, v.ldanger, v.lwarn, v.ideal, v.hwarn, v.hdanger, v.gmax, v.note
FROM (VALUES
('A/G ratio', 'any', 0, 120, 0.0, 0.0, 0.9, 1.45, 2.0, 2.0, 2.0, 'import: All'),
('Absolute Basophil Count', 'any', 0, 120, 0.0, 0.0, 0.02, 0.060000000000000005, 0.1, 1.2, 1.2, 'import: All'),
('Absolute Eosinophil Count', 'any', 0, 120, 0.0, 0.0, 0.04, 0.27, 0.5, 6.0, 6.0, 'import: All'),
('Absolute Lymphocyte Count', 'any', 0, 120, 0.0, 0.0, 1.0, 2.0, 3.0, 23.9, 23.9, 'import: All'),
('Absolute Monocyte Count', 'any', 0, 120, 0.0, 0.0, 0.2, 0.6, 1.0, 3.58, 3.58, 'import: All'),
('Absolute Neutrophil Count', 'any', 0, 120, 0.0, 0.0, 2.0, 4.5, 7.0, 29.9, 29.9, 'import: All'),
('AFP', 'any', 0, 120, 0.0, 0.0, 0.0, 4.05, 8.1, 480.0, 480.0, 'import: All'),
('Albumin', 'any', 18, 59, 0.5, 0.5, 3.2, 4.0, 4.8, 7.5, 7.5, 'import: Adult All'),
('Albumin', 'any', 60, 120, 0.0, 0.0, 3.2, 3.9, 4.6, 7.5, 7.5, 'import: Older All'),
('Alkaline Phosphatase', 'any', 11, 18, 0.0, 0.0, 50.0, 220.0, 390.0, 716.0, 716.0, 'import: Adolescent All'),
('Alkaline Phosphatase', 'female', 18, 59, 0.0, 0.0, 35.0, 69.5, 104.0, 356.0, 356.0, 'import: Adult Female'),
('Alkaline Phosphatase', 'male', 18, 59, 0.0, 0.0, 45.0, 87.0, 129.0, 356.0, 356.0, 'import: Adult Male'),
('Alkaline Phosphatase', 'any', 4, 10, 0.0, 0.0, 145.0, 282.5, 420.0, 764.0, 764.0, 'import: Child All'),
('ALT(SGPT)', 'female', 18, 59, 0.0, 0.0, 7.0, 21.0, 35.0, 216.0, 216.0, 'import: Adult Female'),
('ALT(SGPT)', 'male', 18, 59, 0.0, 0.0, 7.0, 26.0, 45.0, 216.0, 216.0, 'import: Adult Male'),
('ALT(SGPT)', 'any', 4, 10, 0.0, 0.0, 7.0, 26.0, 45.0, 216.0, 216.0, 'import: Children All'),
('Amylase', 'any', 0, 120, 0.0, 0.0, 28.0, 64.0, 100.0, 717.0, 717.0, 'import: All'),
('Anti-TG', 'any', 0, 120, 0.0, 0.0, 0.0, 2.0, 4.0, 120.0, 120.0, 'import: All'),
('Anti-TPO', 'any', 0, 120, 0.0, 0.0, 0.0, 17.0, 34.0, 600.0, 600.0, 'import: All'),
('ApoB/ApoA1 Ratio', 'any', 0, 120, 0.0, 0.0, 0.4, 0.8300000000000001, 1.26, 3.56, 3.56, 'import: All'),
('Apolipoprotein A1', 'female', 18, 59, 10.0, 10.0, 108.0, 144.0, 180.0, 290.0, 290.0, 'import: Adult Female'),
('Apolipoprotein A1', 'male', 18, 59, 10.0, 10.0, 110.0, 135.0, 160.0, 290.0, 290.0, 'import: Adult Male'),
('Apolipoprotein B', 'any', 0, 120, 0.0, 0.0, 56.0, 100.5, 145.0, 354.0, 354.0, 'import: All'),
('APTT', 'any', 0, 120, 4.0, 4.0, 25.0, 30.0, 35.0, 81.0, 81.0, 'import: All'),
('AST(SGOT)', 'female', 18, 59, 0.0, 0.0, 10.0, 20.5, 31.0, 168.0, 168.0, 'import: Adult Female'),
('AST(SGOT)', 'male', 18, 59, 0.0, 0.0, 10.0, 22.5, 35.0, 168.0, 168.0, 'import: Adult Male'),
('AST(SGOT)', 'any', 4, 10, 0.0, 0.0, 10.0, 25.0, 40.0, 168.0, 168.0, 'import: Children All'),
('AST/ALT Ratio', 'any', 0, 120, 0.0, 0.0, 0.0, 1.0, 2.0, 6.0, 6.0, 'import: All'),
('Average Blood Glucose', 'any', 0, 120, 10.0, 10.0, 98.0, 109.0, 120.0, 290.0, 290.0, 'import: All'),
('Basophils', 'any', 0, 120, 0.0, 0.0, 0.0, 1.0, 2.0, 6.0, 6.0, 'import: All'),
('Bicarbonate ( HCO3⁻)', 'any', 18, 59, 4.0, 4.0, 22.0, 25.5, 29.0, 46.0, 46.0, 'import: Adult All'),
('Bicarbonate ( HCO3⁻)', 'any', 4, 10, 16.6, 16.6, 18.0, 21.5, 25.0, 26.4, 26.4, 'import: Children All'),
('Blood Urea', 'any', 0, 120, 0.0, 0.0, 12.0, 27.5, 43.0, 239.0, 239.0, 'import: All'),
('BNP', 'any', 18, 59, 0.0, 0.0, 0.0, 50.0, 100.0, 1200.0, 1200.0, 'import: Adult All'),
('BNP', 'any', 60, 120, 0.0, 0.0, 0.0, 62.5, 125.0, 1440.0, 1440.0, 'import: Older All'),
('BUN', 'any', 18, 59, 0.0, 0.0, 7.94, 14.005, 20.07, 71.2, 71.2, 'import: Adult All'),
('BUN', 'any', 4, 10, 0.0, 0.0, 5.0, 11.5, 18.0, 47.6, 47.6, 'import: Children All'),
('BUN', 'any', 60, 120, 0.0, 0.0, 8.0, 16.0, 24.0, 47.2, 47.2, 'import: Older All'),
('BUN/Creatinine Ratio', 'any', 0, 120, 0.0, 0.0, 9.0, 16.0, 23.0, 95.6, 95.6, 'import: All'),
('C-Peptide', 'any', 0, 120, 0.0, 0.0, 0.8, 1.9500000000000002, 3.1, 12.0, 12.0, 'import: All'),
('CA 15-3', 'any', 0, 120, 0.0, 0.0, 0.0, 15.65, 31.3, 120.0, 120.0, 'import: All'),
('CA 19-9', 'any', 0, 120, 0.0, 0.0, 0.0, 18.5, 37.0, 360.0, 360.0, 'import: All'),
('CA-125', 'female', 18, 59, 0.0, 0.0, 0.0, 17.5, 35.0, 240.0, 240.0, 'import: Adult Female'),
('CA-125', 'female', 60, 120, 0.0, 0.0, 0.0, 10.0, 20.0, 120.0, 120.0, 'import: Older Female'),
('Calcium', 'any', 18, 59, 6.1, 6.1, 8.6, 9.45, 10.3, 12.9, 12.9, 'import: Adult All'),
('Calcium', 'any', 4, 10, 5.2, 5.2, 8.8, 9.8, 10.8, 14.3, 14.3, 'import: Children All'),
('Calcium', 'any', 60, 120, 6.6, 6.6, 8.4, 9.3, 10.2, 12.4, 12.4, 'import: Older All'),
('Casts', 'any', 0, 120, 0.0, 0.0, 0.0, 0.0, 0.0, 5.0, 5.0, 'import: General'),
('CEA', 'any', 0, 120, 0.0, 0.0, 0.0, 2.5, 5.0, 50.0, 50.0, 'import: Non Smoker; smoker ideal 0.0-10.0'),
('Chloride (Cl⁻)', 'any', 0, 120, 78.0, 78.0, 98.0, 102.5, 107.0, 127.0, 127.0, 'import: All'),
('CHOL/HDL ratio', 'any', 0, 120, 0.0, 0.0, 3.0, 4.0, 5.0, 8.4, 8.4, 'import: All'),
('CK-MB', 'any', 0, 120, 0.0, 0.0, 0.0, 12.5, 25.0, 120.0, 120.0, 'import: All'),
('CPK (Total CK)', 'female', 18, 59, 0.0, 0.0, 26.0, 109.0, 192.0, 950.0, 950.0, 'import: Adult Female'),
('CPK (Total CK)', 'male', 18, 59, 0.0, 0.0, 39.0, 173.5, 308.0, 1200.0, 1200.0, 'import: Adult Male'),
('CRP', 'any', 0, 120, 0.0, 0.0, 0.0, 5.0, 10.0, 120.0, 120.0, 'import: All'),
('Cystatin C', 'any', 0, 120, 0.0, 0.0, 0.53, 0.77, 1.01, 6.0, 6.0, 'import: All'),
('DHEA-S', 'female', 18, 59, 0.0, 0.0, 26.0, 203.0, 380.0, 1000.0, 1000.0, 'import: Adult Female'),
('DHEA-S', 'male', 18, 59, 0.0, 0.0, 280.0, 460.0, 640.0, 1000.0, 1000.0, 'import: Adult Male'),
('DHEA-S', 'female', 60, 120, 0.0, 0.0, 13.0, 71.5, 130.0, 1000.0, 1000.0, 'import: Older Adults Female'),
('DHEA-S', 'male', 60, 120, 0.0, 0.0, 42.0, 166.0, 290.0, 1000.0, 1000.0, 'import: Older Adults Male'),
('Direct-Bilirubin', 'any', 0, 120, 0.0, 0.0, 0.0, 0.15, 0.3, 1.2, 1.2, 'import: All'),
('Eosinophils', 'any', 0, 120, 0.0, 0.0, 1.0, 3.5, 6.0, 18.0, 18.0, 'import: All'),
('Epithelial Cells', 'any', 0, 120, 0.0, 0.0, 0.0, 2.5, 5.0, 18.0, 18.0, 'import: All'),
('ESR', 'female', 18, 59, 0.0, 0.0, 0.0, 10.0, 20.0, 120.0, 120.0, 'import: Adult Female'),
('ESR', 'male', 18, 59, 0.0, 0.0, 0.0, 7.5, 15.0, 120.0, 120.0, 'import: Adult Male'),
('ESR', 'female', 60, 120, 0.0, 0.0, 0.0, 15.0, 30.0, 120.0, 120.0, 'import: Older Female'),
('ESR', 'male', 60, 120, 0.0, 0.0, 0.0, 10.0, 20.0, 120.0, 120.0, 'import: Older Male'),
('Estimated Average Glucose (eAG)', 'any', 0, 120, 0.0, 0.0, 90.0, 105.0, 120.0, 292.0, 292.0, 'import: All'),
('Estimated Glomerular Filtration Rate (eGFR)', 'any', 0, 120, 0.0, 0.0, 60.0, 75.0, 90.0, 216.0, 216.0, 'import: All'),
('Estrogen', 'male', 0, 120, 4.0, 4.0, 10.0, 25.0, 40.0, 46.0, 46.0, 'import: Male'),
('Fasting Blood Sugar', 'any', 0, 120, 40.0, 40.0, 70.0, 85.0, 100.0, 292.0, 292.0, 'import: All'),
('Follicle Stimulating Hormone (FSH)', 'any', 0, 120, 0.0, 0.0, 1.5, 6.95, 12.4, 240.0, 240.0, 'import: All'),
('Free T3', 'any', 11, 18, 0.6, 0.6, 2.5, 3.6, 4.7, 6.9, 6.9, 'import: Adolescent All'),
('Free T3', 'any', 18, 59, 0.6, 0.6, 2.3, 3.25, 4.2, 6.9, 6.9, 'import: Adult All'),
('Free T3', 'any', 4, 10, 0.6, 0.6, 2.8, 4.199999999999999, 5.6, 6.9, 6.9, 'import: Child All'),
('Free T4', 'any', 11, 18, 0.0, 0.0, 0.8, 1.4, 2.0, 2.34, 2.34, 'import: Adolescent All'),
('Free T4', 'any', 18, 59, 0.0, 0.0, 0.8, 1.3, 1.8, 2.34, 2.34, 'import: Adult All'),
('Free T4', 'any', 4, 10, 0.0, 0.0, 1.0, 1.55, 2.1, 2.34, 2.34, 'import: Child All'),
('GGT', 'female', 18, 59, 0.0, 0.0, 5.0, 21.5, 38.0, 143.0, 143.0, 'import: Adult Female'),
('GGT', 'male', 18, 59, 0.0, 0.0, 8.0, 31.5, 55.0, 179.0, 179.0, 'import: Adult Male'),
('Globulin', 'any', 0, 120, 1.44, 1.44, 2.0, 2.75, 3.5, 3.96, 3.96, 'import: All'),
('Glycated Hemoglobin (HbA1c)', 'any', 0, 120, 1.4, 1.4, 4.0, 4.85, 5.7, 16.1, 16.1, 'import: All'),
('Growth Hormone', 'female', 18, 59, 0.0, 0.0, 0.0, 8.55, 17.1, 30.0, 30.0, 'import: Adult Female'),
('Growth Hormone', 'male', 18, 59, 0.0, 0.0, 0.0, 0.485, 0.97, 3.6, 3.6, 'import: Adult Male'),
('Growth Hormone', 'any', 4, 10, 0.0, 0.0, 0.0, 4.0, 8.0, 18.0, 18.0, 'import: Children All'),
('HDL Cholesterol', 'female', 18, 59, 18.0, 18.0, 50.0, 60.0, 70.0, 102.0, 102.0, 'import: Adult Female'),
('HDL Cholesterol', 'male', 18, 59, 6.0, 6.0, 40.0, 50.0, 60.0, 104.0, 104.0, 'import: Adult Male'),
('HDL/LDL Ratio', 'any', 0, 120, 0.0, 0.0, 0.4, 499.7, 999.0, 1198.8, 1198.8, 'import: All'),
('Hematocrit', 'female', 11, 18, 12.0, 12.0, 35.0, 39.5, 44.0, 68.0, 68.0, 'import: Adolescent Female'),
('Hematocrit', 'male', 11, 18, 12.0, 12.0, 37.0, 42.5, 48.0, 68.0, 68.0, 'import: Adolescent Male'),
('Hematocrit', 'female', 18, 59, 12.0, 12.0, 36.0, 42.0, 48.0, 68.0, 68.0, 'import: Adult Female'),
('Hematocrit', 'male', 18, 59, 12.0, 12.0, 40.0, 46.0, 52.0, 68.0, 68.0, 'import: Adult Male'),
('Hematocrit', 'any', 4, 10, 12.0, 12.0, 35.0, 39.0, 43.0, 68.0, 68.0, 'import: Child All'),
('Hematocrit', 'any', 0, 1, 12.0, 12.0, 33.0, 38.0, 43.0, 68.0, 68.0, 'import: Infant All'),
('Hemoglobin', 'female', 11, 18, 4.4, 4.4, 11.5, 13.5, 15.5, 21.0, 21.0, 'import: Adolescent Female'),
('Hemoglobin', 'male', 11, 18, 3.4, 3.4, 12.0, 14.0, 16.0, 21.6, 21.6, 'import: Adolescent Male'),
('Hemoglobin', 'female', 18, 59, 4.8, 4.8, 12.0, 13.5, 15.0, 20.2, 20.2, 'import: Adult Female'),
('Hemoglobin', 'male', 18, 59, 4.4, 4.4, 13.5, 15.5, 17.5, 22.6, 22.6, 'import: Adult Male'),
('Hemoglobin', 'any', 4, 10, 3.6, 3.6, 11.5, 13.0, 14.5, 20.4, 20.4, 'import: Child All'),
('Hemoglobin', 'any', 0, 1, 2.6, 2.6, 11.0, 13.75, 16.5, 22.9, 22.9, 'import: Infant All'),
('Hemoglobin', 'female', 60, 120, 4.4, 4.4, 11.7, 13.9, 16.1, 22.6, 22.6, 'import: Older Female'),
('Hemoglobin', 'male', 60, 120, 4.4, 4.4, 12.6, 15.0, 17.4, 22.6, 22.6, 'import: Older Male'),
('HOMA-IR', 'any', 0, 120, 0.0, 0.0, 0.0, 1.25, 2.5, 18.0, 18.0, 'import: All'),
('Homocysteine', 'female', 18, 59, 0.0, 0.0, 5.0, 8.5, 12.0, 60.0, 60.0, 'import: Adult Female'),
('Homocysteine', 'male', 18, 59, 0.0, 0.0, 5.0, 10.0, 15.0, 60.0, 60.0, 'import: Adult Male'),
('Homocysteine', 'any', 4, 10, 0.0, 0.0, 3.0, 7.0, 11.0, 47.0, 47.0, 'import: Children All'),
('hs-Troponin I', 'female', 18, 59, 0.0, 0.0, 0.0, 8.0, 16.0, 83.6, 83.6, 'import: Adult Female'),
('hs-Troponin I', 'male', 18, 59, 0.0, 0.0, 0.0, 10.0, 20.0, 120.0, 120.0, 'import: Adult Male'),
('hs-Troponin T', 'any', 0, 120, 0.0, 0.0, 0.0, 9.5, 19.0, 120.0, 120.0, 'import: All'),
('IGF-1', 'female', 18, 59, 0.0, 0.0, 69.0, 219.0, 369.0, 600.0, 600.0, 'import: Adult Female'),
('IGF-1', 'male', 18, 59, 0.0, 0.0, 117.0, 223.0, 329.0, 600.0, 600.0, 'import: Adult Male'),
('IGF-1', 'female', 60, 120, 0.0, 0.0, 58.0, 118.0, 178.0, 600.0, 600.0, 'import: Older Adults Female'),
('IGF-1', 'male', 60, 120, 0.0, 0.0, 56.0, 116.5, 177.0, 600.0, 600.0, 'import: Older Adults Male'),
('Immature Granulocytes %', 'any', 0, 120, 0.0, 0.0, 0.0, 0.25, 0.5, 12.0, 12.0, 'import: All'),
('Indirect-Bilirubin', 'any', 0, 120, 0.0, 0.0, 0.0, 0.45, 0.9, 1.2, 1.2, 'import: All'),
('INR', 'any', 0, 120, 0.0, 0.0, 0.8, 1.0, 1.2, 7.1, 7.1, 'import: All'),
('Insulin', 'any', 0, 120, 0.0, 0.0, 2.6, 13.75, 24.9, 240.0, 240.0, 'import: All'),
('LDH', 'any', 18, 59, 0.0, 0.0, 140.0, 210.0, 280.0, 826.0, 826.0, 'import: Adult All'),
('LDH', 'any', 4, 10, 0.0, 0.0, 150.0, 250.0, 350.0, 704.0, 704.0, 'import: Children All'),
('LDL Cholesterol', 'any', 0, 120, 0.0, 0.0, 0.0, 50.0, 100.0, 228.0, 228.0, 'import: All'),
('LDL/HDL ratio', 'any', 0, 120, 0.0, 0.0, 1.5, 2.5, 3.5, 7.04, 7.04, 'import: All'),
('Lipase', 'any', 0, 120, 0.0, 0.0, 5.6, 28.45, 51.3, 171.2, 171.2, 'import: All'),
('Lipoprotein(a)', 'any', 0, 120, 0.0, 0.0, 0.0, 15.0, 30.0, 240.0, 240.0, 'import: All'),
('Lymphocytes', 'any', 18, 59, 0.0, 0.0, 20.0, 30.0, 40.0, 70.0, 70.0, 'import: Adult All'),
('Lymphocytes', 'any', 4, 10, 20.0, 20.0, 25.0, 37.5, 50.0, 70.0, 70.0, 'import: Child All'),
('Lymphocytes', 'any', 0, 1, 22.0, 22.0, 30.0, 50.0, 70.0, 78.0, 78.0, 'import: Infant All'),
('Magnesium', 'any', 18, 59, 0.0, 0.0, 1.7, 1.9500000000000002, 2.2, 4.6, 4.6, 'import: Adult All'),
('Magnesium', 'any', 4, 10, 0.0, 0.0, 1.7, 1.9, 2.1, 2.8, 2.8, 'import: Children All'),
('MCH', 'any', 18, 59, 23.0, 23.0, 27.0, 30.0, 33.0, 37.0, 37.0, 'import: Adult All'),
('MCH', 'any', 4, 10, 23.0, 23.0, 25.0, 30.0, 35.0, 37.0, 37.0, 'import: Children All'),
('MCH', 'any', 0, 1, 23.0, 23.0, 25.0, 30.0, 35.0, 37.0, 37.0, 'import: Infant All'),
('MCHC', 'any', 0, 120, 28.8, 28.8, 31.5, 33.6, 35.7, 37.2, 37.2, 'import: All'),
('MCV', 'any', 11, 18, 58.0, 58.0, 77.0, 86.0, 95.0, 142.0, 142.0, 'import: Adolescent All'),
('MCV', 'any', 18, 59, 58.0, 58.0, 80.0, 90.0, 100.0, 142.0, 142.0, 'import: Adult All'),
('MCV', 'any', 4, 10, 58.0, 58.0, 73.0, 81.0, 89.0, 142.0, 142.0, 'import: Child All'),
('MCV', 'any', 0, 1, 58.0, 58.0, 74.0, 91.0, 108.0, 142.0, 142.0, 'import: Infant All'),
('Monocytes', 'any', 0, 120, 0.0, 0.0, 2.0, 6.0, 10.0, 18.0, 18.0, 'import: All'),
('MPV', 'any', 0, 120, 0.8, 0.8, 6.5, 9.25, 12.0, 23.2, 23.2, 'import: All'),
('Neutrophils', 'any', 11, 18, 5.0, 5.0, 40.0, 57.5, 75.0, 100.0, 100.0, 'import: Adolescent All'),
('Neutrophils', 'any', 18, 59, 5.0, 5.0, 40.0, 60.0, 80.0, 100.0, 100.0, 'import: Adult All'),
('Neutrophils', 'any', 4, 10, 5.0, 5.0, 35.0, 50.0, 65.0, 100.0, 100.0, 'import: Child All'),
('Neutrophils', 'any', 0, 1, 5.0, 5.0, 20.0, 40.0, 60.0, 100.0, 100.0, 'import: Infant All'),
('Non HDL Cholesterol', 'any', 0, 120, 0.0, 0.0, 0.0, 65.0, 130.0, 228.0, 228.0, 'import: All'),
('NT-proBNP', 'any', 18, 59, 0.0, 0.0, 0.0, 62.5, 125.0, 1080.0, 1080.0, 'import: Adult All'),
('NT-proBNP', 'any', 60, 120, 0.0, 0.0, 0.0, 225.0, 450.0, 2160.0, 2160.0, 'import: Older All'),
('PDW', 'any', 0, 120, 1.0, 1.0, 9.0, 13.0, 17.0, 29.0, 29.0, 'import: All'),
('Phosphorus', 'any', 18, 59, 0.0, 0.0, 2.5, 3.5, 4.5, 11.8, 11.8, 'import: Adult All'),
('Phosphorus', 'any', 4, 10, 0.0, 0.0, 4.0, 5.5, 7.0, 11.8, 11.8, 'import: Children All'),
('Phosphorus', 'any', 60, 120, 0.0, 0.0, 2.5, 3.25, 4.0, 11.8, 11.8, 'import: Older All'),
('Platelet Count', 'any', 0, 120, 0.0, 0.0, 150000.0, 280000.0, 410000.0, 716000.0, 716000.0, 'import: All'),
('Plateletcrit', 'any', 0, 120, 0.0, 0.0, 0.19, 0.29000000000000004, 0.39, 1.18, 1.18, 'import: All'),
('Postprandial Blood Sugar', 'any', 0, 120, 22.0, 22.0, 100.0, 120.0, 140.0, 288.0, 288.0, 'import: All'),
('Potassium (K⁺)', 'any', 18, 59, 0.1, 0.1, 3.5, 4.3, 5.1, 9.9, 9.9, 'import: Adult All'),
('Potassium (K⁺)', 'any', 4, 10, 0.1, 0.1, 3.4, 4.35, 5.3, 9.9, 9.9, 'import: Children All'),
('Potassium (K⁺)', 'any', 60, 120, 0.1, 0.1, 3.5, 4.25, 5.0, 9.9, 9.9, 'import: Older All'),
('Progesterone', 'female', 0, 120, 0.0, 0.0, 0.1, 0.39999999999999997, 0.7, 0.82, 0.82, 'import: Female'),
('Progesterone', 'male', 0, 120, 0.0, 0.0, 0.2, 0.7999999999999999, 1.4, 1.64, 1.64, 'import: Male'),
('Prolactin', 'female', 18, 59, 0.0, 0.0, 2.0, 15.5, 29.0, 34.4, 34.4, 'import: Adult Female'),
('Prolactin', 'male', 18, 59, 0.0, 0.0, 2.0, 10.0, 18.0, 21.2, 21.2, 'import: Adult Male'),
('Prothrombin Time (PT)', 'any', 0, 120, 4.6, 4.6, 11.0, 12.5, 14.0, 28.4, 28.4, 'import: All'),
('Pus Cells', 'any', 0, 120, 0.0, 0.0, 0.0, 2.5, 5.0, 18.0, 18.0, 'import: All'),
('Random Blood Glucose', 'any', 0, 120, 40.0, 40.0, 70.0, 105.0, 140.0, 352.0, 352.0, 'import: All'),
('RBC', 'female', 11, 18, 2.38, 2.38, 3.9, 4.5, 5.1, 6.72, 6.72, 'import: Adolescent Female'),
('RBC', 'male', 11, 18, 2.38, 2.38, 4.2, 4.9, 5.6, 6.72, 6.72, 'import: Adolescent Male'),
('RBC', 'female', 18, 59, 2.38, 2.38, 4.0, 4.6, 5.2, 6.72, 6.72, 'import: Adult Female'),
('RBC', 'male', 18, 59, 2.38, 2.38, 4.5, 5.2, 5.9, 6.72, 6.72, 'import: Adult Male'),
('RBC', 'any', 4, 10, 2.38, 2.38, 4.2, 4.9, 5.6, 6.72, 6.72, 'import: Child All'),
('RBC', 'any', 0, 1, 2.38, 2.38, 3.4, 4.45, 5.5, 6.72, 6.72, 'import: Infant All'),
('RDW', 'any', 0, 120, 8.4, 8.4, 11.6, 12.8, 14.0, 19.6, 19.6, 'import: All'),
('RDW-CV', 'any', 0, 120, 8.4, 8.4, 11.5, 13.0, 14.5, 19.6, 19.6, 'import: All'),
('RDW-SD', 'any', 0, 120, 14.4, 14.4, 37.0, 42.0, 47.0, 88.6, 88.6, 'import: All'),
('Serum Aluminium', 'any', 0, 120, 0.0, 0.0, 0.0, 15.0, 30.0, 54.0, 54.0, 'import: All'),
('Serum Antimony', 'any', 0, 120, 0.0, 0.0, 0.1, 9.05, 18.0, 30.0, 30.0, 'import: All'),
('Serum Arsenic', 'any', 0, 120, 0.0, 0.0, 0.0, 2.5, 5.0, 12.0, 12.0, 'import: All'),
('Serum Barium', 'any', 0, 120, 0.0, 0.0, 0.0, 15.0, 30.0, 54.0, 54.0, 'import: All'),
('Serum Beryllium', 'any', 0, 120, 0.0, 0.0, 0.0, 0.05, 0.1, 12.0, 12.0, 'import: All'),
('Serum Bismuth', 'any', 0, 120, 0.0, 0.0, 0.1, 0.45, 0.8, 1.4, 1.4, 'import: All'),
('Serum Cadmium', 'any', 0, 120, 0.0, 0.0, 0.0, 0.5, 1.0, 10.0, 10.0, 'import: Non Smoker; smoker ideal 0.0-4.0'),
('Serum Caesium', 'any', 0, 120, 0.0, 0.0, 0.0, 2.5, 5.0, 9.6, 9.6, 'import: All'),
('Serum Chromium', 'any', 0, 120, 0.0, 0.0, 0.0, 15.0, 30.0, 54.0, 54.0, 'import: All'),
('Serum Cobalt', 'any', 0, 120, 0.0, 0.0, 0.1, 0.8, 1.5, 3.6, 3.6, 'import: All'),
('Serum Copper', 'female', 18, 59, 0.0, 0.0, 85.0, 120.0, 155.0, 292.0, 292.0, 'import: Adult Female'),
('Serum Copper', 'male', 18, 59, 0.0, 0.0, 70.0, 105.0, 140.0, 292.0, 292.0, 'import: Adult Male'),
('Serum Copper', 'any', 4, 10, 0.0, 0.0, 80.0, 120.0, 160.0, 292.0, 292.0, 'import: Children All'),
('Serum Creatinine', 'female', 11, 18, 0.0, 0.0, 0.5, 0.7, 0.9, 17.96, 17.96, 'import: Adolescent Female'),
('Serum Creatinine', 'male', 11, 18, 0.0, 0.0, 0.5, 0.75, 1.0, 17.96, 17.96, 'import: Adolescent Male'),
('Serum Creatinine', 'female', 18, 59, 0.0, 0.0, 0.59, 0.815, 1.04, 17.96, 17.96, 'import: Adult Female'),
('Serum Creatinine', 'male', 18, 59, 0.0, 0.0, 0.74, 1.045, 1.35, 17.96, 17.96, 'import: Adult Male'),
('Serum Creatinine', 'any', 4, 10, 0.0, 0.0, 0.2, 0.44999999999999996, 0.7, 17.96, 17.96, 'import: Child All'),
('Serum Ferritin', 'female', 11, 18, 0.0, 0.0, 6.0, 23.0, 40.0, 240.0, 240.0, 'import: Adolescent Female'),
('Serum Ferritin', 'male', 11, 18, 0.0, 0.0, 23.0, 46.5, 70.0, 478.0, 478.0, 'import: Adolescent Male'),
('Serum Ferritin', 'female', 18, 59, 0.0, 0.0, 11.0, 159.0, 307.0, 959.0, 959.0, 'import: Adult Female'),
('Serum Ferritin', 'male', 18, 59, 0.0, 0.0, 24.0, 180.0, 336.0, 1198.0, 1198.0, 'import: Adult Male'),
('Serum Ferritin', 'female', 60, 120, 0.0, 0.0, 13.0, 81.5, 150.0, 718.0, 718.0, 'import: Older Female'),
('Serum Iron', 'female', 18, 59, 0.0, 0.0, 50.0, 110.0, 170.0, 331.0, 331.0, 'import: Adult Female'),
('Serum Iron', 'male', 18, 59, 48.0, 48.0, 65.0, 120.0, 175.0, 354.0, 354.0, 'import: Adult Male'),
('Serum Iron', 'any', 4, 10, 0.0, 0.0, 50.0, 85.0, 120.0, 236.0, 236.0, 'import: Children All'),
('Serum Iron', 'any', 60, 120, 0.0, 0.0, 40.0, 95.0, 150.0, 296.0, 296.0, 'import: Older All'),
('Serum Lead', 'any', 18, 59, 0.0, 0.0, 0.0, 50.0, 100.0, 180.0, 180.0, 'import: Adult All'),
('Serum Lead', 'any', 4, 10, 0.0, 0.0, 0.0, 17.5, 35.0, 84.0, 84.0, 'import: Children All'),
('Serum Manganese', 'any', 0, 120, 0.0, 0.0, 7.1, 13.55, 20.0, 35.3, 35.3, 'import: All'),
('Serum Mercury', 'any', 0, 120, 0.0, 0.0, 0.0, 2.5, 5.0, 8.4, 8.4, 'import: All'),
('Serum Molybdenum', 'any', 0, 120, 0.04, 0.04, 0.7, 2.35, 4.0, 4.66, 4.66, 'import: All'),
('Serum Nickel', 'any', 0, 120, 0.0, 0.0, 0.0, 7.5, 15.0, 18.0, 18.0, 'import: All'),
('Serum Selenium', 'any', 0, 120, 0.0, 0.0, 60.0, 200.0, 340.0, 400.8, 400.8, 'import: All'),
('Serum Silver', 'any', 0, 120, 0.0, 0.0, 0.0, 2.0, 4.0, 4.8, 4.8, 'import: All'),
('Serum Strontium', 'any', 0, 120, 2.0, 2.0, 8.0, 23.0, 38.0, 44.0, 44.0, 'import: All'),
('Serum Thallium', 'any', 0, 120, 0.0, 0.0, 0.0, 0.5, 1.0, 1.2, 1.2, 'import: All'),
('Serum Tin', 'any', 0, 120, 0.0, 0.0, 0.0, 1.0, 2.0, 2.4, 2.4, 'import: All'),
('Serum Uranium', 'any', 0, 120, 0.0, 0.0, 0.0, 0.5, 1.0, 1.2, 1.2, 'import: All'),
('Serum Uric Acid', 'female', 18, 59, 1.9, 1.9, 2.6, 4.3, 6.0, 6.6, 6.6, 'import: Adult Female'),
('Serum Uric Acid', 'male', 18, 59, 2.7, 2.7, 3.5, 5.35, 7.2, 8.0, 8.0, 'import: Adult Male'),
('Serum Uric Acid', 'any', 4, 10, 0.0, 0.0, 2.0, 3.75, 5.5, 9.4, 9.4, 'import: Child All'),
('Serum Uric Acid', 'female', 60, 120, 1.8, 1.8, 2.6, 4.55, 6.5, 7.2, 7.2, 'import: Older Female'),
('Serum Uric Acid', 'male', 60, 120, 2.6, 2.6, 3.5, 5.75, 8.0, 8.9, 8.9, 'import: Older Male'),
('Serum Vanadium', 'any', 0, 120, 0.0, 0.0, 0.0, 0.4, 0.8, 12.0, 12.0, 'import: All'),
('Serum Zinc', 'female', 18, 59, 0.0, 0.0, 65.0, 87.5, 110.0, 354.0, 354.0, 'import: Adult Female'),
('Serum Zinc', 'male', 18, 59, 0.0, 0.0, 70.0, 95.0, 120.0, 354.0, 354.0, 'import: Adult Male'),
('Serum Zinc', 'any', 4, 10, 0.0, 0.0, 65.0, 85.0, 105.0, 354.0, 354.0, 'import: Children All'),
('Sodium ( Na+)', 'any', 0, 120, 134.0, 134.0, 136.0, 141.0, 146.0, 148.0, 148.0, 'import: All'),
('Testosterone', 'any', 4, 10, 0.0, 0.0, 2.0, 6.0, 10.0, 96.0, 96.0, 'import: Child All'),
('Thyroid Stimulating Hormone (TSH)', 'any', 11, 18, 0.0, 0.0, 0.5, 2.5, 4.5, 11.9, 11.9, 'import: Adolescent All'),
('Thyroid Stimulating Hormone (TSH)', 'any', 18, 59, 0.0, 0.0, 0.4, 2.2, 4.0, 11.9, 11.9, 'import: Adult All'),
('Thyroid Stimulating Hormone (TSH)', 'any', 4, 10, 0.0, 0.0, 0.6, 3.05, 5.5, 11.9, 11.9, 'import: Child All'),
('Thyroid Stimulating Hormone (TSH)', 'any', 60, 120, 0.0, 0.0, 0.4, 2.45, 4.5, 11.9, 11.9, 'import: Older All'),
('Thyroid Stimulating Hormone (TSH)', 'any', 1, 3, 0.0, 0.0, 0.7, 3.5500000000000003, 6.4, 11.9, 11.9, 'import: Toddler All'),
('TIBC (Total Iron Binding Capacity)', 'female', 18, 59, 50.0, 50.0, 250.0, 315.0, 380.0, 750.0, 750.0, 'import: Adult Female'),
('TIBC (Total Iron Binding Capacity)', 'male', 18, 59, 60.0, 60.0, 250.0, 310.0, 370.0, 690.0, 690.0, 'import: Adult Male'),
('TIBC (Total Iron Binding Capacity)', 'any', 4, 10, 40.0, 40.0, 250.0, 325.0, 400.0, 810.0, 810.0, 'import: Children All'),
('Total Bilirubin', 'any', 18, 59, 0.0, 0.0, 0.1, 0.65, 1.2, 3.6, 3.6, 'import: Adult All'),
('Total Cholesterol', 'any', 0, 120, 0.0, 0.0, 0.0, 100.0, 200.0, 288.0, 288.0, 'import: All'),
('Total Protein', 'any', 18, 59, 1.2, 1.2, 6.0, 7.15, 8.3, 13.8, 13.8, 'import: Adult All'),
('Total Protein', 'any', 4, 10, 1.2, 1.2, 5.6, 6.8, 8.0, 13.8, 13.8, 'import: Children All'),
('Total Protein', 'any', 60, 120, 1.2, 1.2, 5.7, 6.85, 8.0, 13.8, 13.8, 'import: Older All'),
('Total Thyroxine (T4)', 'any', 11, 18, 0.6, 0.6, 5.6, 9.05, 12.5, 23.6, 23.6, 'import: Adolescent All'),
('Total Thyroxine (T4)', 'any', 18, 59, 0.0, 0.0, 4.5, 8.25, 12.0, 23.6, 23.6, 'import: Adult All'),
('Total Thyroxine (T4)', 'any', 4, 10, 0.0, 0.0, 6.4, 9.850000000000001, 13.3, 23.6, 23.6, 'import: Child All'),
('Total Thyroxine (T4)', 'any', 60, 120, 0.0, 0.0, 4.0, 7.5, 11.0, 23.6, 23.6, 'import: Older All'),
('Total Thyroxine (T4)', 'any', 1, 3, 0.0, 0.0, 7.3, 11.15, 15.0, 23.6, 23.6, 'import: Toddler All'),
('Total Triiodothyronine (T3)', 'any', 11, 18, 0.0, 0.0, 83.0, 148.0, 213.0, 352.0, 352.0, 'import: Adolescent All'),
('Total Triiodothyronine (T3)', 'any', 18, 59, 0.0, 0.0, 80.0, 140.0, 200.0, 352.0, 352.0, 'import: Adult All'),
('Total Triiodothyronine (T3)', 'any', 4, 10, 0.0, 0.0, 94.0, 167.5, 241.0, 352.0, 352.0, 'import: Child All'),
('Total Triiodothyronine (T3)', 'any', 60, 120, 0.0, 0.0, 40.0, 110.0, 180.0, 352.0, 352.0, 'import: Older All'),
('Total Triiodothyronine (T3)', 'any', 1, 3, 0.0, 0.0, 105.0, 187.0, 269.0, 352.0, 352.0, 'import: Toddler All'),
('Transferrin Saturation (%)', 'female', 18, 59, 0.0, 0.0, 15.0, 32.5, 50.0, 94.4, 94.4, 'import: Adult Female'),
('Transferrin Saturation (%)', 'male', 18, 59, 0.0, 0.0, 20.0, 35.0, 50.0, 94.0, 94.0, 'import: Adult Male'),
('Transferrin Serum', 'any', 18, 59, 168.0, 168.0, 200.0, 280.0, 360.0, 392.0, 392.0, 'import: Adult All'),
('Transferrin Serum', 'any', 4, 10, 148.0, 148.0, 180.0, 260.0, 340.0, 372.0, 372.0, 'import: Children All'),
('Trig/HDL Ratio', 'any', 0, 120, 0.0, 0.0, 0.0, 1.56, 3.12, 17.9, 17.9, 'import: All'),
('Triglycerides', 'any', 0, 120, 0.0, 0.0, 0.0, 75.0, 150.0, 600.0, 600.0, 'import: All'),
('Troponin I', 'any', 0, 120, 0.0, 0.0, 0.0, 0.02, 0.04, 60.0, 60.0, 'import: All'),
('Troponin T', 'any', 0, 120, 0.0, 0.0, 0.0, 0.005, 0.01, 12.0, 12.0, 'import: All'),
('UIBC (Unsaturated Iron Binding Capacity)', 'female', 18, 59, 0.0, 0.0, 130.0, 252.5, 375.0, 764.0, 764.0, 'import: Adult Female'),
('UIBC (Unsaturated Iron Binding Capacity)', 'male', 18, 59, 0.0, 0.0, 162.0, 265.0, 368.0, 700.0, 700.0, 'import: Adult Male'),
('Urine pH', 'any', 0, 120, 3.6, 3.6, 5.0, 6.5, 8.0, 9.9, 9.9, 'import: All'),
('Urine Specific Gravity', 'any', 0, 120, 0.992, 0.992, 1.003, 1.0165, 1.03, 1.048, 1.048, 'import: All'),
('Urobilinogen', 'any', 0, 120, 0.0, 0.0, 0.0, 0.1, 0.2, 1.2, 1.2, 'import: All'),
('Vitamin A', 'any', 18, 59, 0.0, 0.0, 300.0, 550.0, 800.0, 2380.0, 2380.0, 'import: Adult All'),
('Vitamin A', 'any', 4, 10, 0.0, 0.0, 200.0, 450.0, 700.0, 2380.0, 2380.0, 'import: Children All'),
('Vitamin B1 (Thiamin)', 'any', 0, 120, 0.0, 0.0, 0.5, 2.25, 4.0, 6.0, 6.0, 'import: All'),
('Vitamin B12', 'any', 0, 120, 0.0, 0.0, 200.0, 550.0, 900.0, 1780.0, 1780.0, 'import: All'),
('Vitamin B2 (Riboflavin)', 'any', 0, 120, 0.0, 0.0, 1.6, 34.9, 68.2, 81.5, 81.5, 'import: All'),
('Vitamin B3 (Niacin)', 'any', 0, 120, 0.0, 0.0, 0.0, 2.5, 5.0, 6.0, 6.0, 'import: All'),
('Vitamin B5 (Pantothenic)', 'any', 0, 120, 0.0, 0.0, 11.0, 80.5, 150.0, 177.8, 177.8, 'import: All'),
('Vitamin B6 (P5P)', 'any', 0, 120, 0.0, 0.0, 5.0, 27.5, 50.0, 59.0, 59.0, 'import: All'),
('Vitamin B7 (Biotin)', 'any', 0, 120, 0.0, 0.0, 0.2, 1.6, 3.0, 3.5, 3.5, 'import: All'),
('Vitamin B9', 'any', 0, 120, 0.4, 0.4, 2.0, 2.5, 3.0, 4.6, 4.6, 'import: All'),
('Vitamin D', 'any', 0, 120, 0.0, 0.0, 30.0, 65.0, 100.0, 178.0, 178.0, 'import: All'),
('Vitamin E', 'any', 18, 59, 3000.0, 3000.0, 5500.0, 11750.0, 18000.0, 20500.0, 20500.0, 'import: Adult All'),
('Vitamin E', 'any', 4, 10, 600.0, 600.0, 3000.0, 9000.0, 15000.0, 17400.0, 17400.0, 'import: Children All'),
('Vitamin K', 'any', 0, 120, 0.0, 0.0, 0.13, 0.6599999999999999, 1.19, 2.25, 2.25, 'import: All'),
('VLDL Cholesterol', 'any', 0, 120, 0.0, 0.0, 5.0, 22.5, 40.0, 72.0, 72.0, 'import: All'),
('WBC', 'any', 11, 18, 0.0, 0.0, 4.5, 9.0, 13.5, 59.8, 59.8, 'import: Adolescent All'),
('WBC', 'any', 18, 59, 0.0, 0.0, 4.0, 7.5, 11.0, 59.8, 59.8, 'import: Adult All'),
('WBC', 'any', 4, 10, 0.0, 0.0, 5.0, 9.75, 14.5, 59.8, 59.8, 'import: Child All'),
('WBC', 'any', 0, 1, 0.0, 0.0, 6.0, 11.5, 17.0, 59.8, 59.8, 'import: Infant All'),
('WBC', 'any', 60, 120, 0.0, 0.0, 4.0, 7.5, 11.0, 59.8, 59.8, 'import: Older All')
) AS v(pname, sex, age_min, age_max, gmin, ldanger, lwarn, ideal, hwarn, hdanger, gmax, note)
JOIN public.traditional_health_parameters t ON t.name = v.pname
ON CONFLICT (thp_id, sex, age_min, age_max) DO NOTHING;

-- ============================================================
-- V19__drug_catalogue_merge.sql
-- ============================================================
-- V19__drug_catalogue_merge.sql
-- medicine_master absorbs the drug_reference catalogue (~250K products) and
-- becomes the single drug table for the app, the staff dashboard AND the AI
-- chat. drug_reference stays in place as the raw ingest target; this
-- migration copies its clinical columns onto medicine_master and imports
-- every product that is not already curated there.
--
-- Deliberately NOT copied: price_inr, pack_size (commercial, stale, merge
-- artifacts), substitutes (derivable: same composition_normalized = a
-- substitute — a static list would rot).
--
-- Idempotent throughout: columns IF NOT EXISTS, inserts guarded, so a rerun
-- is a no-op. On a database whose drug_reference is empty this only adds the
-- columns. Staff edits are never overwritten: the enrich step fills blanks
-- only, the import step skips any name a live curated row already owns.

-- =============================================================================
-- 1. Clinical columns from drug_reference
-- =============================================================================

ALTER TABLE public.medicine_master
    ADD COLUMN IF NOT EXISTS composition1           varchar(255),
    ADD COLUMN IF NOT EXISTS composition2           varchar(255),
    ADD COLUMN IF NOT EXISTS composition_normalized varchar(512),
    ADD COLUMN IF NOT EXISTS therapeutic_class      varchar(128),
    ADD COLUMN IF NOT EXISTS chemical_class         varchar(128),
    ADD COLUMN IF NOT EXISTS action_class           varchar(128),
    ADD COLUMN IF NOT EXISTS habit_forming          boolean,
    ADD COLUMN IF NOT EXISTS is_discontinued        boolean NOT NULL DEFAULT false;

-- Salt lookup ("what contains metformin") scans composition_normalized.
CREATE INDEX IF NOT EXISTS idx_medicine_master_composition
    ON public.medicine_master (composition_normalized);

-- The search trigger now also owns composition_normalized (lowercased,
-- punctuation collapsed — same shape ingest gave drug_reference) and folds
-- the salts into the search vector so "paracetamol" finds "Dolo 650".
CREATE OR REPLACE FUNCTION public.medicine_master_search()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.name_normalized := lower(regexp_replace(NEW.name, '[^a-zA-Z0-9]+', ' ', 'g'));
    NEW.composition_normalized := NULLIF(btrim(lower(regexp_replace(
        concat_ws(' ', NEW.composition1, NEW.composition2), '\s+', ' ', 'g'))), '');
    NEW.search_vector := to_tsvector('simple',
        coalesce(NEW.name, '') || ' ' ||
        coalesce(NEW.generic_name, '') || ' ' ||
        coalesce(NEW.name_normalized, '') || ' ' ||
        coalesce(NEW.composition_normalized, ''));
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_medicine_master_search ON public.medicine_master;
CREATE TRIGGER trg_medicine_master_search
    BEFORE INSERT OR UPDATE OF name, generic_name, composition1, composition2
    ON public.medicine_master
    FOR EACH ROW EXECUTE FUNCTION public.medicine_master_search();

-- =============================================================================
-- 2. Manufacturers referenced by the catalogue
-- =============================================================================

INSERT INTO public.medicine_manufacturer (name)
SELECT DISTINCT btrim(dr.manufacturer)
FROM public.drug_reference dr
WHERE dr.manufacturer IS NOT NULL AND btrim(dr.manufacturer) <> ''
ON CONFLICT (name) DO NOTHING;

-- =============================================================================
-- 3. Enrich + link EXISTING curated rows (fill blanks only, never overwrite)
-- =============================================================================

WITH ref AS (
    SELECT DISTINCT ON (norm) *
    FROM (
        SELECT dr.*,
               lower(regexp_replace(dr.name, '[^a-zA-Z0-9]+', ' ', 'g')) AS norm
        FROM public.drug_reference dr
    ) x
    ORDER BY norm, is_discontinued, length(name), lower(name)
)
UPDATE public.medicine_master m
SET drug_reference_id = COALESCE(m.drug_reference_id, ref.id),
    generic_name      = COALESCE(m.generic_name, left(concat_ws(' + ', ref.composition1, ref.composition2), 255)),
    composition1      = COALESCE(m.composition1, ref.composition1),
    composition2      = COALESCE(m.composition2, ref.composition2),
    therapeutic_class = COALESCE(m.therapeutic_class, ref.therapeutic_class),
    chemical_class    = COALESCE(m.chemical_class, ref.chemical_class),
    action_class      = COALESCE(m.action_class, ref.action_class),
    habit_forming     = COALESCE(m.habit_forming,
                                 CASE lower(ref.habit_forming) WHEN 'yes' THEN true WHEN 'no' THEN false END),
    is_discontinued   = m.is_discontinued OR ref.is_discontinued,
    side_effects      = COALESCE(m.side_effects,
                                 CASE WHEN jsonb_typeof(ref.side_effects) = 'array'
                                      THEN NULLIF(array_to_string(ARRAY(SELECT jsonb_array_elements_text(ref.side_effects)), ', '), '') END),
    used_for          = CASE WHEN m.used_for IS NULL OR cardinality(m.used_for) = 0
                             THEN CASE WHEN jsonb_typeof(ref.uses) = 'array'
                                       THEN NULLIF(ARRAY(SELECT jsonb_array_elements_text(ref.uses)), '{}'::text[]) END
                             ELSE m.used_for END,
    updated_at        = now()
FROM ref
WHERE m.name_normalized = ref.norm
  AND m.deleted_at IS NULL
  AND m.status NOT IN ('rejected', 'merged', 'archived')
  AND m.drug_reference_id IS DISTINCT FROM ref.id;   -- rerun no-op

-- =============================================================================
-- 4. Import every product the catalogue does not already have
-- =============================================================================

WITH ref AS (
    SELECT DISTINCT ON (norm) *
    FROM (
        SELECT dr.*,
               lower(regexp_replace(dr.name, '[^a-zA-Z0-9]+', ' ', 'g')) AS norm
        FROM public.drug_reference dr
        WHERE btrim(dr.name) <> ''
    ) x
    ORDER BY norm, is_discontinued, length(name), lower(name)
),
src AS (
    SELECT ref.*,
           CASE
               WHEN ref.name ~* '\mtablets?\M'                          THEN 'tablet'::dosage_form_enum
               WHEN ref.name ~* '\mcapsules?\M'                         THEN 'capsule'::dosage_form_enum
               WHEN ref.name ~* '\m(syrup|suspension|elixir)\M'         THEN 'syrup'::dosage_form_enum
               WHEN ref.name ~* '\minjections?\M'                       THEN 'injection'::dosage_form_enum
               WHEN ref.name ~* '\mdrops?\M'                            THEN 'drops'::dosage_form_enum
               WHEN ref.name ~* '\minhalers?\M'                         THEN 'inhaler'::dosage_form_enum
               WHEN ref.name ~* '\mpatch(es)?\M'                        THEN 'patch'::dosage_form_enum
               WHEN ref.name ~* '\m(cream|ointment)s?\M'                THEN 'cream'::dosage_form_enum
           END AS form,
           mf.id AS manufacturer_id
    FROM ref
    LEFT JOIN public.medicine_manufacturer mf ON mf.name = btrim(ref.manufacturer)
    WHERE NOT EXISTS (
        SELECT 1 FROM public.medicine_master mm
        WHERE mm.name_normalized = ref.norm
          AND mm.deleted_at IS NULL
          AND mm.status NOT IN ('rejected', 'merged', 'archived')
    )
)
INSERT INTO public.medicine_master
    (name, dosage_form, manufacturer, generic_name, composition1, composition2,
     therapeutic_class, chemical_class, action_class, habit_forming, is_discontinued,
     side_effects, used_for, icon_key, source, status, drug_reference_id)
SELECT src.name,
       src.form,
       src.manufacturer_id,
       left(concat_ws(' + ', src.composition1, src.composition2), 255),
       src.composition1,
       src.composition2,
       src.therapeutic_class,
       src.chemical_class,
       src.action_class,
       CASE lower(src.habit_forming) WHEN 'yes' THEN true WHEN 'no' THEN false END,
       src.is_discontinued,
       CASE WHEN jsonb_typeof(src.side_effects) = 'array'
            THEN NULLIF(array_to_string(ARRAY(SELECT jsonb_array_elements_text(src.side_effects)), ', '), '') END,
       CASE WHEN jsonb_typeof(src.uses) = 'array'
            THEN NULLIF(ARRAY(SELECT jsonb_array_elements_text(src.uses)), '{}'::text[]) END,
       CASE src.form WHEN 'injection' THEN 'syringe' WHEN 'capsule' THEN 'capsule' ELSE 'pill' END,
       'import',
       'approved',
       src.id
FROM src
ON CONFLICT DO NOTHING;

