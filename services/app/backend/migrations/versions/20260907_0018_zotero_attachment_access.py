"""Allow tenant-scoped Zotero attachment delivery through the API.

Revision ID: 20260907_0018
Revises: 20260906_0017
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260907_0018"
down_revision: str | None = "20260906_0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE zotero_import_sources ENABLE ROW LEVEL SECURITY;
        ALTER TABLE zotero_import_entries ENABLE ROW LEVEL SECURITY;
        ALTER TABLE zotero_collection_mappings ENABLE ROW LEVEL SECURITY;

        CREATE POLICY zotero_sources_select ON zotero_import_sources FOR SELECT
            USING (app_security.has_library_access(library_id));
        CREATE POLICY zotero_entries_select ON zotero_import_entries FOR SELECT
            USING (app_security.has_library_access(library_id));
        CREATE POLICY zotero_entries_update ON zotero_import_entries FOR UPDATE
            USING (app_security.can_edit_library(library_id))
            WITH CHECK (app_security.can_edit_library(library_id));
        CREATE POLICY zotero_collection_mappings_select ON zotero_collection_mappings FOR SELECT
            USING (app_security.has_library_access(library_id));
        """
    )
    op.execute(
        """
        DO $grant$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'literature_app') THEN
                GRANT SELECT ON zotero_import_sources, zotero_collection_mappings
                    TO literature_app;
                GRANT SELECT, UPDATE ON zotero_import_entries TO literature_app;
            END IF;
        END
        $grant$;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP POLICY IF EXISTS zotero_collection_mappings_select
            ON zotero_collection_mappings;
        DROP POLICY IF EXISTS zotero_entries_update ON zotero_import_entries;
        DROP POLICY IF EXISTS zotero_entries_select ON zotero_import_entries;
        DROP POLICY IF EXISTS zotero_sources_select ON zotero_import_sources;
        ALTER TABLE zotero_collection_mappings DISABLE ROW LEVEL SECURITY;
        ALTER TABLE zotero_import_entries DISABLE ROW LEVEL SECURITY;
        ALTER TABLE zotero_import_sources DISABLE ROW LEVEL SECURITY;
        REVOKE ALL ON zotero_import_sources, zotero_import_entries,
            zotero_collection_mappings FROM literature_app;
        """
    )
