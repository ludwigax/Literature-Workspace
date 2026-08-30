import { App } from "./App";
import { ChatPage } from "./features/chat/ChatPage";
import { DocumentAdminPanel } from "./features/documents/DocumentAdminPanel";
import { FeatureRoutePage } from "./features/documents/FeatureRoutePage";
import { RetrievalPanel } from "./features/documents/RetrievalPanel";

export function RouteApp() {
  const path = window.location.pathname.replace(/\/+$/, "") || "/";
  if (path === "/chat" || path.startsWith("/chat/sessions/")) {
    return <FeatureRoutePage title="Chat">{(session) => <ChatPage session={session} />}</FeatureRoutePage>;
  }
  if (path === "/documents/admin") {
    return <FeatureRoutePage title="Document administration" adminOnly>{() => <DocumentAdminPanel />}</FeatureRoutePage>;
  }
  if (path === "/retrieval") {
    return <FeatureRoutePage title="Evidence retrieval">{() => <RetrievalPanel />}</FeatureRoutePage>;
  }
  return <App />;
}
