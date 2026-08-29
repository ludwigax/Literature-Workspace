import type { CatalogueItem, PaperMetadata } from "./api";

const WORK_TYPES = [
  ["", "Unspecified"],
  ["JOURNAL_ARTICLE", "Journal article"],
  ["PREPRINT", "Preprint"],
  ["CONFERENCE_PAPER", "Conference paper"],
  ["REVIEW", "Review"],
  ["BOOK", "Book"],
  ["BOOK_CHAPTER", "Book chapter"],
  ["THESIS", "Thesis"],
  ["REPORT", "Report"],
  ["OTHER", "Other"],
] as const;

function textList(value: unknown): string {
  return Array.isArray(value) ? value.map(String).join(", ") : "";
}

function authorLines(value: PaperMetadata["authors"]): string {
  return (value ?? []).map((author) => author.name ?? "").filter(Boolean).join("\n");
}

export function displayedDoi(item: CatalogueItem): string {
  const doi = item.identifiers.find((value) => value.scheme === "DOI")?.value;
  if (doi) return doi;
  const arxiv = item.identifiers.find((value) => value.scheme === "ARXIV")?.value;
  return arxiv ? `10.48550/arXiv.${arxiv}` : "";
}

export function metadataFromForm(form: FormData): PaperMetadata {
  const text = (name: string) => String(form.get(name) ?? "").trim() || null;
  const number = (name: string) => {
    const value = String(form.get(name) ?? "").trim();
    return value ? Number(value) : null;
  };
  const list = (name: string) =>
    String(form.get(name) ?? "")
      .split(/[\n,;]/)
      .map((value) => value.trim())
      .filter(Boolean);
  const year = number("publication_year");
  const month = number("publication_month");
  const day = month ? number("publication_day") : null;
  const publicationDate = year && month && day
    ? `${year.toString().padStart(4, "0")}-${month.toString().padStart(2, "0")}-${day.toString().padStart(2, "0")}`
    : null;
  const authors = String(form.get("authors") ?? "")
    .split("\n")
    .map((value) => value.trim())
    .filter(Boolean)
    .map((name) => ({ name }));
  return {
    title: String(form.get("title") ?? "").trim(),
    abstract: text("abstract"),
    publication_year: year,
    publication_month: month,
    publication_day: day,
    publication_date: publicationDate,
    publication_date_precision: day ? "DAY" : month ? "MONTH" : year ? "YEAR" : null,
    work_type: text("work_type"),
    venue: text("venue"),
    canonical_url: text("canonical_url"),
    publisher: text("publisher"),
    volume: text("volume"),
    issue: text("issue"),
    pages: text("pages"),
    article_number: text("article_number"),
    language: text("language"),
    issn: list("issn"),
    isbn: list("isbn"),
    authors,
  };
}

type MetadataFieldsProps = {
  metadata?: PaperMetadata;
  doi?: string;
  manual?: boolean;
  readOnly?: boolean;
  createdAt?: string;
  updatedAt?: string;
};

export function MetadataFields({
  metadata = { title: "" },
  doi = "",
  manual = false,
  readOnly = false,
  createdAt,
  updatedAt,
}: MetadataFieldsProps) {
  return (
    <div className="metadata-fields">
      <label className="metadata-wide">
        Title
        <input name="title" required maxLength={10000} defaultValue={metadata.title} readOnly={readOnly} autoFocus={manual} />
      </label>
      <div className="paper-form-row metadata-three">
        <label>
          DOI
          <input name="doi" required={manual} placeholder="10.1000/example" defaultValue={doi} readOnly={!manual || readOnly} />
          <small>DataCite arXiv DOIs are recognized as arXiv identifiers.</small>
        </label>
        <label>
          Year
          <input name="publication_year" required={manual} type="number" min="1000" max="3000" defaultValue={metadata.publication_year ?? ""} readOnly={readOnly} />
        </label>
        <label>
          Type
          <select name="work_type" defaultValue={metadata.work_type ?? ""} disabled={readOnly}>
            {WORK_TYPES.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </select>
        </label>
      </div>
      <label className="metadata-wide">
        Authors <small>One author per line, in display order.</small>
        <textarea name="authors" rows={4} defaultValue={authorLines(metadata.authors)} readOnly={readOnly} />
      </label>
      <div className="paper-form-row metadata-three">
        <label>Month<input name="publication_month" type="number" min="1" max="12" defaultValue={metadata.publication_month ?? ""} readOnly={readOnly} /></label>
        <label>Day<input name="publication_day" type="number" min="1" max="31" defaultValue={metadata.publication_day ?? ""} readOnly={readOnly} /></label>
        <label>Language<input name="language" defaultValue={metadata.language ?? ""} readOnly={readOnly} /></label>
      </div>
      <div className="paper-form-row">
        <label>Publication / venue<input name="venue" defaultValue={metadata.venue ?? ""} readOnly={readOnly} /></label>
        <label>Publisher<input name="publisher" defaultValue={metadata.publisher ?? ""} readOnly={readOnly} /></label>
      </div>
      <div className="paper-form-row metadata-four">
        <label>Volume<input name="volume" defaultValue={metadata.volume ?? ""} readOnly={readOnly} /></label>
        <label>Issue<input name="issue" defaultValue={metadata.issue ?? ""} readOnly={readOnly} /></label>
        <label>Pages<input name="pages" defaultValue={metadata.pages ?? ""} readOnly={readOnly} /></label>
        <label>Article no.<input name="article_number" defaultValue={metadata.article_number ?? ""} readOnly={readOnly} /></label>
      </div>
      <div className="paper-form-row">
        <label>ISSN<input name="issn" defaultValue={textList(metadata.issn)} readOnly={readOnly} /></label>
        <label>ISBN<input name="isbn" defaultValue={textList(metadata.isbn)} readOnly={readOnly} /></label>
      </div>
      <label className="metadata-wide">URL<input name="canonical_url" type="url" defaultValue={metadata.canonical_url ?? ""} readOnly={readOnly} /></label>
      <label className="metadata-wide">Abstract<textarea className="metadata-abstract" name="abstract" rows={6} defaultValue={metadata.abstract ?? ""} readOnly={readOnly} /></label>
      {!manual && (createdAt || updatedAt) && (
        <div className="metadata-readonly">
          <span><small>Date added</small>{createdAt ? new Date(createdAt).toLocaleString() : "—"}</span>
          <span><small>Last modified</small>{updatedAt ? new Date(updatedAt).toLocaleString() : "—"}</span>
        </div>
      )}
    </div>
  );
}
