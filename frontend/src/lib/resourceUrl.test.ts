import assert from "node:assert/strict";
import test from "node:test";

// @ts-ignore Node's native TypeScript runner requires the source suffix.
import { rawKnowledgeFileUrl } from "./api.ts";

test("rawKnowledgeFileUrl only returns portable or explicitly allowed resource URLs", () => {
  assert.equal(
    rawKnowledgeFileUrl("/knowledge/spaces/space_1/assets/asset_1/image.png"),
    "/api/knowledge/file/raw?virtual_path=%2Fknowledge%2Fspaces%2Fspace_1%2Fassets%2Fasset_1%2Fimage.png",
  );
  assert.equal(rawKnowledgeFileUrl("https://example.test/image.png"), "https://example.test/image.png");
  assert.equal(rawKnowledgeFileUrl("assets/image.png"), "assets/image.png");
  for (const unsafe of [
    "javascript:alert(1)",
    "data:text/html,<script>alert(1)</script>",
    "//evil.example/image.png",
    "file:///Users/pet/private.txt",
    String.raw`C:\Users\pet\private.txt`,
    String.raw`\\server\share\private.txt`,
    "/Users/pet/private.txt",
    "~/private.txt",
  ]) {
    assert.equal(rawKnowledgeFileUrl(unsafe), "", unsafe);
  }
});
