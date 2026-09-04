-- Tables that exist in Django/Go models but were missing from the frozen GORM dump.
-- Idempotent: safe to run on Neon or any future Postgres.

CREATE TABLE IF NOT EXISTS public.chatbot_flow_steps (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    created_at timestamp with time zone,
    updated_at timestamp with time zone,
    deleted_at timestamp with time zone,
    flow_id uuid NOT NULL,
    step_name character varying(100) NOT NULL,
    step_order bigint NOT NULL,
    message text NOT NULL,
    message_type character varying(20) DEFAULT 'text'::character varying,
    template_id uuid,
    api_config jsonb,
    buttons jsonb,
    transfer_config jsonb,
    input_type character varying(20),
    input_config jsonb,
    validation_regex character varying(255),
    validation_error text,
    store_as character varying(100),
    next_step character varying(100),
    conditional_next jsonb,
    skip_condition text,
    retry_on_invalid boolean DEFAULT true,
    max_retries bigint DEFAULT 3,
    CONSTRAINT chatbot_flow_steps_pkey PRIMARY KEY (id)
);

CREATE INDEX IF NOT EXISTS idx_chatbot_flow_steps_deleted_at ON public.chatbot_flow_steps USING btree (deleted_at);
CREATE INDEX IF NOT EXISTS idx_chatbot_flow_steps_flow_id ON public.chatbot_flow_steps USING btree (flow_id);
