"""Add restricted application grants and Library RLS policies.

Revision ID: 20260825_0005
Revises: 20260825_0004
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260825_0005"
down_revision: str | None = "20260825_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE SCHEMA IF NOT EXISTS app_security;

        CREATE OR REPLACE FUNCTION app_security.principal_id()
        RETURNS uuid
        LANGUAGE sql
        STABLE
        AS $function$
            SELECT CASE
                WHEN current_setting('app.principal_id', true) IS NULL
                  OR current_setting('app.principal_id', true) = ''
                THEN NULL
                ELSE current_setting('app.principal_id', true)::uuid
            END
        $function$;

        CREATE OR REPLACE FUNCTION app_security.has_library_access(target_library_id uuid)
        RETURNS boolean
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $function$
            SELECT EXISTS (
                SELECT 1
                FROM public.library_memberships AS membership
                WHERE membership.library_id = target_library_id
                  AND membership.principal_id = app_security.principal_id()
                  AND membership.status = 'ACTIVE'
            )
        $function$;

        CREATE OR REPLACE FUNCTION app_security.is_library_owner(target_library_id uuid)
        RETURNS boolean
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $function$
            SELECT EXISTS (
                SELECT 1
                FROM public.library_memberships AS membership
                WHERE membership.library_id = target_library_id
                  AND membership.principal_id = app_security.principal_id()
                  AND membership.role = 'OWNER'
                  AND membership.status = 'ACTIVE'
            )
        $function$;

        CREATE OR REPLACE FUNCTION app_security.can_edit_library(target_library_id uuid)
        RETURNS boolean
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $function$
            SELECT EXISTS (
                SELECT 1
                FROM public.library_memberships AS membership
                WHERE membership.library_id = target_library_id
                  AND membership.principal_id = app_security.principal_id()
                  AND membership.role IN ('OWNER', 'EDITOR')
                  AND membership.status = 'ACTIVE'
            )
        $function$;

        CREATE OR REPLACE FUNCTION app_security.is_library_bootstrap(
            target_library_id uuid,
            target_owner_id uuid
        )
        RETURNS boolean
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $function$
            SELECT target_owner_id = app_security.principal_id()
               AND NOT EXISTS (
                    SELECT 1
                    FROM public.library_memberships AS membership
                    WHERE membership.library_id = target_library_id
               )
        $function$;

        CREATE OR REPLACE FUNCTION app_security.can_accept_invitation(target_library_id uuid)
        RETURNS boolean
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $function$
            SELECT EXISTS (
                SELECT 1
                FROM public.library_invitations AS invitation
                WHERE invitation.invitation_id = CASE
                    WHEN current_setting('app.invitation_id', true) IS NULL
                      OR current_setting('app.invitation_id', true) = ''
                    THEN NULL
                    ELSE current_setting('app.invitation_id', true)::uuid
                END
                  AND invitation.library_id = target_library_id
                  AND invitation.accepted_by = app_security.principal_id()
                  AND invitation.status = 'ACCEPTED'
            )
        $function$;

        ALTER TABLE libraries ENABLE ROW LEVEL SECURITY;
        ALTER TABLE library_memberships ENABLE ROW LEVEL SECURITY;
        ALTER TABLE library_invitations ENABLE ROW LEVEL SECURITY;

        CREATE POLICY libraries_select ON libraries
            FOR SELECT
            USING (
                app_security.has_library_access(library_id)
                OR app_security.is_library_bootstrap(library_id, owner_principal_id)
                OR app_security.can_accept_invitation(library_id)
            );
        CREATE POLICY libraries_insert ON libraries
            FOR INSERT
            WITH CHECK (
                app_security.principal_id() IS NOT NULL
                AND owner_principal_id = app_security.principal_id()
            );
        CREATE POLICY libraries_update ON libraries
            FOR UPDATE
            USING (
                app_security.can_edit_library(library_id)
                OR app_security.is_library_bootstrap(library_id, owner_principal_id)
                OR app_security.can_accept_invitation(library_id)
            )
            WITH CHECK (
                app_security.can_edit_library(library_id)
                OR app_security.is_library_bootstrap(library_id, owner_principal_id)
                OR app_security.can_accept_invitation(library_id)
            );
        CREATE POLICY libraries_delete ON libraries
            FOR DELETE
            USING (app_security.is_library_owner(library_id));

        CREATE POLICY memberships_select ON library_memberships
            FOR SELECT
            USING (
                app_security.has_library_access(library_id)
                OR principal_id = app_security.principal_id()
            );
        CREATE POLICY memberships_insert ON library_memberships
            FOR INSERT
            WITH CHECK (
                app_security.is_library_owner(library_id)
                OR (
                    principal_id = app_security.principal_id()
                    AND role = 'OWNER'
                    AND EXISTS (
                        SELECT 1 FROM libraries
                        WHERE libraries.library_id = library_memberships.library_id
                          AND app_security.is_library_bootstrap(
                              libraries.library_id,
                              libraries.owner_principal_id
                          )
                    )
                )
                OR (
                    principal_id = app_security.principal_id()
                    AND app_security.can_accept_invitation(library_id)
                )
            );
        CREATE POLICY memberships_update ON library_memberships
            FOR UPDATE
            USING (
                app_security.is_library_owner(library_id)
                OR (
                    principal_id = app_security.principal_id()
                    AND app_security.can_accept_invitation(library_id)
                )
            )
            WITH CHECK (
                app_security.is_library_owner(library_id)
                OR (
                    principal_id = app_security.principal_id()
                    AND app_security.can_accept_invitation(library_id)
                )
            );
        CREATE POLICY memberships_delete ON library_memberships
            FOR DELETE
            USING (app_security.is_library_owner(library_id));

        CREATE POLICY invitations_select ON library_invitations
            FOR SELECT
            USING (
                app_security.is_library_owner(library_id)
                OR EXISTS (
                    SELECT 1
                    FROM external_identities AS identity
                    WHERE identity.principal_id = app_security.principal_id()
                      AND lower(identity.email) = email_normalized
                )
            );
        CREATE POLICY invitations_insert ON library_invitations
            FOR INSERT
            WITH CHECK (
                app_security.is_library_owner(library_id)
                AND invited_by = app_security.principal_id()
            );
        CREATE POLICY invitations_update ON library_invitations
            FOR UPDATE
            USING (
                app_security.is_library_owner(library_id)
                OR EXISTS (
                    SELECT 1
                    FROM external_identities AS identity
                    WHERE identity.principal_id = app_security.principal_id()
                      AND lower(identity.email) = email_normalized
                )
            )
            WITH CHECK (
                app_security.is_library_owner(library_id)
                OR EXISTS (
                    SELECT 1
                    FROM external_identities AS identity
                    WHERE identity.principal_id = app_security.principal_id()
                      AND lower(identity.email) = email_normalized
                )
            );
        CREATE POLICY invitations_delete ON library_invitations
            FOR DELETE
            USING (app_security.is_library_owner(library_id));
        """
    )
    op.execute(
        """
        DO $grant$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'literature_app') THEN
                GRANT USAGE ON SCHEMA public, app_security TO literature_app;
                GRANT SELECT, INSERT, UPDATE ON
                    principals,
                    external_identities,
                    web_sessions,
                    oidc_login_attempts,
                    libraries,
                    library_memberships,
                    library_invitations
                TO literature_app;
                GRANT SELECT, INSERT ON audit_events TO literature_app;
                GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA app_security TO literature_app;
            END IF;
        END
        $grant$;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP POLICY IF EXISTS invitations_delete ON library_invitations;
        DROP POLICY IF EXISTS invitations_update ON library_invitations;
        DROP POLICY IF EXISTS invitations_insert ON library_invitations;
        DROP POLICY IF EXISTS invitations_select ON library_invitations;
        DROP POLICY IF EXISTS memberships_delete ON library_memberships;
        DROP POLICY IF EXISTS memberships_update ON library_memberships;
        DROP POLICY IF EXISTS memberships_insert ON library_memberships;
        DROP POLICY IF EXISTS memberships_select ON library_memberships;
        DROP POLICY IF EXISTS libraries_delete ON libraries;
        DROP POLICY IF EXISTS libraries_update ON libraries;
        DROP POLICY IF EXISTS libraries_insert ON libraries;
        DROP POLICY IF EXISTS libraries_select ON libraries;
        ALTER TABLE library_invitations DISABLE ROW LEVEL SECURITY;
        ALTER TABLE library_memberships DISABLE ROW LEVEL SECURITY;
        ALTER TABLE libraries DISABLE ROW LEVEL SECURITY;
        DROP SCHEMA IF EXISTS app_security CASCADE;
        """
    )
