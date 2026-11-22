import fs from "node:fs/promises";
import path from "node:path";
import crypto from "node:crypto";
import { fileURLToPath } from "node:url";
import { del, put } from "@vercel/blob";
import dotenv from "dotenv";
import express, {
  ErrorRequestHandler,
  Request,
  RequestHandler,
  Response,
} from "express";
import pino from "pino";
import qrcode from "qrcode-terminal";
import QRCode from "qrcode";
import makeWASocket, {
  downloadMediaMessage,
  DisconnectReason,
  jidNormalizedUser,
  useMultiFileAuthState,
  WAMessage,
} from "@whiskeysockets/baileys";
import type { Pool } from "pg";
import {
  claimBridgeCommand,
  completeBridgeCommand,
  createDatabasePool,
  deferPendingRetailPosts,
  failBridgeCommand,
  getAuthEncryptionKey,
  initializeBridgeTables,
  persistAuthFiles,
  restoreAuthFiles,
  saveRuntimeState,
} from "./vercel_runtime.js";

const repositoryRoot = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "../..",
);
dotenv.config({ path: path.join(repositoryRoot, ".env") });

const logger = pino({ level: process.env.LOG_LEVEL ?? "info" });
function isSignalSessionState(value: unknown): boolean {
  return (
    typeof value === "object" &&
    value !== null &&
    ("currentRatchet" in value || "_chains" in value || "rootKey" in value)
  );
}

for (const method of ["info", "warn", "error"] as const) {
  const original = console[method].bind(console);
  console[method] = (...args: Parameters<typeof console.info>) => {
    if (args.some(isSignalSessionState)) {
      logger.warn(
        { source: "libsignal", event: String(args[0] ?? "session update") },
        "Suppressed cryptographic session state log",
      );
      return;
    }
    original(...args);
  };
}

const backendUrl = (
  process.env.BACKEND_WEBHOOK_URL ?? "http://localhost:8000/api/webhooks"
).replace(/\/$/, "");
const webhookSecret = process.env.BRIDGE_WEBHOOK_SECRET;
const bridgeApiSecret = process.env.BRIDGE_API_SECRET;
const bridgeTransport = process.env.BRIDGE_TRANSPORT ?? "http";
let databasePool: Pool | undefined;
let authEncryptionKey: Buffer | undefined;
const port = Number(
  process.env.BRIDGE_API_PORT ?? process.env.PORT ?? 3001,
);
const host = process.env.BRIDGE_API_HOST ?? "127.0.0.1";
const whatsappAuthDir = process.env.WHATSAPP_AUTH_DIR
  ?? path.resolve("auth_info_baileys");
let wholesaleGroupJids = new Set(
  [
    ...(process.env.WHOLESALE_GROUP_JIDS ?? "").split(","),
    process.env.WHOLESALE_GROUP_JID ?? "",
  ]
    .map((jid) => jid.trim())
    .filter(Boolean),
);
let qrDataUrl: string | null = null;
let connectionState = "starting";
let isShuttingDown = false;
let refreshTimer: NodeJS.Timeout | undefined;
let heartbeatTimer: NodeJS.Timeout | undefined;

let socket: Awaited<ReturnType<typeof makeWASocket>> | undefined;
let reconnectTimer: NodeJS.Timeout | undefined;

function getDisconnectStatusCode(error: unknown): number | undefined {
  if (!error || typeof error !== "object" || !("output" in error)) {
    return undefined;
  }
  const output = error.output;
  if (!output || typeof output !== "object" || !("statusCode" in output)) {
    return undefined;
  }
  return typeof output.statusCode === "number" ? output.statusCode : undefined;
}

async function postWebhook(
  route: "wholesale",
  payload: Record<string, unknown>,
): Promise<{ accepted?: boolean; duplicate?: boolean } | undefined> {
  const headers: Record<string, string> = {
    "content-type": "application/json",
  };
  if (webhookSecret) {
    headers["x-webhook-secret"] = webhookSecret;
  }
  const response = await fetch(`${backendUrl}/${route}`, {
    method: "POST",
    headers,
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    const body = await response.text();
    logger.error(
      { status: response.status, route, body },
      "Backend webhook rejected event",
    );
    return undefined;
  }
  const body: unknown = await response.json();
  if (!body || typeof body !== "object") {
    throw new Error("Backend webhook returned an invalid response");
  }
  const result = body as Record<string, unknown>;
  return {
    accepted: typeof result.accepted === "boolean" ? result.accepted : undefined,
    duplicate: result.duplicate === true,
  };
}

async function downloadImage(message: WAMessage): Promise<{
  buffer: Buffer;
  mimeType: string;
}> {
  const buffer = await downloadMediaMessage(
    message,
    "buffer",
    {},
    {
      logger,
      reuploadRequest: async () => message,
    },
  );
  if (!buffer) {
    throw new Error("WhatsApp returned no media buffer");
  }
  const mimeType =
    message.message?.imageMessage?.mimetype ?? "application/octet-stream";
  return { buffer, mimeType };
}

async function uploadWholesaleImage(
  buffer: Buffer,
  mimeType: string,
): Promise<string> {
  if (!databasePool) {
    throw new Error("Vercel Blob upload requires database bridge transport");
  }
  if (
    !process.env.BLOB_READ_WRITE_TOKEN &&
    !(process.env.BLOB_STORE_ID && process.env.VERCEL_OIDC_TOKEN)
  ) {
    throw new Error("Vercel Blob credentials are not configured");
  }
  const extension = mimeType.split("/")[1]?.replace(/[^a-zA-Z0-9]/g, "") || "jpg";
  const blob = await put(
    `wholesale/${crypto.randomUUID()}.${extension}`,
    buffer,
    { access: "public", contentType: mimeType },
  );
  return blob.url;
}

async function persistRuntimeState(): Promise<void> {
  if (!databasePool) {
    return;
  }
  await saveRuntimeState(databasePool, {
    connected: Boolean(socket?.user),
    connection_state: connectionState,
    account_jid: socket?.user ? jidNormalizedUser(socket.user.id) : null,
    qr_data_url: qrDataUrl,
  });
}

async function startWhatsApp(): Promise<void> {
  const authDirectory = databasePool
    ? path.resolve("/tmp/whatsapp-auth")
    : path.resolve(whatsappAuthDir);
  if (databasePool) {
    if (!authEncryptionKey) {
      throw new Error("WhatsApp auth encryption key is unavailable");
    }
    await fs.rm(authDirectory, { recursive: true, force: true });
    await restoreAuthFiles(databasePool, authDirectory, authEncryptionKey);
  }
  const authState = await useMultiFileAuthState(authDirectory);
  const { state } = authState;
  let persistWrites = Promise.resolve();
  const persistAuth = (): Promise<void> => {
    if (!databasePool) {
      return Promise.resolve();
    }
    const pool = databasePool;
    const encryptionKey = authEncryptionKey;
    if (!encryptionKey) {
      throw new Error("WhatsApp auth encryption key is unavailable");
    }
    persistWrites = persistWrites.catch((error: unknown) => {
      logger.error({ error }, "Previous WhatsApp auth persistence failed");
    }).then(() =>
      persistAuthFiles(pool, authDirectory, encryptionKey),
    );
    return persistWrites;
  };
  const originalSetKeys = state.keys.set.bind(state.keys);
  state.keys.set = async (data) => {
    await originalSetKeys(data);
    await persistAuth();
  };
  const saveCreds = async (): Promise<void> => {
    await authState.saveCreds();
    await persistAuth();
  };
  await persistRuntimeState();
  let credentialWrites = Promise.resolve();
  let reconnectAttempts = 0;

  const connect = (): void => {
    if (isShuttingDown) {
      return;
    }
    const waSocket = makeWASocket({
      auth: state,
      logger,
      printQRInTerminal: false,
    });
    socket = waSocket;
    connectionState = "connecting";

    waSocket.ev.on("creds.update", () => {
      credentialWrites = credentialWrites
        .then(() => saveCreds())
        .catch((error: unknown) => {
          logger.error({ error }, "Failed to persist WhatsApp credentials");
        });
    });

    waSocket.ev.on("connection.update", (update) => {
      if (update.qr) {
        connectionState = "waiting_for_scan";
        qrcode.generate(update.qr, { small: true });
        void QRCode.toDataURL(update.qr)
          .then((dataUrl) => {
            qrDataUrl = dataUrl;
          })
          .catch((error: unknown) => {
            logger.error({ error }, "Failed to render login QR");
          });
        void persistRuntimeState().catch((error: unknown) => {
          logger.error({ error }, "Failed to persist WhatsApp bridge status");
        });
      }
      if (update.connection === "open") {
        connectionState = "connected";
        reconnectAttempts = 0;
        qrDataUrl = null;
        logger.info("WhatsApp connection is ready");
        void persistRuntimeState().catch((error: unknown) => {
          logger.error({ error }, "Failed to persist WhatsApp bridge status");
        });
      }
      if (update.connection === "close") {
        qrDataUrl = null;
        const disconnectError = update.lastDisconnect?.error;
        const statusCode = getDisconnectStatusCode(disconnectError);
        logger.warn(
          { statusCode, error: disconnectError },
          "WhatsApp connection closed",
        );
        if (isShuttingDown) {
          connectionState = "stopping";
          socket = undefined;
          void persistRuntimeState().catch((error: unknown) => {
            logger.error({ error }, "Failed to persist WhatsApp bridge status");
          });
          return;
        }
        if (statusCode === DisconnectReason.loggedOut) {
          connectionState = "logged_out";
          socket = undefined;
          logger.error(
            "WhatsApp logged out. Remove the saved auth_info_baileys session and pair again.",
          );
          void persistRuntimeState().catch((error: unknown) => {
            logger.error({ error }, "Failed to persist WhatsApp bridge status");
          });
          return;
        }

        connectionState =
          statusCode === DisconnectReason.restartRequired
            ? "restarting_after_pairing"
            : "reconnecting";
        void persistRuntimeState().catch((error: unknown) => {
          logger.error({ error }, "Failed to persist WhatsApp bridge status");
        });
        reconnectAttempts += 1;
        const delay =
          statusCode === DisconnectReason.restartRequired
            ? 500
            : Math.min(1_000 * 2 ** Math.min(reconnectAttempts - 1, 5), 30_000);
        if (reconnectTimer) {
          clearTimeout(reconnectTimer);
        }
        reconnectTimer = setTimeout(() => {
          void (async () => {
            await credentialWrites;
            if (socket !== waSocket) {
              return;
            }
            socket = undefined;
            connect();
          })().catch((error: unknown) => {
            logger.error({ error }, "Failed to restart WhatsApp connection");
          });
        }, delay);
      }
    });

    waSocket.ev.on("messages.upsert", async ({ messages, type }) => {
      if (type !== "notify") {
        return;
      }
      for (const message of messages) {
        if (!message.key || message.key.fromMe) {
          continue;
        }
        const chatJid = jidNormalizedUser(message.key.remoteJid ?? "");
        const fromJid = jidNormalizedUser(
          message.key.participant ?? message.key.remoteJid ?? "",
        );
        const quotedMessageId =
          message.message?.extendedTextMessage?.contextInfo?.stanzaId ??
          undefined;

        if (message.message?.imageMessage && wholesaleGroupJids.has(chatJid)) {
          try {
            const image = await downloadImage(message);
            let uploadedBlobUrl: string | undefined;
            const imageFields = databasePool
              ? {
                  image_url: uploadedBlobUrl = await uploadWholesaleImage(
                    image.buffer,
                    image.mimeType,
                  ),
                }
              : { image_base64: image.buffer.toString("base64") };
            const result = await postWebhook("wholesale", {
              message_id: message.key.id,
              from_jid: fromJid,
              chat_jid: chatJid,
              caption: message.message.imageMessage.caption ?? "",
              ...imageFields,
              mime_type: image.mimeType,
              quoted_message_id: quotedMessageId,
              raw: { key: message.key },
            });
            if (
              uploadedBlobUrl &&
              (result?.accepted === false || result?.duplicate === true)
            ) {
              try {
                await del(uploadedBlobUrl);
              } catch (error) {
                logger.error({ error }, "Failed to clean up unused product image");
              }
            }
          } catch (error) {
            logger.error({ error }, "Failed to ingest wholesale image");
          }
          continue;
        }
      }
    });
  };

  connect();
}

const app = express();
let bridgeServer: ReturnType<typeof app.listen> | undefined;
app.use(express.json({ limit: "30mb" }));
app.use((request, response, next) => {
  if (!request.path.startsWith("/api/") && request.path !== "/health") {
    next();
    return;
  }
  if (
    bridgeTransport === "database" &&
    request.method === "GET" &&
    request.path === "/health"
  ) {
    next();
    return;
  }
  if (
    !bridgeApiSecret ||
    bridgeApiSecret.length < 32 ||
    bridgeApiSecret.startsWith("replace-with-")
  ) {
    response.status(503).json({
      error: "BRIDGE_API_SECRET must be configured with at least 32 characters",
    });
    return;
  }
  const supplied = request.get("x-bridge-secret") ?? "";
  const expectedDigest = crypto
    .createHash("sha256")
    .update(bridgeApiSecret)
    .digest();
  const suppliedDigest = crypto.createHash("sha256").update(supplied).digest();
  if (!crypto.timingSafeEqual(expectedDigest, suppliedDigest)) {
    response.status(401).json({ error: "Unauthorized" });
    return;
  }
  if (
    (request.path.startsWith("/api/messages/") || request.path === "/api/groups") &&
    !socket?.user
  ) {
    response.status(503).json({ error: "WhatsApp is not connected" });
    return;
  }
  next();
});

const asyncHandler = (
  handler: (request: Request, response: Response) => Promise<void>,
): RequestHandler => (request, response, next) => {
  void handler(request, response).catch(next);
};

function requiredCommandString(
  payload: Record<string, unknown>,
  key: string,
): string {
  const value = payload[key];
  if (typeof value !== "string" || !value) {
    throw new Error(`Bridge command is missing ${key}`);
  }
  return value;
}

function assertBlobUrl(value: string): URL {
  const url = new URL(value);
  if (
    url.protocol !== "https:" ||
    !url.hostname.endsWith(".public.blob.vercel-storage.com") ||
    url.username ||
    url.password
  ) {
    throw new Error("Bridge media URL is not a trusted Vercel Blob URL");
  }
  return url;
}

async function deleteBlobIfRequested(
  mediaUrl: string,
  shouldDelete: boolean,
): Promise<void> {
  if (!shouldDelete) {
    return;
  }
  try {
    await del(mediaUrl);
  } catch (error) {
    logger.error({ error }, "Failed to delete temporary image from Blob");
  }
}

async function sendQueuedMedia(
  payload: Record<string, unknown>,
): Promise<Record<string, unknown>> {
  if (!databasePool) {
    throw new Error("Database bridge transport is not initialized");
  }
  let to = typeof payload.to === "string" ? payload.to : "";
  let mediaUrl = requiredCommandString(payload, "mediaUrl");
  const productId = typeof payload.productId === "string"
    ? payload.productId
    : undefined;
  const retailVariant = payload.retailVariant === true;
  if (!to && !productId && !retailVariant) {
    throw new Error("Bridge command is missing to");
  }

  if (productId) {
    const result = await databasePool.query<{
      image_path: string;
      is_in_stock: boolean;
      automation_enabled: boolean;
      retail_group_jid: string | null;
    }>(
      `SELECT p.image_path, p.is_in_stock, s.automation_enabled,
              s.retail_group_jid
       FROM products p
       CROSS JOIN business_settings s
       WHERE p.id = $1 AND s.id = 1`,
      [productId],
    );
    const product = result.rows[0];
    if (
      !product ||
      !product.is_in_stock ||
      !product.automation_enabled ||
      !product.retail_group_jid
    ) {
      return { skipped: true };
    }
    to = product.retail_group_jid;
    mediaUrl = product.image_path;
  } else if (retailVariant) {
    const result = await databasePool.query<{
      automation_enabled: boolean;
      retail_group_jid: string | null;
    }>(
      `SELECT automation_enabled, retail_group_jid
       FROM business_settings WHERE id = 1`,
    );
    const settings = result.rows[0];
    if (!settings?.automation_enabled || !settings.retail_group_jid) {
      await deleteBlobIfRequested(
        mediaUrl,
        payload.deleteMediaAfterSend === true,
      );
      return { skipped: true };
    }
    to = settings.retail_group_jid;
  }

  const response = await fetch(assertBlobUrl(mediaUrl));
  if (!response.ok) {
    throw new Error(`Vercel Blob returned ${response.status}`);
  }
  const mimeType = response.headers.get("content-type")?.split(";")[0]
    ?? "image/jpeg";
  if (!mimeType.startsWith("image/")) {
    throw new Error("Vercel Blob did not return an image");
  }
  const quotedMessageId = typeof payload.quotedMessageId === "string"
    ? payload.quotedMessageId
    : undefined;
  const waSocket = getConnectedSocket();
  const result = await waSocket.sendMessage(to, {
    image: Buffer.from(await response.arrayBuffer()),
    mimetype: mimeType,
    caption: typeof payload.caption === "string" ? payload.caption : "",
  }, quotedMessageId
    ? { quoted: { key: { remoteJid: to, id: quotedMessageId } } }
    : undefined);
  const messageId = result?.key?.id;
  if (productId && messageId) {
    await databasePool.query(
      "UPDATE products SET retail_msg_id = $1 WHERE id = $2",
      [messageId, productId],
    );
  }
  await deleteBlobIfRequested(mediaUrl, payload.deleteMediaAfterSend === true);
  return { messageId };
}

async function executeQueuedCommand(
  commandType: string,
  payload: Record<string, unknown>,
): Promise<Record<string, unknown>> {
  const waSocket = getConnectedSocket();
  switch (commandType) {
    case "send_text": {
      const to = requiredCommandString(payload, "to");
      const quotedMessageId = typeof payload.quotedMessageId === "string"
        ? payload.quotedMessageId
        : undefined;
      const result = await waSocket.sendMessage(to, {
        text: requiredCommandString(payload, "text"),
      }, quotedMessageId
        ? { quoted: { key: { remoteJid: to, id: quotedMessageId } } }
        : undefined);
      return { messageId: result?.key?.id };
    }
    case "send_media":
      return sendQueuedMedia(payload);
    case "send_typing": {
      const to = requiredCommandString(payload, "to");
      await waSocket.sendPresenceUpdate("composing", to);
      setTimeout(() => {
        void waSocket.sendPresenceUpdate("paused", to).catch((error: unknown) => {
          logger.warn({ error }, "Failed to clear typing indicator");
        });
      }, 2500);
      return {};
    }
    case "get_groups": {
      const groups = await waSocket.groupFetchAllParticipating();
      return {
        groups: Object.values(groups)
          .map((group) => ({
            jid: group.id,
            subject: group.subject?.trim() || group.id,
          }))
          .sort((left, right) => left.subject.localeCompare(right.subject)),
      };
    }
    default:
      throw new Error(`Unsupported bridge command: ${commandType}`);
  }
}

async function runBridgeQueue(): Promise<void> {
  if (!databasePool) {
    return;
  }
  while (true) {
    if (!socket?.user) {
      await new Promise((resolve) => setTimeout(resolve, 2000));
      continue;
    }
    try {
      const command = await claimBridgeCommand(databasePool);
      if (!command) {
        await new Promise((resolve) => setTimeout(resolve, 1500));
        continue;
      }
      try {
        const result = await executeQueuedCommand(
          command.command_type,
          command.payload,
        );
        await completeBridgeCommand(databasePool, command.id, result);
        if (
          command.command_type === "send_media" &&
          result.skipped !== true &&
          (typeof command.payload.productId === "string" ||
            command.payload.retailVariant === true)
        ) {
          await deferPendingRetailPosts(
            databasePool,
            crypto.randomInt(120, 481),
          );
        }
      } catch (error) {
        const message = error instanceof Error ? error.message : "Unknown error";
        await failBridgeCommand(databasePool, command.id, message);
        logger.error(
          { commandId: command.id, commandType: command.command_type, error },
          "Bridge command failed",
        );
      }
    } catch (error) {
      logger.error({ error }, "Bridge command queue poll failed");
      await new Promise((resolve) => setTimeout(resolve, 5000));
    }
  }
}

function getConnectedSocket(): NonNullable<typeof socket> {
  if (!socket?.user) {
    throw new Error("WhatsApp is not connected");
  }
  return socket;
}

app.get("/health", (_request: Request, response: Response) => {
  response.json({
    status: "ok",
    connected: Boolean(socket?.user),
    connection_state: connectionState,
    account_jid: socket?.user ? jidNormalizedUser(socket.user.id) : null,
    qr_data_url: qrDataUrl,
  });
});

app.get("/api/groups", asyncHandler(async (_request, response) => {
  const groups = await getConnectedSocket().groupFetchAllParticipating();
  response.json({
    groups: Object.values(groups)
      .map((group) => ({
        jid: group.id,
        subject: group.subject?.trim() || group.id,
      }))
      .sort((left, right) => left.subject.localeCompare(right.subject)),
  });
}));

app.post("/api/messages/send", asyncHandler(async (request, response) => {
  const { to, text, quotedMessageId } = request.body as {
    to: string;
    text: string;
    quotedMessageId?: string;
  };
  if (!to || typeof text !== "string") {
    response.status(400).json({ error: "to and text are required" });
    return;
  }
  const waSocket = getConnectedSocket();
  const result = await waSocket.sendMessage(to, {
    text,
  }, quotedMessageId
    ? { quoted: { key: { remoteJid: to, id: quotedMessageId } } }
    : undefined);
  response.json({ messageId: result?.key?.id });
}));

app.post(
  "/api/messages/send-media",
  asyncHandler(async (request, response) => {
    const { to, mediaPath, caption, quotedMessageId } = request.body as {
      to: string;
      mediaPath: string;
      caption: string;
      quotedMessageId?: string;
    };
    if (!to || !mediaPath) {
      response.status(400).json({ error: "to and mediaPath are required" });
      return;
    }
    const waSocket = getConnectedSocket();
    const media = await fs.readFile(mediaPath);
    const extension = path.extname(mediaPath).toLowerCase();
    const mimeType =
      extension === ".png"
        ? "image/png"
        : extension === ".webp"
          ? "image/webp"
          : "image/jpeg";
    const result = await waSocket.sendMessage(to, {
      image: media,
      mimetype: mimeType,
      caption: caption ?? "",
    }, quotedMessageId
      ? { quoted: { key: { remoteJid: to, id: quotedMessageId } } }
      : undefined);
    response.json({ messageId: result?.key?.id });
  }),
);

app.post("/api/messages/typing", asyncHandler(async (request, response) => {
  const { to } = request.body as { to: string };
  if (!to) {
    response.status(400).json({ error: "to is required" });
    return;
  }
  const waSocket = getConnectedSocket();
  await waSocket.sendPresenceUpdate("composing", to);
  setTimeout(() => {
    void waSocket.sendPresenceUpdate("paused", to).catch((error: unknown) => {
      logger.warn({ error }, "Failed to clear typing indicator");
    });
  }, 2500);
  response.status(204).end();
}));

const apiErrorHandler: ErrorRequestHandler = (
  error,
  request,
  response,
  _next,
) => {
  logger.error({ err: error, path: request.path }, "Bridge API request failed");
  if (!response.headersSent) {
    response.status(502).json({
      error: "WhatsApp bridge request failed",
      message: error instanceof Error ? error.message : "Unknown bridge error",
    });
  }
};
app.use(apiErrorHandler);

const refreshBridgeConfig = async (): Promise<void> => {
  if (!bridgeApiSecret || bridgeApiSecret.length < 32) {
    logger.error(
      "BRIDGE_API_SECRET must be set to at least 32 characters; bridge API is disabled",
    );
    return;
  }
  try {
    const response = await fetch(
      `${backendUrl.replace(/\/api\/webhooks$/, "")}/api/bridge/config`,
      { headers: { "x-bridge-secret": bridgeApiSecret } },
    );
    if (!response.ok) {
      throw new Error(`Backend returned ${response.status}`);
    }
    const config = (await response.json()) as {
      wholesale_group_jids?: string[];
      wholesale_group_jid?: string | null;
    };
    wholesaleGroupJids = new Set(
      config.wholesale_group_jids
        ?? (config.wholesale_group_jid ? [config.wholesale_group_jid] : []),
    );
  } catch (error) {
    logger.error({ err: error }, "Failed to refresh bridge configuration");
  }
};
async function startBridge(): Promise<void> {
  if (bridgeTransport !== "http" && bridgeTransport !== "database") {
    throw new Error("BRIDGE_TRANSPORT must be either http or database");
  }
  if (bridgeTransport === "database") {
    if (
      !bridgeApiSecret ||
      bridgeApiSecret.length < 32 ||
      bridgeApiSecret.startsWith("replace-with-")
    ) {
      throw new Error("BRIDGE_API_SECRET must be at least 32 characters");
    }
    if (
      !webhookSecret ||
      webhookSecret.length < 32 ||
      webhookSecret.startsWith("replace-with-")
    ) {
      throw new Error("BRIDGE_WEBHOOK_SECRET must be at least 32 characters");
    }
    if (
      !process.env.BLOB_READ_WRITE_TOKEN &&
      !(process.env.BLOB_STORE_ID && process.env.VERCEL_OIDC_TOKEN)
    ) {
      throw new Error("Vercel Blob credentials are not configured");
    }
    authEncryptionKey = getAuthEncryptionKey();
    const backend = new URL(backendUrl);
    if (backend.protocol !== "https:" || backend.hostname === "localhost") {
      throw new Error("BACKEND_WEBHOOK_URL must use the deployed HTTPS URL");
    }
    databasePool = createDatabasePool();
    await initializeBridgeTables(databasePool);
    await persistRuntimeState();
  }
  await startWhatsApp();
  void refreshBridgeConfig();
  refreshTimer = setInterval(() => {
    void refreshBridgeConfig();
  }, 30_000);
  if (databasePool) {
    heartbeatTimer = setInterval(() => {
      void persistRuntimeState().catch((error: unknown) => {
        logger.error({ error }, "Failed to persist WhatsApp bridge heartbeat");
      });
    }, 15_000);
    void runBridgeQueue();
  }
  bridgeServer = app.listen(port, host, () => {
    logger.info({ port, host }, "WhatsApp bridge listening");
  });
}

process.on("SIGTERM", () => {
  isShuttingDown = true;
  if (refreshTimer) {
    clearInterval(refreshTimer);
  }
  if (heartbeatTimer) {
    clearInterval(heartbeatTimer);
  }
  if (reconnectTimer) {
    clearTimeout(reconnectTimer);
  }
  socket?.end(undefined);
  bridgeServer?.close();
  void (async () => {
    try {
      await persistRuntimeState();
      await databasePool?.end();
    } catch (error) {
      logger.error({ error }, "Bridge shutdown did not complete cleanly");
    }
  })();
});

void startBridge().catch((error: unknown) => {
  logger.error({ error }, "WhatsApp bridge failed to initialize");
  process.exitCode = 1;
});
