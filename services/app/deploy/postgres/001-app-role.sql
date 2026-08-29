DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'literature_app') THEN
        CREATE ROLE literature_app LOGIN PASSWORD 'literature-app-local';
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'literature_worker') THEN
        CREATE ROLE literature_worker LOGIN PASSWORD 'literature-worker-local' BYPASSRLS;
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'chat_worker') THEN
        CREATE ROLE chat_worker LOGIN PASSWORD 'chat-worker-local' BYPASSRLS;
    END IF;
END
$$;

GRANT USAGE ON SCHEMA public TO literature_app;
GRANT USAGE ON SCHEMA public TO literature_worker;
GRANT USAGE ON SCHEMA public TO chat_worker;
