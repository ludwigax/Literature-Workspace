import { useEffect, useState, type ReactNode } from "react";

import { currentSession, type SessionInfo } from "../../api";

type FeatureRoutePageProps = {
  title: string;
  adminOnly?: boolean;
  children: (session: SessionInfo) => ReactNode;
};

export function FeatureRoutePage({ title, adminOnly = false, children }: FeatureRoutePageProps) {
  const [session, setSession] = useState<SessionInfo | null | undefined>(undefined);
  const [error, setError] = useState("");

  useEffect(() => {
    void currentSession().then(setSession).catch((value: unknown) => {
      setError(value instanceof Error ? value.message : "Unable to load session");
    });
  }, []);

  if (error) return <RouteState title={title} detail={error} />;
  if (session === undefined) return <RouteState title={title} detail="Loading workspace…" busy />;
  if (session === null) {
    const returnPath = `${window.location.pathname}${window.location.search}`;
    return (
      <RouteState title={title} detail="Sign in to continue.">
        <a className="primary-button" href={`/api/v2/auth/login?return_path=${encodeURIComponent(returnPath)}`}>Sign in</a>
      </RouteState>
    );
  }
  if (adminOnly && session.principal.system_role !== "ADMIN") {
    return (
      <RouteState title="Access denied" detail="This workspace is available to system administrators only.">
        <a className="secondary-button route-link" href="/">Return to Library</a>
      </RouteState>
    );
  }
  return children(session);
}

function RouteState({ title, detail, busy = false, children }: { title: string; detail: string; busy?: boolean; children?: ReactNode }) {
  return (
    <main className="feature-route-state">
      <section>
        <span className="eyebrow">LITERATURE WORKSPACE</span>
        <h1>{title}</h1>
        <p>{detail}</p>
        {busy && <span className="spinner" />}
        {children}
      </section>
    </main>
  );
}
