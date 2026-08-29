DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'chat_app') THEN
    CREATE ROLE chat_app LOGIN PASSWORD 'chat-app-local';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'chat_worker') THEN
    CREATE ROLE chat_worker LOGIN PASSWORD 'chat-worker-local';
  END IF;
END
$$;

GRANT CONNECT ON DATABASE chat_v2 TO chat_app, chat_worker;
GRANT USAGE ON SCHEMA public TO chat_app, chat_worker;

ALTER DEFAULT PRIVILEGES FOR ROLE chat IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO chat_app, chat_worker;
ALTER DEFAULT PRIVILEGES FOR ROLE chat IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO chat_app, chat_worker;
