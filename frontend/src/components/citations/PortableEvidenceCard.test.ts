import assert from "node:assert/strict";
import test from "node:test";

// @ts-ignore Node's native TypeScript runner requires the source suffix.
import { isPortableEvidence, isPortableEvidenceQuote } from "./portableEvidence.ts";

test("accepts portable Evidence for generic MCP knowledge Resources", () => {
  assert.equal(
    isPortableEvidence({
      asset_id: "query_result_qr_1",
      resource_uri: "knowledge://query-results/query_result_qr_1/artifact",
      locator: { chunk_id: "bytes:0-12" },
      revision: `sha256:${"a".repeat(64)}`,
      matched_by: ["query_result_artifact"],
    }),
    true,
  );
  assert.equal(
    isPortableEvidence({
      asset_id: "asset_1",
      resource_uri: "knowledge://spaces/space_1/assets/asset_1/derivatives/normalized_markdown",
    }),
    true,
  );
});

test("rejects non-knowledge and unsafe Evidence Resources", () => {
  assert.equal(
    isPortableEvidence({ asset_id: "asset_1", resource_uri: "file:///Users/pet/private.txt" }),
    false,
  );
  assert.equal(
    isPortableEvidence({
      asset_id: "asset_1",
      resource_uri: "knowledge://spaces/space_1/assets/asset_1",
      quote: "path=/Users/pet/private.txt",
    }),
    false,
  );
  assert.equal(
    isPortableEvidence({
      asset_id: "asset_1",
      resource_uri: "knowledge://spaces/space_1/assets/asset_1",
      quote: String.raw`source=C:\Users\pet\private.txt`,
    }),
    false,
  );
  assert.equal(
    isPortableEvidence({
      asset_id: "asset_1",
      resource_uri: "knowledge://spaces/space_1/assets/asset_1",
      quote: String.raw`source=\\server\share\private.txt`,
    }),
    false,
  );
  assert.equal(
    isPortableEvidence({
      asset_id: "asset_1",
      resource_uri: "knowledge://spaces/space_1/assets/asset_1",
      locator: { section: "/Users/pet/private.txt" },
    }),
    false,
  );
  assert.equal(
    isPortableEvidence({
      asset_id: "asset_1",
      resource_uri: "knowledge://spaces/space_1/assets/asset_1",
      locator: { section: "unsafe\u0000text" },
    }),
    false,
  );
});

test("portable quote filtering is reusable by legacy Source fallbacks", () => {
  assert.equal(isPortableEvidenceQuote("ordinary grounded text"), true);
  assert.equal(isPortableEvidenceQuote("https://example.test/article"), false);
  assert.equal(isPortableEvidenceQuote(String.raw`C:\Users\pet\private.txt`), false);
  assert.equal(isPortableEvidenceQuote(String.raw`\\server\share\private.txt`), false);
  assert.equal(isPortableEvidenceQuote("prefix=/srv/private.txt"), false);
  assert.equal(isPortableEvidenceQuote("unsafe\u0000text"), false);
});
