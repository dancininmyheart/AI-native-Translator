import path from "node:path";
import { mkdir, rm, symlink, writeFile } from "node:fs/promises";
import { afterEach, describe, expect, it } from "vitest";
import { assertSupportedFilename, resolveContained, resolveExistingContained } from "../src/path-policy.js";
import { testConfig } from "./helpers.js";

const cleanup: string[] = [];
afterEach(async () => Promise.all(cleanup.splice(0).map((value) => rm(value, { recursive: true, force: true }))));

describe("path policy", () => {
  it("allows only supported Office basenames", () => {
    expect(assertSupportedFilename("deck.PPTX")).toBe("deck.PPTX");
    expect(() => assertSupportedFilename("../deck.pptx")).toThrow(/basename/);
    expect(() => assertSupportedFilename("..\\deck.pptx")).toThrow(/basename/);
    expect(() => assertSupportedFilename("deck\r\n.pptx")).toThrow(/basename/);
    expect(() => assertSupportedFilename(`${"a".repeat(251)}.docx`)).toThrow(/basename/);
    expect(() => assertSupportedFilename("report.pdf")).toThrow(/only DOC/);
  });

  it("rejects traversal and escaping symlinks", async () => {
    const config = await testConfig();
    cleanup.push(config.dataRoot);
    const root = path.join(config.dataRoot, "registered");
    const outside = path.join(config.dataRoot, "outside.docx");
    await mkdir(root);
    await writeFile(outside, "outside");
    expect(() => resolveContained(root, "../outside.docx")).toThrow(/escapes/);
    const link = path.join(root, "link.docx");
    await symlink(outside, link, "file");
    await expect(resolveExistingContained(root, "link.docx")).rejects.toThrow(/escapes|symlink/);
  });
});
