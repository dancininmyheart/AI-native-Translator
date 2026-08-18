import http from "node:http";
import path from "node:path";
import { createReadStream } from "node:fs";
import { stat } from "node:fs/promises";
import { fileURLToPath } from "node:url";

const ROOT = path.dirname(fileURLToPath(import.meta.url));
const PORT = parsePort(process.argv[2] ?? "4173");
const MIME_TYPES = new Map([
  [".html", "text/html; charset=utf-8"],
  [".css", "text/css; charset=utf-8"],
  [".mjs", "text/javascript; charset=utf-8"],
  [".json", "application/json; charset=utf-8"],
]);

const server = http.createServer(async (request, response) => {
  try {
    if (request.method !== "GET" && request.method !== "HEAD") {
      respond(response, 405, "Method not allowed\n", { Allow: "GET, HEAD" });
      return;
    }
    const requestUrl = new URL(request.url ?? "/", "http://127.0.0.1");
    const pathname = decodeURIComponent(requestUrl.pathname === "/" ? "/index.html" : requestUrl.pathname);
    if (pathname.includes("\0")) throw new HttpError(400, "Invalid path");
    const filePath = path.resolve(ROOT, `.${pathname}`);
    const relative = path.relative(ROOT, filePath);
    if (relative.startsWith("..") || path.isAbsolute(relative)) throw new HttpError(404, "Not found");
    const metadata = await stat(filePath).catch(() => null);
    if (!metadata?.isFile()) throw new HttpError(404, "Not found");
    const contentType = MIME_TYPES.get(path.extname(filePath).toLowerCase());
    if (!contentType) throw new HttpError(404, "Not found");
    response.writeHead(200, securityHeaders({
      "Content-Type": contentType,
      "Content-Length": String(metadata.size),
      "Cache-Control": filePath.endsWith(".json") ? "no-store" : "no-cache",
    }));
    if (request.method === "HEAD") response.end();
    else createReadStream(filePath).on("error", () => response.destroy()).pipe(response);
  } catch (error) {
    const statusCode = error instanceof HttpError ? error.statusCode : 500;
    const message = error instanceof HttpError ? error.message : "Internal server error";
    respond(response, statusCode, `${message}\n`);
  }
});

server.listen(PORT, "127.0.0.1", () => {
  process.stdout.write(`OfficeGuard Trace Viewer: http://127.0.0.1:${PORT}\n`);
});

function respond(response, statusCode, body, extraHeaders = {}) {
  response.writeHead(statusCode, securityHeaders({
    "Content-Type": "text/plain; charset=utf-8",
    "Content-Length": String(Buffer.byteLength(body)),
    ...extraHeaders,
  }));
  response.end(body);
}

function securityHeaders(headers) {
  return {
    "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    ...headers,
  };
}

function parsePort(value) {
  const port = Number(value);
  if (!Number.isInteger(port) || port < 1 || port > 65_535) throw new Error("Port must be an integer from 1 to 65535");
  return port;
}

class HttpError extends Error {
  constructor(statusCode, message) {
    super(message);
    this.statusCode = statusCode;
  }
}
