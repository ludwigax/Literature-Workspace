import { ChangeEvent, FormEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  api,
  currentSession,
  type AdvancedMetadataSearch,
  type ArtifactResource,
  type AssetResource,
  type CatalogueItem,
  type Collection,
  type Invitation,
  type ItemResources,
  type Library,
  type LibraryTag,
  type Member,
  type SessionInfo,
  type ZoteroAttachment,
} from "./api";
import { displayedDoi, MetadataFields, metadataFromForm } from "./MetadataFields";

type LoadState = "loading" | "anonymous" | "authenticated" | "error";
type CatalogueStatus = "ACTIVE" | "TRASHED";
type WorkspacePage = "library" | "overview";
type BulkAction =
  | "ADD_COLLECTION"
  | "REMOVE_COLLECTION"
  | "ADD_TAG"
  | "REMOVE_TAG"
  | "TRASH"
  | "RESTORE";
type PdfImportPhase =
  | "UPLOADING"
  | "QUEUED"
  | "EXTRACTING"
  | "MATCHING"
  | "RESOLVING_METADATA"
  | "READY"
  | "NEEDS_REVIEW"
  | "FAILED";
type PdfImportView = {
  clientId: string;
  filename: string;
  jobId?: string;
  itemId?: string;
  phase: PdfImportPhase;
  progress: number;
  detail: string;
};
type ZoteroImportView = {
  filename: string;
  phase: "UPLOADING" | "RUNNING" | "READY" | "FAILED";
  current: number;
  total: number;
  detail: string;
};
type ResourceReader = {
  title: string;
  url: string;
  mediaType: string;
};
type ActivityStatus = "UPLOADING" | "PENDING" | "RUNNING" | "SUCCEEDED" | "FAILED" | "CANCELLED" | "NEEDS_REVIEW";
type JobActivity = {
  activityId: string;
  label: string;
  kind: "PDF" | "ZOTERO" | "CITATIONS" | "METADATA";
  status: ActivityStatus;
  current: number;
  total: number;
  detail: string;
};

const DEFAULT_ADVANCED_SEARCH: AdvancedMetadataSearch = {
  tagMode: "ANY",
  sort: "ADDED",
  direction: "DESC",
};

function advancedSearchFromForm(form: HTMLFormElement): AdvancedMetadataSearch {
  const data = new FormData(form);
  const textValue = (name: string) => String(data.get(name) ?? "").trim() || undefined;
  const numberValue = (name: string) => {
    const value = textValue(name);
    return value ? Number(value) : undefined;
  };
  const booleanValue = (name: string) => {
    const value = textValue(name);
    return value === "true" ? true : value === "false" ? false : undefined;
  };
  return {
    title: textValue("title"),
    author: textValue("author"),
    identifier: textValue("identifier"),
    venue: textValue("venue"),
    yearFrom: numberValue("yearFrom"),
    yearTo: numberValue("yearTo"),
    workTypes: (textValue("workTypes") ?? "").split(",").map((value) => value.trim()).filter(Boolean),
    metadataSources: data.getAll("metadataSources").map(String),
    collectionIds: data.getAll("collectionIds").map(String),
    tagIds: data.getAll("tagIds").map(String),
    tagMode: String(data.get("tagMode") ?? "ANY") as "ANY" | "ALL",
    includeSubcollections: data.get("includeSubcollections") === "on",
    hasPdf: booleanValue("hasPdf"),
    hasDocument: booleanValue("hasDocument"),
    hasAsset: booleanValue("hasAsset"),
    addedFrom: textValue("addedFrom"),
    addedTo: textValue("addedTo"),
    modifiedFrom: textValue("modifiedFrom"),
    modifiedTo: textValue("modifiedTo"),
    sort: String(data.get("sort") ?? "ADDED") as AdvancedMetadataSearch["sort"],
    direction: String(data.get("direction") ?? "DESC") as AdvancedMetadataSearch["direction"],
  };
}

function JobActivityPanel({ activities, onClear }: { activities: JobActivity[]; onClear: () => void }) {
  if (activities.length === 0) return null;
  const terminal = (status: ActivityStatus) => ["SUCCEEDED", "FAILED", "CANCELLED", "NEEDS_REVIEW"].includes(status);
  return (
    <section className="job-activity-panel" aria-live="polite">
      <header>
        <div><strong>Background activity</strong><span>{activities.filter((value) => !terminal(value.status)).length} active · {activities.length} total</span></div>
        {activities.every((value) => terminal(value.status)) && <button onClick={onClear}>Clear</button>}
      </header>
      <div className="job-activity-list">
        {activities.map((activity) => (
          <div className="job-activity-row" key={activity.activityId}>
            <small>{activity.kind}</small>
            <div><strong>{activity.label}</strong><span>{activity.detail}</span></div>
            <span className={`job-status ${activity.status.toLowerCase()}`}>{activity.status.replaceAll("_", " ")}</span>
            <progress max={Math.max(1, activity.total)} value={activity.current} />
          </div>
        ))}
      </div>
    </section>
  );
}

type DirectoryPickerWindow = Window & {
  showDirectoryPicker?: (options?: {
    id?: string;
    mode?: "read" | "readwrite";
    startIn?: "desktop" | "documents" | "downloads" | "music" | "pictures" | "videos";
  }) => Promise<FileSystemDirectoryHandle>;
};

async function fileFromDirectory(
  root: FileSystemDirectoryHandle,
  relativePath: string,
): Promise<File | null> {
  const parts = relativePath.replaceAll("\\", "/").split("/").filter(Boolean);
  if (parts.length === 0) return null;
  try {
    let directory = root;
    for (const part of parts.slice(0, -1)) {
      directory = await directory.getDirectoryHandle(part);
    }
    return await (await directory.getFileHandle(parts.at(-1)!)).getFile();
  } catch {
    return null;
  }
}

function currentWorkspaceRoute(): { page: WorkspacePage; libraryId: string } {
  const match = window.location.pathname.match(/^\/libraries\/([^/]+)(\/overview)?\/?$/);
  return {
    page: match?.[2] ? "overview" : "library",
    libraryId: match?.[1] ? decodeURIComponent(match[1]) : "",
  };
}

function collectionDepth(collection: Collection, values: Collection[]): number {
  let depth = 0;
  let parentId = collection.parent_collection_id;
  const visited = new Set<string>();
  while (parentId && !visited.has(parentId)) {
    visited.add(parentId);
    depth += 1;
    parentId = values.find((value) => value.collection_id === parentId)?.parent_collection_id ?? null;
  }
  return depth;
}

export function App() {
  const [state, setState] = useState<LoadState>("loading");
  const [session, setSession] = useState<SessionInfo | null>(null);
  const [libraries, setLibraries] = useState<Library[]>([]);
  const [selectedId, setSelectedId] = useState<string>("");
  const [members, setMembers] = useState<Member[]>([]);
  const [invitations, setInvitations] = useState<Invitation[]>([]);
  const [collections, setCollections] = useState<Collection[]>([]);
  const [tags, setTags] = useState<LibraryTag[]>([]);
  const [items, setItems] = useState<CatalogueItem[]>([]);
  const [activeCollectionId, setActiveCollectionId] = useState("");
  const [activeTagId, setActiveTagId] = useState("");
  const [catalogueStatus, setCatalogueStatus] = useState<CatalogueStatus>("ACTIVE");
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const loadingMoreRef = useRef(false);
  const [selectedItemIds, setSelectedItemIds] = useState<string[]>([]);
  const [bulkAction, setBulkAction] = useState<BulkAction>("ADD_COLLECTION");
  const [bulkTargetId, setBulkTargetId] = useState("");
  const [addPaperOpen, setAddPaperOpen] = useState(false);
  const [selectedItem, setSelectedItem] = useState<CatalogueItem | null>(null);
  const [inspectedItemId, setInspectedItemId] = useState("");
  const [manageCollectionOpen, setManageCollectionOpen] = useState(false);
  const [invitationLink, setInvitationLink] = useState<string>("");
  const [message, setMessage] = useState<string>("");
  const [busy, setBusy] = useState(false);
  const [pdfImports, setPdfImports] = useState<PdfImportView[]>([]);
  const [zoteroImport, setZoteroImport] = useState<ZoteroImportView | null>(null);
  const [jobActivities, setJobActivities] = useState<JobActivity[]>([]);
  const [workspacePage, setWorkspacePage] = useState<WorkspacePage>(
    () => currentWorkspaceRoute().page,
  );
  const [libraryQuery, setLibraryQuery] = useState("");
  const [metadataQuery, setMetadataQuery] = useState("");
  const metadataQueryRef = useRef("");
  const [advancedSearchOpen, setAdvancedSearchOpen] = useState(false);
  const [advancedSearch, setAdvancedSearch] = useState<AdvancedMetadataSearch>(DEFAULT_ADVANCED_SEARCH);
  const advancedSearchRef = useRef<AdvancedMetadataSearch>(DEFAULT_ADVANCED_SEARCH);
  const [advancedFormKey, setAdvancedFormKey] = useState(0);
  const catalogueRequestRef = useRef(0);
  const [expandedItemIds, setExpandedItemIds] = useState<string[]>([]);
  const [expandedDocumentIds, setExpandedDocumentIds] = useState<string[]>([]);
  const [resourcesByItem, setResourcesByItem] = useState<Record<string, ItemResources>>({});
  const [resourceLoadingIds, setResourceLoadingIds] = useState<string[]>([]);
  const [resourceReader, setResourceReader] = useState<ResourceReader | null>(null);

  const selected = useMemo(
    () => libraries.find((library) => library.library_id === selectedId) ?? null,
    [libraries, selectedId],
  );
  const activeCollection = useMemo(
    () =>
      collections.find((collection) => collection.collection_id === activeCollectionId) ?? null,
    [activeCollectionId, collections],
  );
  const orderedCollections = useMemo(() => {
    const children = new Map<string, Collection[]>();
    for (const collection of collections) {
      const parent = collection.parent_collection_id ?? "";
      children.set(parent, [...(children.get(parent) ?? []), collection]);
    }
    const result: Collection[] = [];
    const visited = new Set<string>();
    const visit = (parent: string) => {
      for (const collection of children.get(parent) ?? []) {
        if (visited.has(collection.collection_id)) continue;
        visited.add(collection.collection_id);
        result.push(collection);
        visit(collection.collection_id);
      }
    };
    visit("");
    for (const collection of collections) {
      if (!visited.has(collection.collection_id)) result.push(collection);
    }
    return result;
  }, [collections]);
  const visibleLibraryItems = items;
  const advancedFilterCount = Object.entries(advancedSearch).filter(([key, value]) => {
    if (key === "sort") return value !== "ADDED";
    if (key === "direction") return value !== "DESC";
    if (key === "tagMode") return value !== "ANY";
    return Array.isArray(value) ? value.length > 0 : value !== undefined && value !== false;
  }).length;
  const inspectedItem = useMemo(
    () => items.find((item) => item.library_item_id === inspectedItemId) ?? null,
    [inspectedItemId, items],
  );
  const inspectedResources = inspectedItemId ? resourcesByItem[inspectedItemId] : undefined;
  const activities = useMemo<JobActivity[]>(() => {
    const pdfActivities: JobActivity[] = pdfImports.map((entry) => ({
      activityId: entry.clientId,
      label: entry.filename,
      kind: "PDF",
      status: entry.phase === "READY" ? "SUCCEEDED" : entry.phase === "NEEDS_REVIEW" ? "NEEDS_REVIEW" : entry.phase === "FAILED" ? "FAILED" : entry.phase === "UPLOADING" ? "UPLOADING" : "RUNNING",
      current: entry.progress,
      total: 100,
      detail: entry.detail,
    }));
    const zoteroActivities: JobActivity[] = zoteroImport ? [{
      activityId: `zotero:${zoteroImport.filename}`,
      label: zoteroImport.filename,
      kind: "ZOTERO",
      status: zoteroImport.phase === "READY" ? "SUCCEEDED" : zoteroImport.phase,
      current: zoteroImport.current,
      total: zoteroImport.total,
      detail: zoteroImport.detail,
    }] : [];
    return [...pdfActivities, ...zoteroActivities, ...jobActivities];
  }, [jobActivities, pdfImports, zoteroImport]);

  function upsertJobActivity(activity: JobActivity) {
    setJobActivities((current) => [
      ...current.filter((value) => value.activityId !== activity.activityId),
      activity,
    ]);
  }

  function clearFinishedActivities() {
    setPdfImports((current) => current.filter((value) => !["READY", "NEEDS_REVIEW", "FAILED"].includes(value.phase)));
    setZoteroImport((current) => current && ["READY", "FAILED"].includes(current.phase) ? null : current);
    setJobActivities((current) => current.filter((value) => !["SUCCEEDED", "FAILED", "CANCELLED", "NEEDS_REVIEW"].includes(value.status)));
  }

  function applyAdvancedSearch(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const value = advancedSearchFromForm(event.currentTarget);
    advancedSearchRef.current = value;
    setAdvancedSearch(value);
    setSelectedItemIds([]);
  }

  function clearAdvancedSearch() {
    advancedSearchRef.current = DEFAULT_ADVANCED_SEARCH;
    setAdvancedSearch(DEFAULT_ADVANCED_SEARCH);
    setAdvancedFormKey((value) => value + 1);
    setSelectedItemIds([]);
  }

  useEffect(() => {
    setSelectedItemIds([]);
  }, [libraryQuery]);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      const value = libraryQuery.trim();
      metadataQueryRef.current = value;
      setMetadataQuery(value);
    }, 250);
    return () => window.clearTimeout(timer);
  }, [libraryQuery]);

  const loadLibraries = useCallback(async () => {
    const value = await api.libraries();
    setLibraries(value.libraries);
    setSelectedId((current) => {
      const routed = currentWorkspaceRoute().libraryId;
      if (value.libraries.some((library) => library.library_id === routed)) return routed;
      return value.libraries.some((library) => library.library_id === current)
        ? current
        : (value.libraries[0]?.library_id ?? "");
    });
  }, []);

  const navigateWorkspace = useCallback((page: WorkspacePage, libraryId: string) => {
    setWorkspacePage(page);
    const suffix = page === "overview" ? "/overview" : "";
    window.history.pushState({}, "", `/libraries/${encodeURIComponent(libraryId)}${suffix}`);
  }, []);

  useEffect(() => {
    void (async () => {
      try {
        const current = await currentSession();
        if (!current) {
          setState("anonymous");
          return;
        }
        setSession(current);
        await loadLibraries();
        setState("authenticated");
      } catch (error) {
        setMessage(error instanceof Error ? error.message : "Unable to start the application");
        setState("error");
      }
    })();
  }, [loadLibraries]);

  useEffect(() => {
    const onPopState = () => {
      const route = currentWorkspaceRoute();
      setWorkspacePage(route.page);
      if (route.libraryId && libraries.some((value) => value.library_id === route.libraryId)) {
        setSelectedId(route.libraryId);
      }
    };
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, [libraries]);

  useEffect(() => {
    if (state !== "authenticated" || !selectedId) return;
    if (new URLSearchParams(window.location.search).has("invite")) return;
    const route = currentWorkspaceRoute();
    if (route.libraryId === selectedId && route.page === workspacePage) return;
    const suffix = workspacePage === "overview" ? "/overview" : "";
    window.history.replaceState(
      {},
      "",
      `/libraries/${encodeURIComponent(selectedId)}${suffix}`,
    );
  }, [selectedId, state, workspacePage]);

  useEffect(() => {
    if (!selected) return;
    setExpandedItemIds([]);
    setExpandedDocumentIds([]);
    setResourcesByItem({});
    setResourceLoadingIds([]);
    setResourceReader(null);
    void (async () => {
      try {
        const [memberValue, collectionValue, tagValue] = await Promise.all([
          api.members(selected.library_id),
          api.collections(selected.library_id),
          api.tags(selected.library_id),
        ]);
        setMembers(memberValue.members);
        setCollections(collectionValue.collections);
        setTags(tagValue.tags);
        setActiveCollectionId("");
        setActiveTagId("");
        if (selected.role === "OWNER" && selected.library_type === "GROUP") {
          const invitationValue = await api.invitations(selected.library_id);
          setInvitations(invitationValue.invitations);
        } else {
          setInvitations([]);
        }
      } catch (error) {
        setMessage(error instanceof Error ? error.message : "Unable to load Library details");
      }
    })();
  }, [selected]);

  useEffect(() => {
    if (!selected) return;
    const requestId = catalogueRequestRef.current + 1;
    catalogueRequestRef.current = requestId;
    void (async () => {
      try {
        const value = await api.items(
          selected.library_id,
          activeCollectionId || null,
          catalogueStatus,
          {
            tagId: activeTagId || null,
            query: metadataQuery || null,
            advanced: advancedSearch,
          },
        );
        if (catalogueRequestRef.current !== requestId) return;
        setItems(value.items);
        setNextCursor(value.next_cursor);
        setSelectedItemIds([]);
      } catch (error) {
        setMessage(error instanceof Error ? error.message : "Unable to load catalogue");
      }
    })();
  }, [activeCollectionId, activeTagId, advancedSearch, catalogueStatus, metadataQuery, selected]);

  useEffect(() => {
    if (state !== "authenticated") return;
    const token = new URLSearchParams(window.location.search).get("invite");
    if (!token) return;
    void (async () => {
      try {
        setBusy(true);
        const joined = await api.acceptInvitation(token);
        window.history.replaceState({}, "", window.location.pathname);
        setMessage(`Joined ${joined.name} as ${joined.role.toLowerCase()}.`);
        await loadLibraries();
      } catch (error) {
        setMessage(error instanceof Error ? error.message : "Unable to accept invitation");
      } finally {
        setBusy(false);
      }
    })();
  }, [loadLibraries, state]);

  async function createGroup(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const formElement = event.currentTarget;
    const form = new FormData(formElement);
    const name = String(form.get("name") ?? "").trim();
    if (!name) return;
    setBusy(true);
    try {
      const created = await api.createGroup(name);
      await loadLibraries();
      setActiveCollectionId("");
      setActiveTagId("");
      setCatalogueStatus("ACTIVE");
      setSelectedId(created.library_id);
      setMessage(`Created ${created.name}.`);
      formElement.reset();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Unable to create group Library");
    } finally {
      setBusy(false);
    }
  }

  async function createCollection(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!selected) return;
    const formElement = event.currentTarget;
    const form = new FormData(formElement);
    const name = String(form.get("name") ?? "").trim();
    if (!name) return;
    setBusy(true);
    try {
      const created = await api.createCollection(
        selected.library_id,
        name,
        activeCollectionId || null,
      );
      setCollections((current) => [...current, created]);
      formElement.reset();
      setMessage(`Created collection ${created.name}.`);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Unable to create collection");
    } finally {
      setBusy(false);
    }
  }

  async function createPaper(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!selected) return;
    const formElement = event.currentTarget;
    const form = new FormData(formElement);
    const metadata = metadataFromForm(form);
    const doi = String(form.get("doi") ?? "").trim();
    const collectionIds = form.getAll("collection_ids").map(String);
    const tagIds = form.getAll("tag_ids").map(String);
    if (!metadata.title || !doi || !metadata.publication_year) return;
    setBusy(true);
    try {
      const created = await api.createItem(selected.library_id, {
        metadata,
        doi,
        collectionIds,
        tagIds,
      });
      let creationMessage = "Paper added with the available metadata.";
      if (doi) {
        try {
          const refresh = await api.refreshMetadata(
            selected.library_id,
            [created.library_item_id],
            "AUTO",
          );
          const job = refresh.jobs[0];
          if (job) void watchMetadataJob(selected.library_id, job.job_id, created.library_item_id);
          creationMessage = "Paper added. DOI metadata refresh is running in the background.";
        } catch {
          creationMessage =
            "Paper added, but metadata refresh could not be queued; retry it from the selection toolbar.";
        }
      }
      const [itemValue, collectionValue, tagValue] = await Promise.all([
        api.items(selected.library_id, activeCollectionId || null, catalogueStatus, {
          tagId: activeTagId || null,
          query: metadataQueryRef.current || null,
          advanced: advancedSearchRef.current,
        }),
        api.collections(selected.library_id),
        api.tags(selected.library_id),
      ]);
      setItems(itemValue.items);
      setNextCursor(itemValue.next_cursor);
      setCollections(collectionValue.collections);
      setTags(tagValue.tags);
      setAddPaperOpen(false);
      formElement.reset();
      setMessage(creationMessage);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Unable to add paper");
    } finally {
      setBusy(false);
    }
  }

  async function watchMetadataJob(libraryId: string, jobId: string, itemId: string) {
    const label = items.find((item) => item.library_item_id === itemId)?.effective_metadata.title ?? "Metadata refresh";
    upsertJobActivity({ activityId: jobId, label, kind: "METADATA", status: "PENDING", current: 0, total: 1, detail: "Waiting for metadata worker" });
    for (let poll = 0; poll < 30; poll += 1) {
      await new Promise((resolve) => window.setTimeout(resolve, 1000));
      try {
        const job = await api.job(libraryId, jobId);
        upsertJobActivity({
          activityId: jobId,
          label,
          kind: "METADATA",
          status: job.status,
          current: job.status === "SUCCEEDED" ? Math.max(1, job.progress_total) : job.progress_current,
          total: Math.max(1, job.progress_total),
          detail: job.progress_message || (job.status === "FAILED" ? "Metadata refresh failed" : "Refreshing metadata"),
        });
        if (job.status === "SUCCEEDED") {
          const targetItemId = String(job.result?.library_item_id ?? itemId);
          const refreshed = await api.item(libraryId, targetItemId);
          setItems((current) =>
            [
              refreshed,
              ...current.filter(
                (item) =>
                  item.library_item_id !== itemId &&
                  item.library_item_id !== targetItemId,
              ),
            ],
          );
          setSelectedItem((current) =>
            current?.library_item_id === itemId || current?.library_item_id === targetItemId
              ? refreshed
              : current,
          );
          setMessage(`Metadata refreshed from ${refreshed.metadata_source}.`);
          return;
        }
        if (["FAILED", "CANCELLED"].includes(job.status)) return;
      } catch {
        upsertJobActivity({ activityId: jobId, label, kind: "METADATA", status: "FAILED", current: 0, total: 1, detail: "Unable to read job status" });
        return;
      }
    }
  }

  async function importCitations(event: ChangeEvent<HTMLInputElement>) {
    if (!selected) return;
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file) return;
    setBusy(true);
    try {
      const queued = await api.importCitations(selected.library_id, file);
      upsertJobActivity({ activityId: queued.job_id, label: queued.filename, kind: "CITATIONS", status: "PENDING", current: 0, total: 1, detail: "Waiting for citation parser" });
      setMessage(`Importing ${queued.filename} in the background.`);
      void watchCitationImportJob(selected.library_id, queued.job_id, queued.filename);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Unable to import citation file");
    } finally {
      setBusy(false);
    }
  }

  async function watchCitationImportJob(libraryId: string, jobId: string, filename: string) {
    for (let poll = 0; poll < 120; poll += 1) {
      await new Promise((resolve) => window.setTimeout(resolve, 1000));
      try {
        const job = await api.job(libraryId, jobId);
        upsertJobActivity({
          activityId: jobId,
          label: filename,
          kind: "CITATIONS",
          status: job.status,
          current: job.status === "SUCCEEDED" ? Math.max(1, job.progress_total) : job.progress_current,
          total: Math.max(1, job.progress_total),
          detail: job.progress_message || (job.status === "FAILED" ? "Citation import failed" : "Importing citation records"),
        });
        if (job.status === "SUCCEEDED") {
          const [itemValue, collectionValue, tagValue] = await Promise.all([
            api.items(libraryId, null, "ACTIVE", {
              query: metadataQueryRef.current || null,
              advanced: advancedSearchRef.current,
            }),
            api.collections(libraryId),
            api.tags(libraryId),
          ]);
          if (selectedId === libraryId) {
            setActiveCollectionId("");
            setActiveTagId("");
            setCatalogueStatus("ACTIVE");
            setItems(itemValue.items);
            setNextCursor(itemValue.next_cursor);
            setCollections(collectionValue.collections);
            setTags(tagValue.tags);
          }
          const count = Number(job.result?.record_count ?? 0);
          const refreshes = Number(job.result?.metadata_refresh_jobs ?? 0);
          setMessage(`Imported ${count} paper(s); ${refreshes} DOI refresh job(s) queued.`);
          return;
        }
        if (["FAILED", "CANCELLED"].includes(job.status)) {
          setMessage(`Citation import ${job.status.toLowerCase()}.`);
          return;
        }
      } catch {
        upsertJobActivity({ activityId: jobId, label: filename, kind: "CITATIONS", status: "FAILED", current: 0, total: 1, detail: "Unable to read job status" });
        return;
      }
    }
    setMessage("Citation import is still running; its results will appear after a reload.");
  }

  async function selectZoteroFolder() {
    if (!selected) return;
    const picker = (window as DirectoryPickerWindow).showDirectoryPicker;
    if (!picker) {
      setMessage(
        "This browser cannot read a selected folder. Use Chromium or import zotero.sqlite only.",
      );
      return;
    }
    try {
      const directory = await picker({
        id: "literature-workspace-zotero",
        mode: "read",
        startIn: "documents",
      });
      const sqlite = await (await directory.getFileHandle("zotero.sqlite")).getFile();
      await beginZoteroImport(selected.library_id, sqlite, directory);
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") return;
      setMessage(
        error instanceof Error
          ? `Unable to open Zotero folder: ${error.message}`
          : "Choose the Zotero folder containing zotero.sqlite and storage.",
      );
    }
  }

  async function beginZoteroImport(
    libraryId: string,
    file: File,
    directory?: FileSystemDirectoryHandle,
  ) {
    setBusy(true);
    setZoteroImport({
      filename: directory?.name ?? file.name,
      phase: "UPLOADING",
      current: 0,
      total: 1,
      detail: "Uploading Zotero database snapshot",
    });
    try {
      const queued = await api.importZotero(libraryId, file);
      setZoteroImport((current) =>
        current
          ? { ...current, phase: "RUNNING", detail: "Waiting for Zotero parser" }
          : current,
      );
      setMessage(`Importing ${queued.filename} in the background.`);
      void watchZoteroImportJob(libraryId, queued.job_id, directory);
    } catch (error) {
      const detail = error instanceof Error ? error.message : "Unable to import Zotero database";
      setZoteroImport((current) =>
        current ? { ...current, phase: "FAILED", detail } : current,
      );
      setMessage(detail);
    } finally {
      setBusy(false);
    }
  }

  async function uploadZoteroFolderAttachments(
    libraryId: string,
    jobId: string,
    directory: FileSystemDirectoryHandle,
  ) {
    const manifest = await api.zoteroAttachments(libraryId, jobId);
    const pending: ZoteroAttachment[] = manifest.attachments.filter(
      (value) => !value.imported,
    );
    let cursor = 0;
    let completed = manifest.attachments.length - pending.length;
    let uploaded = 0;
    let missing = 0;
    let failed = 0;
    setZoteroImport((current) =>
      current
        ? {
            ...current,
            phase: "RUNNING",
            current: completed,
            total: Math.max(1, manifest.attachments.length),
            detail: `Uploading ${pending.length} matching Zotero PDF(s)`,
          }
        : current,
    );
    const uploadOne = async () => {
      while (cursor < pending.length) {
        const index = cursor;
        cursor += 1;
        const attachment = pending[index];
        const file = await fileFromDirectory(directory, attachment.relative_path);
        if (file === null) {
          missing += 1;
        } else {
          try {
            await api.importZoteroAttachment(libraryId, jobId, attachment, file);
            uploaded += 1;
          } catch {
            failed += 1;
          }
        }
        completed += 1;
        setZoteroImport((current) =>
          current
            ? {
                ...current,
                current: completed,
                total: Math.max(1, manifest.attachments.length),
                detail: `${uploaded} uploaded · ${missing} missing · ${failed} failed`,
              }
            : current,
        );
      }
    };
    await Promise.all(
      Array.from({ length: Math.min(3, pending.length) }, () => uploadOne()),
    );
    return {
      declared: manifest.attachments.length,
      uploaded,
      missing,
      failed,
      alreadyImported: manifest.attachments.length - pending.length,
    };
  }

  async function watchZoteroImportJob(
    libraryId: string,
    jobId: string,
    directory?: FileSystemDirectoryHandle,
  ) {
    for (let poll = 0; poll < 900; poll += 1) {
      await new Promise((resolve) => window.setTimeout(resolve, 1000));
      try {
        const job = await api.job(libraryId, jobId);
        if (["PENDING", "RUNNING"].includes(job.status)) {
          setZoteroImport((current) =>
            current
              ? {
                  ...current,
                  phase: "RUNNING",
                  current: job.progress_current,
                  total: Math.max(1, job.progress_total),
                  detail: job.progress_message || "Importing Zotero records",
                }
              : current,
          );
          continue;
        }
        if (job.status === "SUCCEEDED") {
          const [initialItems, collectionValue, tagValue] = await Promise.all([
            api.items(libraryId, null, "ACTIVE", {
              query: metadataQueryRef.current || null,
              advanced: advancedSearchRef.current,
            }),
            api.collections(libraryId),
            api.tags(libraryId),
          ]);
          if (selectedId === libraryId) {
            setActiveCollectionId("");
            setActiveTagId("");
            setCatalogueStatus("ACTIVE");
            setItems(initialItems.items);
            setNextCursor(initialItems.next_cursor);
            setCollections(collectionValue.collections);
            setTags(tagValue.tags);
          }
          const attachmentResult = directory
            ? await uploadZoteroFolderAttachments(libraryId, jobId, directory)
            : null;
          if (attachmentResult && selectedId === libraryId) {
            const refreshedItems = await api.items(libraryId, null, "ACTIVE", {
              query: metadataQueryRef.current || null,
              advanced: advancedSearchRef.current,
            });
            setItems(refreshedItems.items);
            setNextCursor(refreshedItems.next_cursor);
          }
          const records = Number(job.result?.record_count ?? 0);
          const declarations = Number(job.result?.attachment_declarations ?? 0);
          const conflicts = Number(job.result?.identifier_conflicts ?? 0);
          setZoteroImport((current) =>
            current
              ? {
                  ...current,
                  phase: "READY",
                  current: attachmentResult?.declared ?? records,
                  total: Math.max(1, attachmentResult?.declared ?? records),
                  detail: attachmentResult
                    ? `${records} records · ${attachmentResult.uploaded} PDFs uploaded · ${attachmentResult.alreadyImported} reused · ${attachmentResult.missing} missing · ${attachmentResult.failed} failed`
                    : `${records} records · ${declarations} attachment declarations · ${conflicts} conflicts`,
                }
              : current,
          );
          setMessage(
            attachmentResult
              ? `Zotero import completed: ${records} records and ${attachmentResult.uploaded} PDFs uploaded.`
              : `Zotero import completed: ${records} records, ${conflicts} conflicts.`,
          );
          return;
        }
        setZoteroImport((current) =>
          current
            ? { ...current, phase: "FAILED", detail: "Zotero import failed" }
            : current,
        );
        setMessage("Zotero import failed.");
        return;
      } catch {
        return;
      }
    }
    setMessage("Zotero import is still running; progress remains available after reload.");
  }

  async function importPdfs(event: ChangeEvent<HTMLInputElement>) {
    if (!selected) return;
    const files = Array.from(event.target.files ?? []);
    event.target.value = "";
    if (files.length === 0) return;
    const libraryId = selected.library_id;
    const collectionId = activeCollectionId || null;
    const entries = files.map((file) => ({
      clientId: crypto.randomUUID(),
      filename: file.name,
      phase: "UPLOADING" as const,
      progress: 0,
      detail: "Uploading PDF",
    }));
    setPdfImports(entries);
    setActiveTagId("");
    setCatalogueStatus("ACTIVE");
    setBusy(true);
    setMessage(`Uploading ${files.length} PDF(s); entries will appear progressively.`);
    let cursor = 0;
    const uploadOne = async () => {
      while (cursor < files.length) {
        const index = cursor;
        cursor += 1;
        const file = files[index];
        const entry = entries[index];
        try {
          const queued = await api.importPdf(libraryId, file, collectionId);
          if (queued.initial_item) {
            setItems((current) => [
              queued.initial_item!,
              ...current.filter(
                (item) => item.library_item_id !== queued.initial_item!.library_item_id,
              ),
            ]);
          }
          setPdfImports((current) =>
            current.map((value) =>
              value.clientId === entry.clientId
                ? {
                    ...value,
                    jobId: queued.job_id,
                    itemId: queued.library_item_id,
                    phase: "QUEUED",
                    progress: 5,
                    detail: "Waiting for PDF extraction",
                  }
                : value,
            ),
          );
          void watchPdfImportJob(
            libraryId,
            entry.clientId,
            queued.job_id,
            queued.library_item_id,
          );
        } catch (error) {
          setPdfImports((current) =>
            current.map((value) =>
              value.clientId === entry.clientId
                ? {
                    ...value,
                    phase: "FAILED",
                    detail: error instanceof Error ? error.message : "Upload failed",
                  }
                : value,
            ),
          );
        }
      }
    };
    await Promise.all(
      Array.from({ length: Math.min(4, files.length) }, () => uploadOne()),
    );
    setBusy(false);
  }

  async function watchPdfImportJob(
    libraryId: string,
    clientId: string,
    jobId: string,
    initialItemId: string,
  ) {
    for (let poll = 0; poll < 300; poll += 1) {
      await new Promise((resolve) => window.setTimeout(resolve, 1000));
      try {
        const job = await api.job(libraryId, jobId);
        if (["PENDING", "RUNNING"].includes(job.status)) {
          const phase: PdfImportPhase =
            job.progress_current >= 2 ? "MATCHING" : job.progress_current >= 1 ? "EXTRACTING" : "QUEUED";
          setPdfImports((current) =>
            current.map((value) =>
              value.clientId === clientId
                ? {
                    ...value,
                    phase,
                    progress: Math.max(
                      value.progress,
                      Math.round((job.progress_current / job.progress_total) * 70),
                    ),
                    detail: job.progress_message || "Waiting for worker",
                  }
                : value,
            ),
          );
          continue;
        }
        if (job.status === "SUCCEEDED") {
          const targetItemId = String(job.result?.library_item_id ?? initialItemId);
          const outcome = String(job.result?.outcome ?? "READY");
          const metadataJobId = job.result?.metadata_job_id
            ? String(job.result.metadata_job_id)
            : null;
          const refreshed = await api.item(libraryId, targetItemId);
          setItems((current) => [
            refreshed,
            ...current.filter(
              (item) =>
                item.library_item_id !== initialItemId &&
                item.library_item_id !== targetItemId,
            ),
          ]);
          setPdfImports((current) =>
            current.map((value) =>
              value.clientId === clientId
                ? {
                    ...value,
                    itemId: targetItemId,
                    phase:
                      outcome === "NEEDS_REVIEW"
                        ? "NEEDS_REVIEW"
                        : metadataJobId
                          ? "RESOLVING_METADATA"
                          : "READY",
                    progress: outcome === "NEEDS_REVIEW" ? 100 : metadataJobId ? 75 : 100,
                    detail:
                      outcome === "NEEDS_REVIEW"
                        ? "No DOI found; manual review required"
                        : metadataJobId
                          ? "DOI matched; resolving metadata"
                          : "DOI matched to existing metadata",
                  }
                : value,
            ),
          );
          if (metadataJobId) {
            void watchPdfMetadataJob(libraryId, clientId, metadataJobId, targetItemId);
          }
          return;
        }
        setPdfImports((current) =>
          current.map((value) =>
            value.clientId === clientId
              ? { ...value, phase: "FAILED", detail: "PDF processing failed" }
              : value,
          ),
        );
        return;
      } catch {
        return;
      }
    }
  }

  async function watchPdfMetadataJob(
    libraryId: string,
    clientId: string,
    jobId: string,
    itemId: string,
  ) {
    for (let poll = 0; poll < 120; poll += 1) {
      await new Promise((resolve) => window.setTimeout(resolve, 1000));
      try {
        const job = await api.job(libraryId, jobId);
        if (job.status === "SUCCEEDED") {
          const refreshed = await api.item(libraryId, itemId);
          setItems((current) =>
            current.map((item) => (item.library_item_id === itemId ? refreshed : item)),
          );
          setPdfImports((current) =>
            current.map((value) =>
              value.clientId === clientId
                ? {
                    ...value,
                    phase: "READY",
                    progress: 100,
                    detail: `Metadata ready from ${refreshed.metadata_source}`,
                  }
                : value,
            ),
          );
          return;
        }
        if (["FAILED", "CANCELLED"].includes(job.status)) {
          setPdfImports((current) =>
            current.map((value) =>
              value.clientId === clientId
                ? {
                    ...value,
                    phase: "NEEDS_REVIEW",
                    progress: 100,
                    detail: "DOI found, but metadata could not be resolved",
                  }
                : value,
            ),
          );
          return;
        }
      } catch {
        return;
      }
    }
  }

  async function trashPaper(item: CatalogueItem) {
    if (!selected) return;
    if (!window.confirm(`Move “${item.effective_metadata.title}” to trash?`)) return;
    setBusy(true);
    try {
      await api.trashItem(selected.library_id, item.library_item_id, item.revision);
      setItems((current) =>
        current.filter((value) => value.library_item_id !== item.library_item_id),
      );
      setSelectedItem(null);
      const [collectionValue, tagValue] = await Promise.all([
        api.collections(selected.library_id),
        api.tags(selected.library_id),
      ]);
      setCollections(collectionValue.collections);
      setTags(tagValue.tags);
      setMessage("Paper moved to trash. Its canonical record remains available.");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Unable to trash paper");
    } finally {
      setBusy(false);
    }
  }

  async function restorePaper(item: CatalogueItem) {
    if (!selected) return;
    setBusy(true);
    try {
      await api.restoreItem(selected.library_id, item.library_item_id, item.revision);
      setItems((current) =>
        current.filter((value) => value.library_item_id !== item.library_item_id),
      );
      setSelectedItem(null);
      const [collectionValue, tagValue] = await Promise.all([
        api.collections(selected.library_id),
        api.tags(selected.library_id),
      ]);
      setCollections(collectionValue.collections);
      setTags(tagValue.tags);
      setMessage("Paper restored with its previous Collection and tag placement.");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Unable to restore paper");
    } finally {
      setBusy(false);
    }
  }

  async function updatePaper(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!selected || !selectedItem) return;
    const form = new FormData(event.currentTarget);
    const baseline = selectedItem.canonical_metadata;
    const metadata = metadataFromForm(form);
    const overrides = Object.fromEntries(
      Object.entries(metadata).map(([key, value]) => [
        key,
        JSON.stringify(value) === JSON.stringify(baseline[key] ?? null) ? null : value,
      ]),
    );
    const desiredCollections = new Set(form.getAll("collection_ids").map(String));
    const desiredTags = new Set(form.getAll("tag_ids").map(String));
    setBusy(true);
    try {
      const updated = await api.updateItem(
        selected.library_id,
        selectedItem.library_item_id,
        selectedItem.revision,
        overrides,
        [...desiredCollections],
        [...desiredTags],
      );
      const [itemValue, collectionValue, tagValue] = await Promise.all([
        api.items(selected.library_id, activeCollectionId || null, catalogueStatus, {
          tagId: activeTagId || null,
          query: metadataQueryRef.current || null,
          advanced: advancedSearchRef.current,
        }),
        api.collections(selected.library_id),
        api.tags(selected.library_id),
      ]);
      setItems(itemValue.items);
      setNextCursor(itemValue.next_cursor);
      setCollections(collectionValue.collections);
      setTags(tagValue.tags);
      setSelectedItem({
        ...updated,
        collection_ids: [...desiredCollections],
        tag_ids: [...desiredTags],
      });
      setMessage("Library metadata, Collection placement, and tags updated.");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Unable to update paper");
    } finally {
      setBusy(false);
    }
  }

  async function updateCollection(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!selected || !activeCollection) return;
    const form = new FormData(event.currentTarget);
    const name = String(form.get("name") ?? "").trim();
    if (!name) return;
    setBusy(true);
    try {
      const updated = await api.updateCollection(
        selected.library_id,
        activeCollection.collection_id,
        {
          name,
          parentCollectionId: activeCollection.parent_collection_id,
          expectedRevision: activeCollection.revision,
        },
      );
      setCollections((current) =>
        current.map((value) =>
          value.collection_id === updated.collection_id ? updated : value,
        ),
      );
      setManageCollectionOpen(false);
      setMessage(`Renamed Collection to ${updated.name}.`);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Unable to update Collection");
    } finally {
      setBusy(false);
    }
  }

  async function deleteCollection() {
    if (!selected || !activeCollection) return;
    if (
      !window.confirm(
        `Delete “${activeCollection.name}”? Papers remain in the Library and other Collections.`,
      )
    ) return;
    setBusy(true);
    try {
      await api.deleteCollection(
        selected.library_id,
        activeCollection.collection_id,
        activeCollection.revision,
      );
      setCollections((current) =>
        current.filter((value) => value.collection_id !== activeCollection.collection_id),
      );
      setActiveCollectionId("");
      setManageCollectionOpen(false);
      setMessage("Collection deleted. Its papers remain in the Library.");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Unable to delete Collection");
    } finally {
      setBusy(false);
    }
  }

  async function createTag(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!selected) return;
    const formElement = event.currentTarget;
    const form = new FormData(formElement);
    const name = String(form.get("name") ?? "").trim();
    if (!name) return;
    setBusy(true);
    try {
      const created = await api.createTag(selected.library_id, name);
      setTags((current) => [...current, created]);
      formElement.reset();
      setMessage(`Created tag ${created.name}.`);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Unable to create tag");
    } finally {
      setBusy(false);
    }
  }

  async function deleteTag(tag: LibraryTag) {
    if (!selected) return;
    if (!window.confirm(`Delete tag “${tag.name}”? Papers will remain in the Library.`)) return;
    setBusy(true);
    try {
      await api.deleteTag(selected.library_id, tag.tag_id, tag.revision);
      setTags((current) => current.filter((value) => value.tag_id !== tag.tag_id));
      if (activeTagId === tag.tag_id) setActiveTagId("");
      setMessage("Tag deleted. Its papers remain in the Library.");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Unable to delete tag");
    } finally {
      setBusy(false);
    }
  }

  async function loadMoreItems() {
    if (!selected || !nextCursor || loadingMoreRef.current) return;
    loadingMoreRef.current = true;
    setLoadingMore(true);
    const requestId = catalogueRequestRef.current;
    try {
      const value = await api.items(
        selected.library_id,
        activeCollectionId || null,
        catalogueStatus,
        {
          tagId: activeTagId || null,
          cursor: nextCursor,
          query: metadataQueryRef.current || null,
          advanced: advancedSearchRef.current,
        },
      );
      if (catalogueRequestRef.current !== requestId) return;
      setItems((current) => {
        const known = new Set(current.map((item) => item.library_item_id));
        return [...current, ...value.items.filter((item) => !known.has(item.library_item_id))];
      });
      setNextCursor(value.next_cursor);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Unable to load more papers");
    } finally {
      loadingMoreRef.current = false;
      setLoadingMore(false);
    }
  }

  function loadMoreAtTableEnd(element: HTMLDivElement) {
    if (
      nextCursor
      && element.scrollHeight - element.scrollTop - element.clientHeight <= 32
    ) {
      void loadMoreItems();
    }
  }

  async function selectAllResults() {
    if (!selected) return;
    setBusy(true);
    try {
      const allItems: CatalogueItem[] = [];
      let cursor: string | null = null;
      do {
        const value = await api.items(
          selected.library_id,
          activeCollectionId || null,
          catalogueStatus,
          {
            tagId: activeTagId || null,
            cursor,
            limit: 100,
            query: metadataQueryRef.current || null,
            advanced: advancedSearchRef.current,
          },
        );
        allItems.push(...value.items);
        cursor = value.next_cursor;
      } while (cursor);
      setItems(allItems);
      setNextCursor(null);
      setSelectedItemIds(allItems.map((item) => item.library_item_id));
      setMessage(`Selected all ${allItems.length} matching paper(s).`);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Unable to select all papers");
    } finally {
      setBusy(false);
    }
  }

  async function applyBulkAction(action: BulkAction = bulkAction) {
    if (!selected || selectedItemIds.length === 0) return;
    const requiresTarget = !["TRASH", "RESTORE"].includes(action);
    if (requiresTarget && !bulkTargetId) {
      setMessage("Choose a Collection or tag target first.");
      return;
    }
    const selectedItems = items.filter((item) => selectedItemIds.includes(item.library_item_id));
    setBusy(true);
    try {
      let updated = 0;
      const entries = selectedItems.map((item) => ({
          library_item_id: item.library_item_id,
          expected_revision: item.revision,
      }));
      for (let offset = 0; offset < entries.length; offset += 100) {
        const result = await api.bulkOrganize(
          selected.library_id,
          entries.slice(offset, offset + 100),
          action,
          requiresTarget ? bulkTargetId : null,
        );
        updated += result.updated;
      }
      const [itemValue, collectionValue, tagValue] = await Promise.all([
        api.items(selected.library_id, activeCollectionId || null, catalogueStatus, {
          tagId: activeTagId || null,
          query: metadataQueryRef.current || null,
          advanced: advancedSearchRef.current,
        }),
        api.collections(selected.library_id),
        api.tags(selected.library_id),
      ]);
      setItems(itemValue.items);
      setNextCursor(itemValue.next_cursor);
      setCollections(collectionValue.collections);
      setTags(tagValue.tags);
      setSelectedItemIds([]);
      setMessage(`${updated} papers updated in batches of at most 100.`);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Unable to organize selected papers");
    } finally {
      setBusy(false);
    }
  }

  async function trashSelectedItems() {
    if (selectedItemIds.length === 0) return;
    if (!window.confirm(`Move ${selectedItemIds.length} selected paper(s) to trash?`)) return;
    await applyBulkAction("TRASH");
  }

  async function refreshSelectedMetadata() {
    if (!selected || selectedItemIds.length === 0) return;
    setBusy(true);
    try {
      let queued = 0;
      for (let offset = 0; offset < selectedItemIds.length; offset += 100) {
        const result = await api.refreshMetadata(
          selected.library_id,
          selectedItemIds.slice(offset, offset + 100),
        );
        queued += result.jobs.length;
        for (const job of result.jobs) {
          void watchMetadataJob(selected.library_id, job.job_id, job.library_item_id);
        }
      }
      setMessage(`Queued metadata refresh for ${queued} paper(s).`);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Unable to refresh metadata");
    } finally {
      setBusy(false);
    }
  }

  async function loadItemResources(itemId: string, refreshItem = false) {
    if (!selected) return;
    setResourceLoadingIds((current) =>
      current.includes(itemId) ? current : [...current, itemId],
    );
    try {
      const [resources, refreshedItem] = await Promise.all([
        api.itemResources(selected.library_id, itemId),
        refreshItem ? api.item(selected.library_id, itemId) : Promise.resolve(null),
      ]);
      setResourcesByItem((current) => ({ ...current, [itemId]: resources }));
      if (refreshedItem) {
        setItems((current) =>
          current.map((item) =>
            item.library_item_id === itemId ? refreshedItem : item,
          ),
        );
      }
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Unable to load item resources");
    } finally {
      setResourceLoadingIds((current) => current.filter((value) => value !== itemId));
    }
  }

  function toggleItemResources(itemId: string) {
    const expanded = expandedItemIds.includes(itemId);
    setExpandedItemIds((current) =>
      expanded ? current.filter((value) => value !== itemId) : [...current, itemId],
    );
    if (!expanded && !resourcesByItem[itemId]) void loadItemResources(itemId);
  }

  async function uploadItemPdf(
    item: CatalogueItem,
    event: ChangeEvent<HTMLInputElement>,
  ) {
    if (!selected) return;
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file) return;
    const current = resourcesByItem[item.library_item_id]?.primary_pdf;
    setBusy(true);
    try {
      await api.uploadPrimaryPdf(
        selected.library_id,
        item.library_item_id,
        file,
        current?.origin === "OVERRIDE" ? current.revision : undefined,
      );
      await loadItemResources(item.library_item_id, true);
      setExpandedItemIds((values) =>
        values.includes(item.library_item_id) ? values : [...values, item.library_item_id],
      );
      setMessage(`${file.name} is now the PDF override for this Library Item.`);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Unable to upload PDF override");
    } finally {
      setBusy(false);
    }
  }

  async function cancelItemPdfOverride(item: CatalogueItem, resource: ArtifactResource) {
    if (!selected || resource.origin !== "OVERRIDE") return;
    if (!window.confirm("Cancel this PDF override and return to the canonical PDF?")) return;
    setBusy(true);
    try {
      await api.cancelPrimaryPdf(
        selected.library_id,
        item.library_item_id,
        resource.revision,
      );
      await loadItemResources(item.library_item_id, true);
      setMessage("PDF override cancelled; this item now follows the canonical PDF.");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Unable to cancel PDF override");
    } finally {
      setBusy(false);
    }
  }

  async function uploadItemAsset(
    item: CatalogueItem,
    event: ChangeEvent<HTMLInputElement>,
  ) {
    if (!selected) return;
    const files = Array.from(event.target.files ?? []);
    event.target.value = "";
    if (files.length === 0) return;
    setBusy(true);
    try {
      for (const file of files) {
        await api.uploadAsset(selected.library_id, item.library_item_id, file);
      }
      await loadItemResources(item.library_item_id, true);
      setExpandedItemIds((values) =>
        values.includes(item.library_item_id) ? values : [...values, item.library_item_id],
      );
      setMessage(`${files.length} user file(s) attached.`);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Unable to upload attachment");
    } finally {
      setBusy(false);
    }
  }

  async function renameItemAsset(item: CatalogueItem, asset: AssetResource) {
    if (!selected) return;
    const name = window.prompt("Attachment name", asset.filename)?.trim();
    if (!name || name === asset.filename) return;
    setBusy(true);
    try {
      await api.renameAsset(
        selected.library_id,
        item.library_item_id,
        asset.asset_id,
        name,
        asset.revision,
      );
      await loadItemResources(item.library_item_id, true);
      setMessage("Attachment renamed.");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Unable to rename attachment");
    } finally {
      setBusy(false);
    }
  }

  async function deleteItemAsset(item: CatalogueItem, asset: AssetResource) {
    if (!selected || !window.confirm(`Delete ${asset.filename} from this item?`)) return;
    setBusy(true);
    try {
      await api.deleteAsset(
        selected.library_id,
        item.library_item_id,
        asset.asset_id,
        asset.revision,
      );
      await loadItemResources(item.library_item_id, true);
      setMessage("Attachment deleted. Shared Blob bytes are retained when still referenced.");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Unable to delete attachment");
    } finally {
      setBusy(false);
    }
  }

  function openArtifact(item: CatalogueItem, resource: ArtifactResource) {
    if (!selected || resource.status === "MISSING") return;
    setResourceReader({
      title: resource.filename ?? resource.artifact_key,
      mediaType: resource.media_type,
      url: api.artifactContentPath(
        selected.library_id,
        item.library_item_id,
        resource.artifact_key,
      ),
    });
  }

  function openAsset(item: CatalogueItem, resource: AssetResource) {
    if (!selected || resource.status === "MISSING") return;
    setResourceReader({
      title: resource.filename,
      mediaType: resource.media_type,
      url: api.assetContentPath(
        selected.library_id,
        item.library_item_id,
        resource.asset_id,
      ),
    });
  }

  async function invite(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!selected) return;
    const formElement = event.currentTarget;
    const form = new FormData(formElement);
    const email = String(form.get("email") ?? "").trim();
    const role = String(form.get("role") ?? "VIEWER") as "EDITOR" | "VIEWER";
    setBusy(true);
    try {
      const created = await api.invite(selected.library_id, email, role);
      setInvitations((current) => [created, ...current]);
      const link = `${window.location.origin}/?invite=${encodeURIComponent(created.accept_token ?? "")}`;
      setInvitationLink(link);
      setMessage(`Invitation created for ${email}.`);
      formElement.reset();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Unable to invite member");
    } finally {
      setBusy(false);
    }
  }

  async function signOut() {
    setBusy(true);
    try {
      const result = await api.logout();
      setSession(null);
      setLibraries([]);
      setSelectedId("");
      setInvitationLink("");
      setState("anonymous");
      if (result.provider_logout_url) {
        window.location.assign(result.provider_logout_url);
      }
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Unable to sign out");
      setBusy(false);
    }
  }

  async function regenerateInvitation(invitation: Invitation) {
    if (!selected) return;
    setBusy(true);
    try {
      const regenerated = await api.regenerateInvitation(
        selected.library_id,
        invitation.invitation_id,
      );
      setInvitations((current) =>
        current.map((value) =>
          value.invitation_id === regenerated.invitation_id ? regenerated : value,
        ),
      );
      const link = `${window.location.origin}/?invite=${encodeURIComponent(regenerated.accept_token ?? "")}`;
      setInvitationLink(link);
      setMessage(`Generated a new invitation link for ${regenerated.email}.`);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Unable to regenerate invitation");
    } finally {
      setBusy(false);
    }
  }

  async function copyInvitationLink() {
    try {
      await navigator.clipboard.writeText(invitationLink);
      setMessage("Invitation link copied.");
    } catch {
      setMessage("Unable to copy automatically. Select the link and copy it manually.");
    }
  }

  async function updateMember(member: Member, role: Member["role"]) {
    if (!selected) return;
    setBusy(true);
    try {
      await api.updateMemberRole(selected.library_id, member.principal_id, role);
      setMembers((current) =>
        current.map((value) =>
          value.principal_id === member.principal_id ? { ...value, role } : value,
        ),
      );
      setMessage(`Updated ${member.display_name} to ${role.toLowerCase()}.`);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Unable to update member");
    } finally {
      setBusy(false);
    }
  }

  async function removeMember(member: Member) {
    if (!selected) return;
    if (!window.confirm(`Remove ${member.display_name} from ${selected.name}?`)) return;
    setBusy(true);
    try {
      await api.removeMember(selected.library_id, member.principal_id);
      setMembers((current) =>
        current.filter((value) => value.principal_id !== member.principal_id),
      );
      setMessage(`Removed ${member.display_name}. Their access is no longer active.`);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Unable to remove member");
    } finally {
      setBusy(false);
    }
  }

  if (state === "loading") return <Centered label="Establishing secure session…" />;
  if (state === "anonymous") {
    const returnPath = `${window.location.pathname}${window.location.search}`;
    return (
      <main className="welcome-shell">
        <section className="welcome-card">
          <span className="eyebrow">LITERATURE WORKSPACE V2</span>
          <h1>Your research library,<br />with explicit ownership.</h1>
          <p>Sign in to access your personal and shared group Libraries.</p>
          <a
            className="primary-button"
            href={`/api/v2/auth/login?return_path=${encodeURIComponent(returnPath)}`}
          >
            Continue with identity provider
          </a>
          <span className="security-note">OIDC · server session · Library-scoped authorization</span>
        </section>
      </main>
    );
  }
  if (state === "error") return <Centered label={message || "Application unavailable"} />;

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-mark">LW</span>
          <div><strong>Literature</strong><span>Library v2</span></div>
        </div>
        <div className="library-list">
          <span className="section-label">LIBRARIES</span>
          {libraries.map((library) => (
            <button
              className={`library-button ${library.library_id === selectedId ? "active" : ""}`}
              key={library.library_id}
              onClick={() => {
                setActiveCollectionId("");
                setActiveTagId("");
                setCatalogueStatus("ACTIVE");
                setSelectedItem(null);
                setInspectedItemId("");
                setSelectedId(library.library_id);
                navigateWorkspace(workspacePage, library.library_id);
              }}
            >
              <span className="library-glyph">{library.library_type === "PERSONAL" ? "P" : "G"}</span>
              <span><strong>{library.name}</strong><small>{library.role.toLowerCase()}</small></span>
            </button>
          ))}
        </div>
        <form className="new-group" onSubmit={createGroup}>
          <input name="name" placeholder="New group Library" maxLength={200} disabled={busy} />
          <button disabled={busy} aria-label="Create group Library">+</button>
        </form>
        <div className="account-block">
          <div><strong>{session?.principal.display_name}</strong><span>Authenticated</span></div>
          <button disabled={busy} onClick={() => void signOut()}>Sign out</button>
        </div>
      </aside>

      <main className={`content ${workspacePage === "library" ? "library-content" : ""}`}>
        {selected ? (
          <>
            <header className="content-header">
              <div>
                <span className="eyebrow">{selected.library_type} LIBRARY</span>
                <h1>{selected.name}</h1>
              </div>
              <span className="role-pill">{selected.role}</span>
            </header>
            <nav className="workspace-tabs" aria-label="Library pages">
              <button
                className={workspacePage === "library" ? "active" : ""}
                onClick={() => navigateWorkspace("library", selected.library_id)}
              >
                Library
              </button>
              <button
                className={workspacePage === "overview" ? "active" : ""}
                onClick={() => navigateWorkspace("overview", selected.library_id)}
              >
                Overview
              </button>
              <a href="/retrieval">Retrieval</a>
              {session?.principal.system_role === "ADMIN" && (
                <a href="/documents/admin">Document admin</a>
              )}
            </nav>
            {message && <div className="notice" onClick={() => setMessage("")}>{message}</div>}
            {workspacePage === "library" ? (
              <section className="zotero-shell">
                <aside className="zotero-nav">
                  <div className="zotero-pane-heading">
                    <strong>My Library</strong>
                    <small>{items.length} shown</small>
                  </div>
                  <button
                    className={`zotero-nav-row ${catalogueStatus === "ACTIVE" && !activeCollectionId && !activeTagId ? "active" : ""}`}
                    onClick={() => {
                      setCatalogueStatus("ACTIVE");
                      setActiveCollectionId("");
                      setActiveTagId("");
                    }}
                  >
                    <span>▦</span> All Papers
                  </button>
                  <div className="zotero-nav-label">COLLECTIONS</div>
                  {orderedCollections.map((collection) => (
                    <button
                      className={`zotero-nav-row ${activeCollectionId === collection.collection_id ? "active" : ""}`}
                      key={collection.collection_id}
                      style={{ paddingLeft: `${12 + collectionDepth(collection, collections) * 16}px` }}
                      onClick={() => {
                        setCatalogueStatus("ACTIVE");
                        setActiveTagId("");
                        setActiveCollectionId(collection.collection_id);
                      }}
                    >
                      <span>⌑</span>
                      <span className="zotero-nav-name">{collection.name}</span>
                      <small>{collection.item_count}</small>
                    </button>
                  ))}
                  {selected.role !== "VIEWER" && (
                    <form className="zotero-inline-create" onSubmit={createCollection}>
                      <input name="name" placeholder="New collection" maxLength={200} disabled={busy} />
                      <button disabled={busy}>+</button>
                    </form>
                  )}
                  {activeCollection && selected.role !== "VIEWER" && (
                    <button className="zotero-manage-link" onClick={() => setManageCollectionOpen(true)}>
                      Edit selected collection
                    </button>
                  )}
                  <div className="zotero-nav-label">TAGS</div>
                  {tags.map((tag) => (
                    <div className="zotero-tag-row" key={tag.tag_id}>
                      <button
                        className={`zotero-nav-row ${activeTagId === tag.tag_id ? "active" : ""}`}
                        onClick={() => {
                          setCatalogueStatus("ACTIVE");
                          setActiveCollectionId("");
                          setActiveTagId(tag.tag_id);
                        }}
                      >
                        <span className="tag-dot" />
                        <span className="zotero-nav-name">{tag.name}</span>
                        <small>{tag.item_count}</small>
                      </button>
                      {selected.role !== "VIEWER" && (
                        <button className="zotero-tag-delete" aria-label={`Delete tag ${tag.name}`} onClick={() => void deleteTag(tag)}>×</button>
                      )}
                    </div>
                  ))}
                  {selected.role !== "VIEWER" && (
                    <form className="zotero-inline-create" onSubmit={createTag}>
                      <input name="name" placeholder="New tag" maxLength={100} disabled={busy} />
                      <button disabled={busy}>+</button>
                    </form>
                  )}
                  <div className="zotero-nav-spacer" />
                  <button
                    className={`zotero-nav-row trash ${catalogueStatus === "TRASHED" ? "active" : ""}`}
                    onClick={() => {
                      setCatalogueStatus("TRASHED");
                      setActiveCollectionId("");
                      setActiveTagId("");
                      setBulkAction("RESTORE");
                    }}
                  >
                    <span>⌫</span> Trash
                  </button>
                </aside>

                <div className="zotero-library-pane">
                  <div className="zotero-toolbar">
                    <input
                      className="zotero-search"
                      value={libraryQuery}
                      onChange={(event) => setLibraryQuery(event.target.value)}
                      placeholder="Search Library metadata"
                      aria-label="Search Library metadata"
                    />
                    <button
                      className={`zotero-toolbar-button ${advancedFilterCount ? "active" : ""}`}
                      type="button"
                      onClick={() => setAdvancedSearchOpen((value) => !value)}
                    >
                      Advanced{advancedFilterCount ? ` (${advancedFilterCount})` : ""}
                    </button>
                    {selected.role !== "VIEWER" && catalogueStatus === "ACTIVE" && (
                      <div className="zotero-import-actions">
                        <label className="zotero-toolbar-button primary">
                          Import PDF
                          <input type="file" accept=".pdf,application/pdf" multiple disabled={busy} onChange={(event) => void importPdfs(event)} />
                        </label>
                        <label className="zotero-toolbar-button">
                          Import citations
                          <input type="file" accept=".bib,.ris,.json,application/x-bibtex,application/x-research-info-systems,application/json" disabled={busy} onChange={(event) => void importCitations(event)} />
                        </label>
                        <button className="zotero-toolbar-button" disabled={busy} onClick={() => void selectZoteroFolder()}>
                          Zotero folder
                        </button>
                        <button className="zotero-toolbar-button" onClick={() => setAddPaperOpen(true)}>Manual</button>
                      </div>
                    )}
                  </div>
                  {advancedSearchOpen && (
                    <form
                      className="advanced-search-panel"
                      key={advancedFormKey}
                      onSubmit={applyAdvancedSearch}
                    >
                      <div className="advanced-search-grid">
                        <label>Title<input name="title" defaultValue={advancedSearch.title ?? ""} /></label>
                        <label>Author<input name="author" defaultValue={advancedSearch.author ?? ""} /></label>
                        <label>DOI / arXiv / identifier<input name="identifier" defaultValue={advancedSearch.identifier ?? ""} /></label>
                        <label>Publication / venue<input name="venue" defaultValue={advancedSearch.venue ?? ""} /></label>
                        <label>Year from<input name="yearFrom" type="number" min={1000} max={3000} defaultValue={advancedSearch.yearFrom ?? ""} /></label>
                        <label>Year to<input name="yearTo" type="number" min={1000} max={3000} defaultValue={advancedSearch.yearTo ?? ""} /></label>
                        <label className="advanced-wide">Work types (comma separated)<input name="workTypes" defaultValue={(advancedSearch.workTypes ?? []).join(", ")} placeholder="journal-article, preprint" /></label>
                        <label>Added from<input name="addedFrom" type="date" defaultValue={advancedSearch.addedFrom ?? ""} /></label>
                        <label>Added to<input name="addedTo" type="date" defaultValue={advancedSearch.addedTo ?? ""} /></label>
                        <label>Modified from<input name="modifiedFrom" type="date" defaultValue={advancedSearch.modifiedFrom ?? ""} /></label>
                        <label>Modified to<input name="modifiedTo" type="date" defaultValue={advancedSearch.modifiedTo ?? ""} /></label>
                        <label>PDF<select name="hasPdf" defaultValue={advancedSearch.hasPdf === undefined ? "" : String(advancedSearch.hasPdf)}><option value="">Any</option><option value="true">Has PDF</option><option value="false">No PDF</option></select></label>
                        <label>Document<select name="hasDocument" defaultValue={advancedSearch.hasDocument === undefined ? "" : String(advancedSearch.hasDocument)}><option value="">Any</option><option value="true">Has document</option><option value="false">No document</option></select></label>
                        <label>User file<select name="hasAsset" defaultValue={advancedSearch.hasAsset === undefined ? "" : String(advancedSearch.hasAsset)}><option value="">Any</option><option value="true">Has file</option><option value="false">No file</option></select></label>
                        <label>Sort by<select name="sort" defaultValue={advancedSearch.sort ?? "ADDED"}><option value="ADDED">Date added</option><option value="MODIFIED">Date modified</option><option value="TITLE">Title</option><option value="AUTHOR">Author</option><option value="YEAR">Publication year</option></select></label>
                        <label>Direction<select name="direction" defaultValue={advancedSearch.direction ?? "DESC"}><option value="DESC">Descending</option><option value="ASC">Ascending</option></select></label>
                      </div>
                      <fieldset>
                        <legend>Metadata source</legend>
                        {["CROSSREF", "OPENALEX", "ARXIV", "ZOTERO", "UNDEFINED"].map((source) => (
                          <label key={source}><input type="checkbox" name="metadataSources" value={source} defaultChecked={(advancedSearch.metadataSources ?? []).includes(source)} />{source}</label>
                        ))}
                      </fieldset>
                      {orderedCollections.length > 0 && (
                        <fieldset>
                          <legend>Collections (match any)</legend>
                          <div className="advanced-option-list">
                            {orderedCollections.map((collection) => (
                              <label key={collection.collection_id} style={{ paddingLeft: `${collectionDepth(collection, collections) * 14}px` }}>
                                <input type="checkbox" name="collectionIds" value={collection.collection_id} defaultChecked={(advancedSearch.collectionIds ?? []).includes(collection.collection_id)} />{collection.name}
                              </label>
                            ))}
                          </div>
                          <label><input type="checkbox" name="includeSubcollections" defaultChecked={advancedSearch.includeSubcollections ?? false} /> Include descendant Collections</label>
                        </fieldset>
                      )}
                      {tags.length > 0 && (
                        <fieldset>
                          <legend>Tags</legend>
                          <div className="advanced-option-list">
                            {tags.map((tag) => (
                              <label key={tag.tag_id}><input type="checkbox" name="tagIds" value={tag.tag_id} defaultChecked={(advancedSearch.tagIds ?? []).includes(tag.tag_id)} />{tag.name}</label>
                            ))}
                          </div>
                          <label>Match <select name="tagMode" defaultValue={advancedSearch.tagMode ?? "ANY"}><option value="ANY">any selected tag</option><option value="ALL">all selected tags</option></select></label>
                        </fieldset>
                      )}
                      <div className="advanced-search-actions">
                        <button type="button" onClick={clearAdvancedSearch}>Clear</button>
                        <button className="primary" type="submit">Apply filters</button>
                      </div>
                    </form>
                  )}
                  <JobActivityPanel activities={activities} onClear={clearFinishedActivities} />
                  {items.length > 0 && selected.role !== "VIEWER" && (
                    <div className="zotero-bulkbar">
                      <label>
                        <input
                          type="checkbox"
                          checked={nextCursor === null && visibleLibraryItems.length > 0 && selectedItemIds.length === visibleLibraryItems.length}
                          onChange={(event) => event.target.checked ? void selectAllResults() : setSelectedItemIds([])}
                        />
                        {selectedItemIds.length} selected
                      </label>
                      <select value={bulkAction} onChange={(event) => { setBulkAction(event.target.value as BulkAction); setBulkTargetId(""); }}>
                        {catalogueStatus === "ACTIVE" ? (
                          <>
                            <option value="ADD_COLLECTION">Add to Collection</option>
                            <option value="REMOVE_COLLECTION">Remove from Collection</option>
                            <option value="ADD_TAG">Add tag</option>
                            <option value="REMOVE_TAG">Remove tag</option>
                            <option value="TRASH">Move to trash</option>
                          </>
                        ) : <option value="RESTORE">Restore</option>}
                      </select>
                      {bulkAction.includes("COLLECTION") && (
                        <select value={bulkTargetId} onChange={(event) => setBulkTargetId(event.target.value)}>
                          <option value="">Choose Collection…</option>
                          {orderedCollections.map((collection) => <option key={collection.collection_id} value={collection.collection_id}>{collection.name}</option>)}
                        </select>
                      )}
                      {bulkAction.includes("TAG") && (
                        <select value={bulkTargetId} onChange={(event) => setBulkTargetId(event.target.value)}>
                          <option value="">Choose tag…</option>
                          {tags.map((tag) => <option key={tag.tag_id} value={tag.tag_id}>{tag.name}</option>)}
                        </select>
                      )}
                      <button disabled={busy || selectedItemIds.length === 0} onClick={() => void applyBulkAction()}>Apply</button>
                      {catalogueStatus === "ACTIVE" && <button disabled={busy || selectedItemIds.length === 0} onClick={() => void refreshSelectedMetadata()}>Refresh metadata</button>}
                    </div>
                  )}
                  <div
                    className="zotero-table"
                    role="table"
                    aria-label="Library items"
                    onScroll={(event) => loadMoreAtTableEnd(event.currentTarget)}
                    onWheel={(event) => {
                      if (event.deltaY > 0) loadMoreAtTableEnd(event.currentTarget);
                    }}
                  >
                    <div className="zotero-table-head" role="row">
                      <span />
                      <span>Title</span>
                      <span>Creator</span>
                      <span>Year</span>
                      <span>Publication</span>
                      <span>PDF</span>
                    </div>
                    {visibleLibraryItems.map((item) => {
                      const expanded = expandedItemIds.includes(item.library_item_id);
                      const authors = item.effective_metadata.authors ?? [];
                      const resources = resourcesByItem[item.library_item_id];
                      const documentsExpanded = expandedDocumentIds.includes(item.library_item_id);
                      const summary = item.resource_summary;
                      const resourceCount = summary
                        ? summary.primary_pdf + summary.extracted_text + summary.documents + summary.assets
                        : Number(Boolean(item.pdf_attachment)) + item.asset_attachments.length;
                      const loadedCount = resources
                        ? Number(Boolean(resources.primary_pdf)) +
                          resources.documents.length +
                          resources.canonical_attachments.filter(
                            (value) => value.artifact_type === "EXTRACTED_TEXT",
                          ).length +
                          resources.assets.length
                        : 0;
                      const hasChildren = resourceCount > 0 || loadedCount > 0;
                      const textArtifacts = resources?.canonical_attachments.filter(
                        (value) => value.artifact_type === "EXTRACTED_TEXT",
                      ) ?? [];
                      return (
                        <div className="zotero-item-group" key={item.library_item_id}>
                          <div
                            className={`zotero-table-row ${inspectedItemId === item.library_item_id ? "selected" : ""}`}
                            role="row"
                            tabIndex={0}
                            onClick={() => {
                              setInspectedItemId(item.library_item_id);
                              if (!resourcesByItem[item.library_item_id]) void loadItemResources(item.library_item_id);
                            }}
                            onKeyDown={(event) => {
                              if (event.key === "Enter") {
                                setInspectedItemId(item.library_item_id);
                                if (!resourcesByItem[item.library_item_id]) void loadItemResources(item.library_item_id);
                              }
                            }}
                          >
                            <span className="zotero-row-controls">
                              {selected.role !== "VIEWER" && <input type="checkbox" checked={selectedItemIds.includes(item.library_item_id)} onClick={(event) => event.stopPropagation()} onChange={(event) => setSelectedItemIds((current) => event.target.checked ? [...current, item.library_item_id] : current.filter((value) => value !== item.library_item_id))} />}
                              <button
                                disabled={!hasChildren}
                                onClick={(event) => {
                                  event.stopPropagation();
                                  toggleItemResources(item.library_item_id);
                                }}
                              >
                                {hasChildren ? (expanded ? "⌄" : "›") : ""}
                              </button>
                            </span>
                            <strong title={item.effective_metadata.title}>{item.effective_metadata.title}</strong>
                            <span>{authors[0]?.name ?? "—"}</span>
                            <span>{item.effective_metadata.publication_year ?? "—"}</span>
                            <span>{item.effective_metadata.venue ?? "—"}</span>
                            <span className={item.pdf_attachment ? "pdf-present" : "pdf-missing"}>{item.pdf_attachment ? "PDF" : "—"}</span>
                          </div>
                          {expanded && (
                            <div className="zotero-resource-rows">
                              {resourceLoadingIds.includes(item.library_item_id) && !resources && (
                                <div className="resource-loading">Loading resources…</div>
                              )}
                              {resources?.primary_pdf && (
                                <div className="resource-tree-row">
                                  <button className="resource-main" onClick={() => openArtifact(item, resources.primary_pdf!)}>
                                    <span className="resource-icon pdf">PDF</span>
                                    <strong>{resources.primary_pdf.filename ?? "Primary PDF"}</strong>
                                    <small>{resources.primary_pdf.origin.toLowerCase()}</small>
                                  </button>
                                  {selected.role !== "VIEWER" && resources.primary_pdf.origin === "OVERRIDE" && (
                                    <button className="resource-row-action" disabled={busy} onClick={() => void cancelItemPdfOverride(item, resources.primary_pdf!)}>Use canonical</button>
                                  )}
                                </div>
                              )}
                              {textArtifacts.map((resource) => (
                                <div className="resource-tree-row" key={resource.artifact_key}>
                                  <button className="resource-main" onClick={() => openArtifact(item, resource)}>
                                    <span className="resource-icon text">TXT</span>
                                    <strong>{resource.filename ?? "PDF text"}</strong>
                                    <small>{resource.status.toLowerCase()}</small>
                                  </button>
                                </div>
                              ))}
                              {resources && resources.documents.length > 0 && (
                                <div className="document-tree-group">
                                  <button
                                    className="document-folder-row"
                                    onClick={() => setExpandedDocumentIds((current) =>
                                      documentsExpanded
                                        ? current.filter((value) => value !== item.library_item_id)
                                        : [...current, item.library_item_id]
                                    )}
                                  >
                                    <span>{documentsExpanded ? "⌄" : "›"}</span>
                                    <span className="resource-icon document">DOC</span>
                                    <strong>Documents</strong>
                                    <small>{resources.documents.length}</small>
                                  </button>
                                  {documentsExpanded && (
                                    <div className="document-tree-children">
                                      {resources.documents.map((resource) => (
                                        <button key={resource.artifact_key} className="resource-main" onClick={() => openArtifact(item, resource)}>
                                          <span className="resource-icon document">MD</span>
                                          <strong>{resource.filename ?? resource.artifact_key}</strong>
                                          <small>{resource.status.toLowerCase()}</small>
                                        </button>
                                      ))}
                                    </div>
                                  )}
                                </div>
                              )}
                              {resources?.assets.map((asset) => (
                                <div className="resource-tree-row" key={asset.asset_id}>
                                  <button className="resource-main" onClick={() => openAsset(item, asset)}>
                                    <span className="resource-icon user-file">FILE</span>
                                    <strong>{asset.filename}</strong>
                                    <small>user file</small>
                                  </button>
                                  {selected.role !== "VIEWER" && (
                                    <div className="resource-row-actions">
                                      <button disabled={busy} onClick={() => void renameItemAsset(item, asset)}>Rename</button>
                                      <button className="danger" disabled={busy} onClick={() => void deleteItemAsset(item, asset)}>Delete</button>
                                    </div>
                                  )}
                                </div>
                              ))}
                            </div>
                          )}
                        </div>
                      );
                    })}
                    {visibleLibraryItems.length === 0 && <div className="zotero-table-empty">No papers match this view.</div>}
                    {nextCursor && (
                      <div className="zotero-load-more-status" role="status">
                        {loadingMore ? "Loading more papers…" : "Scroll to load more papers"}
                      </div>
                    )}
                  </div>
                </div>

                <aside className="zotero-inspector">
                  {inspectedItem ? (
                    <>
                      <span className="eyebrow">ITEM DETAILS</span>
                      <h2>{inspectedItem.effective_metadata.title}</h2>
                      <p className="inspector-authors">{(inspectedItem.effective_metadata.authors ?? []).map((author) => author.name).filter(Boolean).join(", ") || "No creators recorded"}</p>
                      <dl>
                        <dt>Source</dt><dd>{inspectedItem.metadata_source}</dd>
                        <dt>Type</dt><dd>{inspectedItem.effective_metadata.work_type?.replaceAll("_", " ") ?? "—"}</dd>
                        <dt>Published</dt><dd>{inspectedItem.effective_metadata.publication_date ?? inspectedItem.effective_metadata.publication_year ?? "—"}</dd>
                        <dt>Publication</dt><dd>{inspectedItem.effective_metadata.venue ?? "—"}</dd>
                        <dt>Publisher</dt><dd>{inspectedItem.effective_metadata.publisher ?? "—"}</dd>
                        <dt>Volume / issue</dt><dd>{[inspectedItem.effective_metadata.volume, inspectedItem.effective_metadata.issue].filter(Boolean).join(" / ") || "—"}</dd>
                        <dt>Pages</dt><dd>{inspectedItem.effective_metadata.pages ?? inspectedItem.effective_metadata.article_number ?? "—"}</dd>
                        <dt>Language</dt><dd>{inspectedItem.effective_metadata.language ?? "—"}</dd>
                        <dt>ISSN</dt><dd>{inspectedItem.effective_metadata.issn?.join(", ") || "—"}</dd>
                        <dt>ISBN</dt><dd>{inspectedItem.effective_metadata.isbn?.join(", ") || "—"}</dd>
                        <dt>Identifiers</dt><dd>{inspectedItem.identifiers.map((value) => `${value.scheme}: ${value.value}`).join(" · ") || "—"}</dd>
                        <dt>Added</dt><dd>{new Date(inspectedItem.created_at).toLocaleString()}</dd>
                        <dt>Modified</dt><dd>{new Date(inspectedItem.updated_at).toLocaleString()}</dd>
                      </dl>
                      {inspectedItem.effective_metadata.canonical_url && (
                        <a className="inspector-url" href={inspectedItem.effective_metadata.canonical_url} target="_blank" rel="noreferrer">Open canonical webpage ↗</a>
                      )}
                      <div className="inspector-section">
                        <strong>Collections</strong>
                        <p>{collections.filter((collection) => inspectedItem.collection_ids.includes(collection.collection_id)).map((collection) => collection.name).join(", ") || "None"}</p>
                      </div>
                      <div className="inspector-section">
                        <strong>Tags</strong>
                        <p>{tags.filter((tag) => inspectedItem.tag_ids.includes(tag.tag_id)).map((tag) => tag.name).join(", ") || "None"}</p>
                      </div>
                      {inspectedResources && (
                        inspectedResources.primary_pdf ||
                        inspectedResources.documents.length > 0 ||
                        inspectedResources.canonical_attachments.some((value) => value.artifact_type === "EXTRACTED_TEXT") ||
                        inspectedResources.assets.length > 0
                      ) && (
                        <div className="inspector-section resources">
                          <strong>Resources</strong>
                          {inspectedResources.primary_pdf && (
                            <button onClick={() => openArtifact(inspectedItem, inspectedResources.primary_pdf!)}>
                              <span className="resource-icon pdf">PDF</span>
                              {inspectedResources.primary_pdf.filename ?? "Primary PDF"}
                            </button>
                          )}
                          {inspectedResources.canonical_attachments.filter((value) => value.artifact_type === "EXTRACTED_TEXT").map((resource) => (
                            <button key={resource.artifact_key} onClick={() => openArtifact(inspectedItem, resource)}>
                              <span className="resource-icon text">TXT</span>
                              {resource.filename ?? "PDF text"}
                            </button>
                          ))}
                          {inspectedResources.documents.map((resource) => (
                            <button key={resource.artifact_key} onClick={() => openArtifact(inspectedItem, resource)}>
                              <span className="resource-icon document">MD</span>
                              {resource.filename ?? resource.artifact_key}
                            </button>
                          ))}
                          {inspectedResources.assets.map((asset) => (
                            <button key={asset.asset_id} onClick={() => openAsset(inspectedItem, asset)}>
                              <span className="resource-icon user-file">FILE</span>
                              {asset.filename}
                            </button>
                          ))}
                        </div>
                      )}
                      {selected.role !== "VIEWER" && inspectedItem.status === "ACTIVE" && (
                        <div className="inspector-resource-actions">
                          <label className="secondary-button">
                            {inspectedResources?.primary_pdf ? "Replace PDF" : "Add PDF"}
                            <input type="file" accept=".pdf,application/pdf" disabled={busy} onChange={(event) => void uploadItemPdf(inspectedItem, event)} />
                          </label>
                          <label className="secondary-button">
                            Add user files
                            <input type="file" multiple disabled={busy} onChange={(event) => void uploadItemAsset(inspectedItem, event)} />
                          </label>
                          {inspectedResources?.primary_pdf?.origin === "OVERRIDE" && (
                            <button className="resource-cancel" disabled={busy} onClick={() => void cancelItemPdfOverride(inspectedItem, inspectedResources.primary_pdf!)}>Use canonical PDF</button>
                          )}
                        </div>
                      )}
                      <div className="inspector-actions">
                        <button className="secondary-button" onClick={() => setSelectedItem(inspectedItem)}>Edit item</button>
                        {selected.role !== "VIEWER" && inspectedItem.status === "ACTIVE" && <button className="danger-button" disabled={busy} onClick={() => void trashPaper(inspectedItem)}>Move to trash</button>}
                        {selected.role !== "VIEWER" && inspectedItem.status === "TRASHED" && <button className="secondary-button" disabled={busy} onClick={() => void restorePaper(inspectedItem)}>Restore</button>}
                      </div>
                    </>
                  ) : (
                    <div className="inspector-empty"><span>↖</span><p>Select a paper to inspect its metadata and resources.</p></div>
                  )}
                </aside>
              </section>
            ) : (
            <>
            <section className="catalogue-section">
              <div className="catalogue-toolbar">
                <div className="collection-strip" aria-label="Collection filter">
                  <button
                    className={!activeCollectionId ? "active" : ""}
                    onClick={() => setActiveCollectionId("")}
                  >
                    All papers
                  </button>
                  {orderedCollections.map((collection) => (
                    <button
                      className={collection.collection_id === activeCollectionId ? "active" : ""}
                      key={collection.collection_id}
                      onClick={() => setActiveCollectionId(collection.collection_id)}
                    >
                      {collection.parent_collection_id && <small>↳</small>}
                      {collection.name}
                      {catalogueStatus === "ACTIVE" && <span>{collection.item_count}</span>}
                    </button>
                  ))}
                </div>
                <div className="catalogue-actions">
                  <div className="status-switch" aria-label="Catalogue status">
                    <button
                      className={catalogueStatus === "ACTIVE" ? "active" : ""}
                      onClick={() => {
                        setCatalogueStatus("ACTIVE");
                        setBulkAction("ADD_COLLECTION");
                        setBulkTargetId("");
                      }}
                    >
                      Active
                    </button>
                    <button
                      className={catalogueStatus === "TRASHED" ? "active" : ""}
                      onClick={() => {
                        setCatalogueStatus("TRASHED");
                        setBulkAction("RESTORE");
                        setBulkTargetId("");
                      }}
                    >
                      Trash
                    </button>
                  </div>
                  {activeCollection && selected.role !== "VIEWER" && (
                    <button
                      className="secondary-button"
                      onClick={() => setManageCollectionOpen(true)}
                    >
                      Manage Collection
                    </button>
                  )}
                  {selected.role !== "VIEWER" && catalogueStatus === "ACTIVE" && (
                    <>
                      <button
                        className="quiet-button"
                        onClick={() => setAddPaperOpen(true)}
                      >
                        Manual add
                      </button>
                      <label className="secondary-button citation-import-button">
                        Import citations
                        <input
                          type="file"
                          accept=".bib,.ris,.json,application/x-bibtex,application/x-research-info-systems,application/json"
                          disabled={busy}
                          onChange={(event) => void importCitations(event)}
                        />
                      </label>
                      <button
                        className="secondary-button"
                        disabled={busy}
                        title="Choose the Zotero folder containing zotero.sqlite and storage"
                        onClick={() => void selectZoteroFolder()}
                      >
                        Import Zotero folder
                      </button>
                      <label className="primary-button pdf-import-button">
                        Import PDF
                        <input
                          type="file"
                          accept=".pdf,application/pdf"
                          multiple
                          disabled={busy}
                          onChange={(event) => void importPdfs(event)}
                        />
                      </label>
                    </>
                  )}
                </div>
              </div>
              <JobActivityPanel activities={activities} onClear={clearFinishedActivities} />
              {selected.role !== "VIEWER" && (
                <form className="new-collection" onSubmit={createCollection}>
                  <input
                    name="name"
                    placeholder={
                      activeCollection
                        ? `New child of ${activeCollection.name}`
                        : "New top-level Collection"
                    }
                    maxLength={200}
                    disabled={busy}
                  />
                  <button disabled={busy}>Create collection</button>
                </form>
              )}
              <div className="tag-toolbar">
                <div className="tag-strip" aria-label="Tag filter">
                  <button
                    className={!activeTagId ? "active" : ""}
                    onClick={() => setActiveTagId("")}
                  >
                    All tags
                  </button>
                  {tags.map((tag) => (
                    <span className={`tag-filter ${tag.tag_id === activeTagId ? "active" : ""}`} key={tag.tag_id}>
                      <button onClick={() => setActiveTagId(tag.tag_id)}>
                        {tag.name}
                        {catalogueStatus === "ACTIVE" && <small>{tag.item_count}</small>}
                      </button>
                      {selected.role !== "VIEWER" && (
                        <button
                          className="tag-delete"
                          aria-label={`Delete tag ${tag.name}`}
                          onClick={() => void deleteTag(tag)}
                        >
                          ×
                        </button>
                      )}
                    </span>
                  ))}
                </div>
                {selected.role !== "VIEWER" && (
                  <form className="new-tag" onSubmit={createTag}>
                    <input name="name" placeholder="New tag" maxLength={100} disabled={busy} />
                    <button disabled={busy}>+</button>
                  </form>
                )}
              </div>
              {items.length > 0 && selected.role !== "VIEWER" && (
                <div className="bulk-toolbar">
                  <label>
                    <input
                      type="checkbox"
                      checked={
                        nextCursor === null &&
                        visibleLibraryItems.length > 0 &&
                        selectedItemIds.length === visibleLibraryItems.length
                      }
                      onChange={(event) => {
                        if (event.target.checked) void selectAllResults();
                        else setSelectedItemIds([]);
                      }}
                    />
                    {selectedItemIds.length} selected
                  </label>
                  <select
                    value={bulkAction}
                    onChange={(event) => {
                      setBulkAction(event.target.value as BulkAction);
                      setBulkTargetId("");
                    }}
                  >
                    {catalogueStatus === "ACTIVE" ? (
                      <>
                        <option value="ADD_COLLECTION">Add to Collection</option>
                        <option value="REMOVE_COLLECTION">Remove from Collection</option>
                        <option value="ADD_TAG">Add tag</option>
                        <option value="REMOVE_TAG">Remove tag</option>
                        <option value="TRASH">Move to trash</option>
                      </>
                    ) : (
                      <option value="RESTORE">Restore</option>
                    )}
                  </select>
                  {bulkAction.includes("COLLECTION") && (
                    <select value={bulkTargetId} onChange={(event) => setBulkTargetId(event.target.value)}>
                      <option value="">Choose Collection…</option>
                      {orderedCollections.map((collection) => (
                        <option key={collection.collection_id} value={collection.collection_id}>
                          {collection.name}
                        </option>
                      ))}
                    </select>
                  )}
                  {bulkAction.includes("TAG") && (
                    <select value={bulkTargetId} onChange={(event) => setBulkTargetId(event.target.value)}>
                      <option value="">Choose tag…</option>
                      {tags.map((tag) => (
                        <option key={tag.tag_id} value={tag.tag_id}>{tag.name}</option>
                      ))}
                    </select>
                  )}
                  <button
                    className="secondary-button"
                    disabled={busy || selectedItemIds.length === 0}
                    onClick={() => void applyBulkAction()}
                  >
                    Apply
                  </button>
                  {catalogueStatus === "ACTIVE" && (
                    <>
                      <button
                        className="secondary-button"
                        disabled={busy || selectedItemIds.length === 0}
                        onClick={() => void refreshSelectedMetadata()}
                      >
                        Refresh metadata
                      </button>
                      <button
                        className="danger-button"
                        disabled={busy || selectedItemIds.length === 0}
                        onClick={() => void trashSelectedItems()}
                      >
                        Move to trash
                      </button>
                    </>
                  )}
                </div>
              )}
              {items.length ? (
                <>
                <div className="catalogue-grid">
                  {items.map((item) => {
                    const doi = item.identifiers.find((value) => value.scheme === "DOI");
                    const itemTags = tags.filter((tag) => item.tag_ids.includes(tag.tag_id));
                    const pdfImport = pdfImports.find(
                      (value) => value.itemId === item.library_item_id,
                    );
                    return (
                      <article
                        className="paper-card"
                        key={item.library_item_id}
                        tabIndex={0}
                        onClick={() => setSelectedItem(item)}
                        onKeyDown={(event) => {
                          if (event.key === "Enter") setSelectedItem(item);
                        }}
                      >
                        {selected.role !== "VIEWER" && (
                          <input
                            className="paper-select"
                            type="checkbox"
                            aria-label={`Select ${item.effective_metadata.title}`}
                            checked={selectedItemIds.includes(item.library_item_id)}
                            onClick={(event) => event.stopPropagation()}
                            onChange={(event) =>
                              setSelectedItemIds((current) =>
                                event.target.checked
                                  ? [...current, item.library_item_id]
                                  : current.filter((value) => value !== item.library_item_id),
                              )
                            }
                          />
                        )}
                        <div className="paper-card-meta">
                          <span>{item.effective_metadata.publication_year ?? "UNDATED"}</span>
                          <span>{item.metadata_source}</span>
                          <span className={item.pdf_attachment ? "pdf-present" : "pdf-missing"}>
                            {item.pdf_attachment
                              ? `PDF · ${item.pdf_attachment.origin}`
                              : "NO PDF"}
                          </span>
                        </div>
                        {pdfImport && pdfImport.phase !== "READY" && (
                          <span className={`import-state ${pdfImport.phase.toLowerCase()}`}>
                            {pdfImport.phase.replaceAll("_", " ")}
                          </span>
                        )}
                        <h2>{item.effective_metadata.title}</h2>
                        <p>{item.effective_metadata.venue || "No venue recorded"}</p>
                        {itemTags.length > 0 && (
                          <div className="paper-tags">
                            {itemTags.map((tag) => <span key={tag.tag_id}>{tag.name}</span>)}
                          </div>
                        )}
                        <div className="paper-identifiers">
                          <code>{doi ? `doi:${doi.value}` : "No persistent identifier"}</code>
                          <span>{item.collection_ids.length} collections</span>
                        </div>
                        {selected.role !== "VIEWER" && catalogueStatus === "ACTIVE" && (
                          <button
                            className="text-danger"
                            disabled={busy}
                            onClick={(event) => {
                              event.stopPropagation();
                              void trashPaper(item);
                            }}
                          >
                            Move to trash
                          </button>
                        )}
                        {selected.role !== "VIEWER" && catalogueStatus === "TRASHED" && (
                          <button
                            className="text-action"
                            disabled={busy}
                            onClick={(event) => {
                              event.stopPropagation();
                              void restorePaper(item);
                            }}
                          >
                            Restore paper
                          </button>
                        )}
                      </article>
                    );
                  })}
                </div>
                {nextCursor && (
                  <button
                    className="load-more"
                    disabled={busy}
                    onClick={() => void loadMoreItems()}
                  >
                    Load more papers
                  </button>
                )}
                </>
              ) : (
                <div className="catalogue-empty">
                  <span className="line-icon">◇</span>
                  <h2>
                    {catalogueStatus === "TRASHED"
                      ? "Trash is empty"
                      : activeCollectionId
                        ? "This collection is empty"
                        : "No papers yet"}
                  </h2>
                  <p>
                    {catalogueStatus === "TRASHED"
                      ? "Trashed papers retain their Collection placement for restoration."
                      : "Add a paper once, then place the same Library item in multiple Collections."}
                  </p>
                </div>
              )}
            </section>
            <section className="management-grid">
              <article className="panel">
                <div className="panel-heading"><h2>Members</h2><span>{members.length}</span></div>
                <div className="member-list">
                  {members.map((member) => (
                    <div className="member-row" key={member.principal_id}>
                      <span className="avatar">{member.display_name.slice(0, 1).toUpperCase()}</span>
                      <div><strong>{member.display_name}</strong><small>{member.principal_id.slice(0, 8)}</small></div>
                      {selected.role === "OWNER" &&
                      member.principal_id !== session?.principal.principal_id ? (
                        <div className="member-actions">
                          <select
                            aria-label={`Role for ${member.display_name}`}
                            value={member.role}
                            disabled={busy}
                            onChange={(event) =>
                              void updateMember(member, event.target.value as Member["role"])
                            }
                          >
                            <option>OWNER</option>
                            <option>EDITOR</option>
                            <option>VIEWER</option>
                          </select>
                          <button disabled={busy} onClick={() => void removeMember(member)}>
                            Remove
                          </button>
                        </div>
                      ) : (
                        <span className="role-label">{member.role}</span>
                      )}
                    </div>
                  ))}
                </div>
              </article>
              {selected.library_type === "GROUP" && selected.role === "OWNER" && (
                <article className="panel">
                  <div className="panel-heading"><h2>Invite member</h2><span>7 days</span></div>
                  <form className="invite-form" onSubmit={invite}>
                    <input type="email" name="email" placeholder="researcher@example.org" required />
                    <select name="role" defaultValue="VIEWER"><option>VIEWER</option><option>EDITOR</option></select>
                    <button className="primary-button" disabled={busy}>Create local invitation</button>
                  </form>
                  <div className="invitation-list">
                    {invitations.map((item) => (
                      <div className="invitation-row" key={item.invitation_id}>
                        <span>{item.email}</span>
                        <div className="invitation-actions">
                          <small>{item.role} · {item.status}</small>
                          {item.status !== "ACCEPTED" && (
                            <button
                              disabled={busy}
                              onClick={() => void regenerateInvitation(item)}
                            >
                              Regenerate
                            </button>
                          )}
                        </div>
                      </div>
                    ))}
                  </div>
                </article>
              )}
            </section>
            </>
            )}
          </>
        ) : <Centered label="No accessible Library" />}
      </main>
      {resourceReader && (
        <div className="resource-reader-backdrop" onClick={() => setResourceReader(null)}>
          <section className="resource-reader" onClick={(event) => event.stopPropagation()}>
            <header>
              <div>
                <strong>{resourceReader.title}</strong>
                <small>{resourceReader.mediaType}</small>
              </div>
              <div>
                <a href={resourceReader.url} target="_blank" rel="noreferrer">Open separately ↗</a>
                <button onClick={() => setResourceReader(null)}>Close</button>
              </div>
            </header>
            <iframe title={resourceReader.title} src={resourceReader.url} />
          </section>
        </div>
      )}
      {selectedItem && selected && (
        <div className="modal-backdrop" onClick={() => setSelectedItem(null)}>
          <section
            className="invitation-modal paper-modal"
            role="dialog"
            aria-modal="true"
            aria-labelledby="paper-detail-title"
            onClick={(event) => event.stopPropagation()}
          >
            <span className="eyebrow">
              LIBRARY ITEM · {selectedItem.metadata_source}
            </span>
            <h2 id="paper-detail-title">Paper details</h2>
            <p>
              Changes here are local Library overrides. They do not rewrite the canonical metadata
              used by another Library.
            </p>
            <div className={`paper-resource ${selectedItem.pdf_attachment ? "available" : "missing"}`}>
              <span>Primary PDF</span>
              <strong>
                {selectedItem.pdf_attachment?.filename ||
                  (selectedItem.pdf_attachment ? "PDF available" : "No PDF attached")}
              </strong>
              <small>
                {selectedItem.pdf_attachment
                  ? `Effective source: ${selectedItem.pdf_attachment.origin}`
                  : "Neither this Library Item nor its canonical paper has a PDF."}
              </small>
            </div>
            <form className="paper-form" onSubmit={updatePaper}>
              <MetadataFields
                metadata={selectedItem.effective_metadata}
                doi={displayedDoi(selectedItem)}
                readOnly={selected.role === "VIEWER" || selectedItem.status === "TRASHED"}
                createdAt={selectedItem.created_at}
                updatedAt={selectedItem.updated_at}
              />
              {collections.length > 0 && (
                <fieldset disabled={selected.role === "VIEWER" || selectedItem.status === "TRASHED"}>
                  <legend>Collection placement</legend>
                  <div className="collection-choices">
                    {orderedCollections.map((collection) => (
                      <label key={collection.collection_id}>
                        <input
                          type="checkbox"
                          name="collection_ids"
                          value={collection.collection_id}
                          defaultChecked={selectedItem.collection_ids.includes(
                            collection.collection_id,
                          )}
                        />
                        {collection.name}
                      </label>
                    ))}
                  </div>
                </fieldset>
              )}
              {tags.length > 0 && (
                <fieldset disabled={selected.role === "VIEWER" || selectedItem.status === "TRASHED"}>
                  <legend>Tags</legend>
                  <div className="collection-choices">
                    {tags.map((tag) => (
                      <label key={tag.tag_id}>
                        <input
                          type="checkbox"
                          name="tag_ids"
                          value={tag.tag_id}
                          defaultChecked={selectedItem.tag_ids.includes(tag.tag_id)}
                        />
                        {tag.name}
                      </label>
                    ))}
                  </div>
                </fieldset>
              )}
              <div className="canonical-note">
                <strong>Canonical baseline</strong>
                <span>{String(selectedItem.canonical_metadata.title ?? "Untitled")}</span>
              </div>
              <div className="modal-actions split-actions">
                {selected.role !== "VIEWER" && selectedItem.status === "TRASHED" ? (
                  <button
                    className="secondary-button"
                    type="button"
                    onClick={() => void restorePaper(selectedItem)}
                  >
                    Restore paper
                  </button>
                ) : <span />}
                <div>
                  <button
                    className="secondary-button"
                    type="button"
                    onClick={() => setSelectedItem(null)}
                  >
                    Close
                  </button>
                  {selected.role !== "VIEWER" && selectedItem.status === "ACTIVE" && (
                    <button className="primary-button" disabled={busy}>Save Library changes</button>
                  )}
                </div>
              </div>
            </form>
          </section>
        </div>
      )}
      {manageCollectionOpen && activeCollection && selected && (
        <div className="modal-backdrop" onClick={() => setManageCollectionOpen(false)}>
          <section
            className="invitation-modal collection-modal"
            role="dialog"
            aria-modal="true"
            aria-labelledby="manage-collection-title"
            onClick={(event) => event.stopPropagation()}
          >
            <span className="eyebrow">COLLECTION MANAGEMENT</span>
            <h2 id="manage-collection-title">Edit {activeCollection.name}</h2>
            <p>Deleting a Collection removes only its placement links, never its papers.</p>
            <form className="paper-form" onSubmit={updateCollection}>
              <label>
                Collection name
                <input name="name" required maxLength={200} defaultValue={activeCollection.name} />
              </label>
              <div className="modal-actions split-actions">
                <button
                  className="danger-button"
                  type="button"
                  disabled={busy}
                  onClick={() => void deleteCollection()}
                >
                  Delete Collection
                </button>
                <div>
                  <button
                    className="secondary-button"
                    type="button"
                    onClick={() => setManageCollectionOpen(false)}
                  >
                    Cancel
                  </button>
                  <button className="primary-button" disabled={busy}>Save name</button>
                </div>
              </div>
            </form>
          </section>
        </div>
      )}
      {addPaperOpen && selected && (
        <div className="modal-backdrop" onClick={() => setAddPaperOpen(false)}>
          <section
            className="invitation-modal paper-modal"
            role="dialog"
            aria-modal="true"
            aria-labelledby="add-paper-title"
            onClick={(event) => event.stopPropagation()}
          >
            <span className="eyebrow">LIBRARY CATALOGUE</span>
            <h2 id="add-paper-title">Add a paper</h2>
            <p>
              A persistent identifier reuses an existing canonical paper; this Library keeps its
              own item and collection placement.
            </p>
            <form className="paper-form" onSubmit={createPaper}>
              <MetadataFields manual />
              {collections.length > 0 && (
                <fieldset>
                  <legend>Place in collections</legend>
                  <div className="collection-choices">
                    {orderedCollections.map((collection) => (
                      <label key={collection.collection_id}>
                        <input
                          type="checkbox"
                          name="collection_ids"
                          value={collection.collection_id}
                          defaultChecked={collection.collection_id === activeCollectionId}
                        />
                        {collection.name}
                      </label>
                    ))}
                  </div>
                </fieldset>
              )}
              {tags.length > 0 && (
                <fieldset>
                  <legend>Apply tags</legend>
                  <div className="collection-choices">
                    {tags.map((tag) => (
                      <label key={tag.tag_id}>
                        <input
                          type="checkbox"
                          name="tag_ids"
                          value={tag.tag_id}
                          defaultChecked={tag.tag_id === activeTagId}
                        />
                        {tag.name}
                      </label>
                    ))}
                  </div>
                </fieldset>
              )}
              <div className="modal-actions">
                <button
                  className="secondary-button"
                  type="button"
                  onClick={() => setAddPaperOpen(false)}
                >
                  Cancel
                </button>
                <button className="primary-button" disabled={busy}>Add to Library</button>
              </div>
            </form>
          </section>
        </div>
      )}
      {invitationLink && (
        <div className="modal-backdrop" onClick={() => setInvitationLink("")}>
          <section
            className="invitation-modal"
            role="dialog"
            aria-modal="true"
            aria-labelledby="invitation-link-title"
            onClick={(event) => event.stopPropagation()}
          >
            <span className="eyebrow">LOCAL DEVELOPMENT INVITATION</span>
            <h2 id="invitation-link-title">Share this invitation link</h2>
            <p>The invited user must sign in with the matching verified email address.</p>
            <input aria-label="Invitation link" readOnly value={invitationLink} />
            <div className="modal-actions">
              <button className="secondary-button" onClick={() => setInvitationLink("")}>
                Close
              </button>
              <button className="primary-button" onClick={() => void copyInvitationLink()}>
                Copy link
              </button>
            </div>
          </section>
        </div>
      )}
    </div>
  );
}

function Centered({ label }: { label: string }) {
  return <main className="centered"><span className="spinner" />{label}</main>;
}
