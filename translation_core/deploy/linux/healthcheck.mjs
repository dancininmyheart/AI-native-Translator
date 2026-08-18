import { constants as fsConstants } from "node:fs";
import { access } from "node:fs/promises";
import net from "node:net";
import tls from "node:tls";
import { spawnSync } from "node:child_process";

const timeoutMs = Number.parseInt(process.env.HEALTHCHECK_TIMEOUT_MS ?? "5000", 10);

function encodeRedisCommand(parts) {
  let command = `*${parts.length}\r\n`;
  for (const part of parts) {
    const value = String(part);
    command += `$${Buffer.byteLength(value)}\r\n${value}\r\n`;
  }
  return command;
}

async function assertRedisReady() {
  const redisUrl = new URL(process.env.REDIS_URL ?? "redis://redis:6379/0");
  if (!new Set(["redis:", "rediss:"]).has(redisUrl.protocol)) {
    throw new Error(`REDIS_URL protocol must be redis: or rediss:, got ${redisUrl.protocol}`);
  }

  const commands = [];
  const username = decodeURIComponent(redisUrl.username);
  const password = decodeURIComponent(redisUrl.password);
  if (password) {
    commands.push(username
      ? encodeRedisCommand(["AUTH", username, password])
      : encodeRedisCommand(["AUTH", password]));
  }

  const database = redisUrl.pathname.replace(/^\//, "");
  if (database && database !== "0") {
    commands.push(encodeRedisCommand(["SELECT", database]));
  }
  commands.push(encodeRedisCommand(["PING"]));

  await new Promise((resolve, reject) => {
    const options = {
      host: redisUrl.hostname,
      port: Number.parseInt(redisUrl.port || "6379", 10),
      servername: redisUrl.hostname,
    };
    const socket = redisUrl.protocol === "rediss:"
      ? tls.connect(options)
      : net.createConnection(options);
    let response = "";

    const fail = (error) => {
      socket.destroy();
      reject(error);
    };

    socket.setTimeout(timeoutMs, () => fail(new Error("Redis health check timed out")));
    socket.on("error", fail);
    socket.once(redisUrl.protocol === "rediss:" ? "secureConnect" : "connect", () => {
      socket.write(commands.join(""));
    });
    socket.on("data", (chunk) => {
      response += chunk.toString("utf8");
      if (response.includes("-")) {
        fail(new Error(`Redis rejected health check: ${response.trim()}`));
        return;
      }
      if (response.includes("+PONG\r\n")) {
        socket.end();
        resolve();
      }
    });
  });
}

async function assertAgentReady() {
  const port = Number.parseInt(process.env.PI_AGENT_PORT ?? "8080", 10);
  const response = await fetch(`http://127.0.0.1:${port}/readyz`, {
    signal: AbortSignal.timeout(timeoutMs),
  });
  if (!response.ok) {
    throw new Error(`Pi Agent readiness endpoint returned HTTP ${response.status}`);
  }
}

function assertCommand(command, args, label) {
  const result = spawnSync(command, args, {
    encoding: "utf8",
    timeout: timeoutMs,
  });
  if (result.error || result.status !== 0) {
    const detail = result.error?.message ?? result.stderr?.trim() ?? `exit ${result.status}`;
    throw new Error(`${label} failed: ${detail}`);
  }
}

async function main() {
  const writableDirectories = [
    process.env.PI_AGENT_JOBS_ROOT
      ?? process.env.FORMAT_TRANSLATOR_JOB_ROOT
      ?? "/data/jobs",
    process.env.PI_AGENT_UPLOADS_ROOT ?? "/data/uploads",
    process.env.PI_AGENT_CONFIG_DIR ?? "/data/pi-config",
    process.env.FORMAT_TRANSLATOR_LIBREOFFICE_LOCK_DIRECTORY
      ?? "/var/lock/format-translator/libreoffice",
  ];
  for (const directory of writableDirectories) {
    await access(directory, fsConstants.R_OK | fsConstants.W_OK);
  }

  assertCommand(
    "python",
    ["-c", "import format_translator"],
    "format_translator import",
  );
  assertCommand(
    process.env.FORMAT_TRANSLATOR_SOFFICE_PATH ?? "/usr/bin/libreoffice",
    ["--headless", "--version"],
    "LibreOffice",
  );
  await assertRedisReady();
  await assertAgentReady();

  process.stdout.write("healthy\n");
}

main().catch((error) => {
  process.stderr.write(`unhealthy: ${error.message}\n`);
  process.exitCode = 1;
});
