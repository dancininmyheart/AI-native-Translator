import path from "node:path";
import { mkdir, readFile, stat, writeFile } from "node:fs/promises";
import { pathToFileURL } from "node:url";
import {
  MAX_PUBLIC_TRACE_JSON_BYTES,
  formatPublicTraceReplay,
  parsePublicTraceJson,
  projectP1aDemoToPublicTraces,
} from "./public-trace.js";

export interface PublicTraceCliIo {
  stdout(text: string): void;
  stderr(text: string): void;
}

const PROCESS_IO: PublicTraceCliIo = {
  stdout: (text) => process.stdout.write(text),
  stderr: (text) => process.stderr.write(text),
};

export async function runPublicTraceCli(argv: readonly string[], io: PublicTraceCliIo = PROCESS_IO): Promise<number> {
  const [command, inputPath] = argv;
  if (command === "project-p1a" && inputPath && argv[2] && (argv.length === 3 || argv.length === 4)) {
    const generatedAt = argv[3] ?? new Date().toISOString();
    const rawText = await readBoundedFile(inputPath, MAX_PROJECTION_INPUT_BYTES);
    let raw: unknown;
    try {
      raw = JSON.parse(rawText) as unknown;
    } catch {
      throw new Error("P1-A demo trace is not valid JSON");
    }
    const traces = projectP1aDemoToPublicTraces(raw, generatedAt);
    const outputDirectory = path.resolve(argv[2]);
    await mkdir(outputDirectory, { recursive: true });
    for (const trace of traces) {
      const outputPath = path.join(outputDirectory, `${trace.trace_id}.json`);
      await writeFile(outputPath, `${JSON.stringify(trace, null, 2)}\n`, { encoding: "utf8", mode: 0o600 });
      io.stdout(`WROTE ${outputPath}\n`);
    }
    return 0;
  }
  if ((command !== "replay" && command !== "validate") || !inputPath || argv.length !== 2) {
    io.stderr(
      "Usage:\n" +
      "  public-trace-cli <replay|validate> <public-trace.json>\n" +
      "  public-trace-cli project-p1a <demo-trace.json> <output-directory> [generated-at]\n",
    );
    return 2;
  }
  const trace = parsePublicTraceJson(await readBoundedFile(inputPath, MAX_PUBLIC_TRACE_JSON_BYTES));
  if (command === "validate") {
    io.stdout(`VALID ${trace.trace_id} schema_version=${trace.schema_version}\n`);
  } else {
    io.stdout(formatPublicTraceReplay(trace));
  }
  return 0;
}

const MAX_PROJECTION_INPUT_BYTES = 4 * 1_048_576;

async function readBoundedFile(filePath: string, maximumBytes: number): Promise<string> {
  const file = await stat(filePath);
  if (!file.isFile()) throw new Error("public trace input must be a regular file");
  if (file.size > maximumBytes) {
    throw new Error(`trace JSON exceeds the ${maximumBytes} byte size limit`);
  }
  return readFile(filePath, "utf8");
}

async function main(): Promise<void> {
  try {
    process.exitCode = await runPublicTraceCli(process.argv.slice(2));
  } catch (error) {
    const message = error instanceof Error ? error.message : "public trace command failed";
    process.stderr.write(`ERROR ${message}\n`);
    process.exitCode = 1;
  }
}

const invokedAsScript = process.argv[1]
  ? pathToFileURL(path.resolve(process.argv[1])).href === import.meta.url
  : false;
if (invokedAsScript) void main();
