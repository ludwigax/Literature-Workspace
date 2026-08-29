import { App } from "./App";
import { DocumentAdminPanel } from "./features/documents/DocumentAdminPanel";
import { FeatureRoutePage } from "./features/documents/FeatureRoutePage";
import { RetrievalPanel } from "./features/documents/RetrievalPanel";

export function RouteApp() {
  const path = window.location.pathname.replace(/\/+$/, "") || "/";
  if (path === "/documents/admin") {
    return <FeatureRoutePage title="Document administration" adminOnly>{() => <DocumentAdminPanel />}</FeatureRoutePage>;
  }
  if (path === "/retrieval") {
    return <FeatureRoutePage title="Evidence retrieval">{() => <RetrievalPanel />}</FeatureRoutePage>;
  }
  return <App />;
}
