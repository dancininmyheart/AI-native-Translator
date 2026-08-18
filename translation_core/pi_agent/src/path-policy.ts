import path from "node:path";
import { lstat, realpath } from "node:fs/promises";
import { SUPPORTED_EXTENSIONS } from "./contracts.js";

const SAFE_ID = /^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/;

export function assertSafeId(value: string, label = "id"): string {
  if (!SAFE_ID.test(value)) throw new Error(`${label} is not a safe identifier`);
  return value;
}

export function assertSupportedFilename(filename: string): string {
  const base = path.basename(filename);
  if (
    base !== filename ||
    base === "." ||
    base === ".." ||
    filename.length > 255 ||
    /[\\/\u0000-\u001f\u007f]/.test(filename)
  ) {
    throw new Error("filename must be a safe basename");
  }
  const extension = path.extname(base).toLowerCase();
  if (!SUPPORTED_EXTENSIONS.includes(extension as (typeof SUPPORTED_EXTENSIONS)[number])) {
    throw new Error("only DOC, DOCX, PPT, and PPTX inputs are supported");
  }
  return base;
}

export function resolveContained(root: string, relativeValue: string): string {
  if (!relativeValue || path.isAbsolute(relativeValue) || relativeValue.includes("\0")) {
    throw new Error("path must be a non-empty relative path");
  }
  const resolvedRoot = path.resolve(root);
  const resolved = path.resolve(resolvedRoot, relativeValue);
  const relative = path.relative(resolvedRoot, resolved);
  if (relative.startsWith("..") || path.isAbsolute(relative)) throw new Error("path escapes the configured root");
  return resolved;
}

export async function resolveExistingContained(root: string, relativeValue: string): Promise<string> {
  const candidate = resolveContained(root, relativeValue);
  const [realRoot, realCandidate] = await Promise.all([realpath(root), realpath(candidate)]);
  const relative = path.relative(realRoot, realCandidate);
  if (relative.startsWith("..") || path.isAbsolute(relative)) throw new Error("symlink escapes the configured root");
  const stat = await lstat(candidate);
  if (stat.isSymbolicLink() || !stat.isFile()) throw new Error("source must be a regular file, not a symlink");
  return realCandidate;
}

export function safeArtifactPath(jobRoot: string, jobId: string, relpath: string): string {
  assertSafeId(jobId, "job_id");
  return resolveContained(path.join(jobRoot, jobId), relpath);
}
