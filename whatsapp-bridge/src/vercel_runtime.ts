import fs from "node:fs/promises";
import path from "node:path";
import {
  createCipheriv,
  createDecipheriv,
  randomBytes,
} from "node:crypto";
import { Pool } from "pg";

export interface QueuedBridgeCommand {
  id: string;
  command_type: string;
  payload: Record<string, unknown>;
}

export function createDatabasePool(): Pool {
  const connectionString = process.env.DATABASE_URL;
  if (!connectionString) {
    throw new Error("DATABASE_URL is required for database bridge transport");
  }
  return new Pool({ connectionString, max: 2 });
}

export function getAuthEncryptionKey(): Buffer {
  const encoded = process.env.WHATSAPP_AUTH_ENCRYPTION_KEY;
  if (!encoded) {
    throw new Error("WHATSAPP_AUTH_ENCRYPTION_KEY is required");
  }
  const key = Buffer.from(encoded, "base64");
  if (key.length !== 32) {
    throw new Error("WHATSAPP_AUTH_ENCRYPTION_KEY must be a base64 32-byte key");
  }
  return key;
}

export function encryptAuthFile(contents: string, key: Buffer): string {
  const iv = randomBytes(12);
  const cipher = createCipheriv("aes-256-gcm", key, iv);
  const encrypted = Buffer.concat([
    cipher.update(contents, "utf8"),
    cipher.final(),
  ]);
  return [
    "v1",
    iv.toString("base64"),
    cipher.getAuthTag().toString("base64"),
    encrypted.toString("base64"),
  ].join(":");
}

export function decryptAuthFile(contents: string, key: Buffer): string {
  const [version, encodedIv, encodedTag, encodedData] = contents.split(":");
  if (!encodedIv || !encodedTag || !encodedData || version !== "v1") {
    throw new Error("WhatsApp auth data has an unsupported encryption format");
  }
  const decipher = createDecipheriv(
    "aes-256-gcm",
    key,
    Buffer.from(encodedIv, "base64"),
  );
  decipher.setAuthTag(Buffer.from(encodedTag, "base64"));
  return Buffer.concat([
    decipher.update(Buffer.from(encodedData, "base64")),
    decipher.final(),
  ]).toString("utf8");
}

export async function initializeBridgeTables(pool: Pool): Promise<void> {
  await pool.query(`
    CREATE TABLE IF NOT EXISTS bridge_commands (
      id UUID PRIMARY KEY,
      command_type VARCHAR(50) NOT NULL,
      payload JSONB NOT NULL,
      status VARCHAR(20) NOT NULL DEFAULT 'queued',
      result JSONB,
      error TEXT,
      available_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
      created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
      updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
  `);
  await pool.query(`
    CREATE INDEX IF NOT EXISTS ix_bridge_commands_ready
    ON bridge_commands (status, available_at, created_at)
  `);
  await pool.query(`
    CREATE TABLE IF NOT EXISTS bridge_runtime_state (
      id INTEGER PRIMARY KEY,
      state JSONB NOT NULL,
      updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
  `);
  await pool.query(`
    CREATE TABLE IF NOT EXISTS bridge_auth_files (
      file_name VARCHAR(255) PRIMARY KEY,
      contents TEXT NOT NULL,
      updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
  `);
}

export async function restoreAuthFiles(
  pool: Pool,
  authDirectory: string,
  encryptionKey: Buffer,
): Promise<void> {
  await fs.mkdir(authDirectory, { recursive: true });
  const result = await pool.query<{ file_name: string; contents: string }>(
    "SELECT file_name, contents FROM bridge_auth_files",
  );
  await Promise.all(
    result.rows.map(async ({ file_name, contents }) => {
      if (path.basename(file_name) !== file_name) {
        throw new Error("Invalid WhatsApp auth filename in database");
      }
      await fs.writeFile(
        path.join(authDirectory, file_name),
        decryptAuthFile(contents, encryptionKey),
        "utf8",
      );
    }),
  );
}

export async function persistAuthFiles(
  pool: Pool,
  authDirectory: string,
  encryptionKey: Buffer,
): Promise<void> {
  const files = await fs.readdir(authDirectory, { withFileTypes: true });
  const client = await pool.connect();
  try {
    await client.query("BEGIN");
    await client.query("DELETE FROM bridge_auth_files");
    for (const file of files) {
      if (!file.isFile()) {
        continue;
      }
      const contents = await fs.readFile(
        path.join(authDirectory, file.name),
        "utf8",
      );
      await client.query(
        `INSERT INTO bridge_auth_files (file_name, contents)
         VALUES ($1, $2)
         ON CONFLICT (file_name) DO UPDATE
         SET contents = EXCLUDED.contents, updated_at = NOW()`,
        [file.name, encryptAuthFile(contents, encryptionKey)],
      );
    }
    await client.query("COMMIT");
  } catch (error) {
    await client.query("ROLLBACK");
    throw error;
  } finally {
    client.release();
  }
}

export async function saveRuntimeState(
  pool: Pool,
  state: Record<string, unknown>,
): Promise<void> {
  await pool.query(
    `INSERT INTO bridge_runtime_state (id, state, updated_at)
     VALUES (1, $1, NOW())
     ON CONFLICT (id) DO UPDATE
     SET state = EXCLUDED.state, updated_at = NOW()`,
    [JSON.stringify(state)],
  );
}

export async function claimBridgeCommand(
  pool: Pool,
): Promise<QueuedBridgeCommand | undefined> {
  await pool.query(
    `UPDATE bridge_commands
     SET status = 'queued', updated_at = NOW()
     WHERE status = 'processing'
       AND updated_at < NOW() - INTERVAL '10 minutes'`,
  );
  const result = await pool.query<QueuedBridgeCommand>(
    `UPDATE bridge_commands
     SET status = 'processing', updated_at = NOW()
     WHERE id = (
       SELECT id
       FROM bridge_commands
       WHERE status = 'queued' AND available_at <= NOW()
       ORDER BY available_at, created_at
       FOR UPDATE SKIP LOCKED
       LIMIT 1
     )
     RETURNING id, command_type, payload`,
  );
  return result.rows[0];
}

export async function deferPendingRetailPosts(
  pool: Pool,
  delaySeconds: number,
): Promise<void> {
  await pool.query(
    `UPDATE bridge_commands
     SET available_at = GREATEST(
           available_at,
           NOW() + ($1 * INTERVAL '1 second')
         ),
         updated_at = NOW()
     WHERE status = 'queued'
       AND command_type = 'send_media'
       AND (
         payload ? 'productId'
         OR payload->>'retailVariant' = 'true'
       )
       AND available_at < NOW() + ($1 * INTERVAL '1 second')`,
    [delaySeconds],
  );
}

export async function completeBridgeCommand(
  pool: Pool,
  commandId: string,
  result: Record<string, unknown>,
): Promise<void> {
  await pool.query(
    `UPDATE bridge_commands
     SET status = 'completed', result = $2, error = NULL, updated_at = NOW()
     WHERE id = $1`,
    [commandId, JSON.stringify(result)],
  );
}

export async function failBridgeCommand(
  pool: Pool,
  commandId: string,
  error: string,
): Promise<void> {
  await pool.query(
    `UPDATE bridge_commands
     SET status = 'failed', error = $2, updated_at = NOW()
     WHERE id = $1`,
    [commandId, error],
  );
}
