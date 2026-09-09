const KNOWLEDGE_URI_RE = /^knowledge:\/\/[A-Za-z0-9][A-Za-z0-9._-]{0,95}(?:\/[A-Za-z0-9][A-Za-z0-9._:-]{0,159})+$/;
const OPAQUE_ID_RE = /^[A-Za-z0-9._:-]{1,160}$/;
const UNSAFE_TEXT_RE = /(?:password|api[_ -]?key|secret|token|authorization|cookie|private[_ -]?key|path)\s*[:=]|(?:https?:\/\/|file:|(?:^|[^A-Za-z0-9_])[A-Za-z]:[\\/]|\\\\|(?:^|[\s(=])\/(?:[^\s]+)|(?:^|[\s(])~\/)/i;
const LOCATOR_KEYS = new Set(["page", "line_start", "line_end", "section", "chunk_id"]);

export function isPortableEvidenceQuote(value: unknown): value is string {
  return typeof value === "string"
    && value.length <= 1200
    && !/[\u0000-\u0008\u000b\u000c\u000e-\u001f]/.test(value)
    && !UNSAFE_TEXT_RE.test(value);
}

function isPortableEvidenceLocatorValue(value: unknown): boolean {
  if (typeof value === "string") {
    return value.length > 0 && value.length <= 256
      && !/[\u0000-\u0008\u000b\u000c\u000e-\u001f]/.test(value)
      && !UNSAFE_TEXT_RE.test(value);
  }
  if (typeof value === "number") return Number.isFinite(value);
  return typeof value === "boolean";
}

export type PortableEvidence = {
  asset_id: string;
  resource_uri: string;
  locator?: Record<string, string | number | boolean>;
  quote?: string;
  revision?: string;
  score?: number;
  matched_by?: string[];
};

export function isPortableEvidence(value: unknown): value is PortableEvidence {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const evidence = value as Record<string, unknown>;
  if (typeof evidence.asset_id !== "string" || !OPAQUE_ID_RE.test(evidence.asset_id)) return false;
  if (typeof evidence.resource_uri !== "string" || !KNOWLEDGE_URI_RE.test(evidence.resource_uri)) return false;
  if (evidence.quote !== undefined && !isPortableEvidenceQuote(evidence.quote)) return false;
  if (evidence.revision !== undefined && (typeof evidence.revision !== "string" || !/^sha256:[0-9a-f]{64}$/.test(evidence.revision))) return false;
  if (evidence.score !== undefined && (typeof evidence.score !== "number" || evidence.score < 0 || evidence.score > 1)) return false;
  if (evidence.locator !== undefined && (!evidence.locator || typeof evidence.locator !== "object" || Array.isArray(evidence.locator))) return false;
  if (evidence.locator !== undefined && Object.entries(evidence.locator as Record<string, unknown>).some(([key, item]) => (
    !LOCATOR_KEYS.has(key) || !isPortableEvidenceLocatorValue(item)
  ))) return false;
  if (evidence.matched_by !== undefined && (!Array.isArray(evidence.matched_by) || evidence.matched_by.some((item) => typeof item !== "string" || !OPAQUE_ID_RE.test(item)))) return false;
  return true;
}
