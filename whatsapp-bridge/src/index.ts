import "dotenv/config";

import fs from "node:fs/promises";
import path from "node:path";
import express, { Request, Response } from "express";
import pino from "pino";
import qrcode from "qrcode-terminal";
import makeWASocket, {
  downloadMediaMessage,
  jidNormalizedUser,
  useMultiFileAuthState,
  WAMessage,
} from "@whiskeysockets/baileys";

const logger = pino({ level: process.env.LOG_LEVEL ?? "info" });
const port = Number(process.env.BRIDGE_API_PORT ?? 3001);
const backendUrl = (
  process.env.BACKEND_WEBHOOK_URL ?? "http://localhost:8000/api/webhooks"
).replace(/\/$/, "");
const webhookSecret = process.env.BRIDGE_WEBHOOK_SECRET;
const wholesaleGroupJid = process.env.WHOLESALE_GROUP_JID;
const retailGroupJid = process.env.RETAIL_GROUP_JID;
const mumAdminJid = process.env.MUM_ADMIN_JID;
const wholesalerJids = new Set(
  (process.env.WHOLESALER_JIDS ?? "")
    .split(",")
    .map((jid) => jid.trim())
    .filter(Boolean),
);

let socket: Awaited<ReturnType<typeof makeWASocket>>;

async function postWebhook(
  route: "wholesale" | "customer" | "wholesaler",
  payload: Record<string, unknown>,
): Promise<void> {
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
  }
}

async function downloadImage(message: WAMessage): Promise<{
  base64: string;
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
  return { base64: buffer.toString("base64"), mimeType };
}

async function startWhatsApp(): Promise<void> {
  const { state, saveCreds } = await useMultiFileAuthState(
    path.resolve("auth_info_baileys"),
  );
  socket = makeWASocket({
    auth: state,
    logger,
    printQRInTerminal: false,
  });
  socket.ev.on("creds.update", saveCreds);

  socket.ev.on("connection.update", (update) => {
    if (update.qr) {
      qrcode.generate(update.qr, { small: true });
    }
  });

  socket.ev.on("messages.upsert", async ({ messages, type }) => {
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
      const text =
        message.message?.conversation ??
        message.message?.extendedTextMessage?.text ??
        "";
      const quotedMessageId =
        message.message?.extendedTextMessage?.contextInfo
          ?.stanzaId ?? undefined;

      if (message.message?.imageMessage && chatJid === wholesaleGroupJid) {
        try {
          const image = await downloadImage(message);
          await postWebhook("wholesale", {
            message_id: message.key.id,
            from_jid: fromJid,
            chat_jid: chatJid,
            caption: message.message.imageMessage.caption ?? "",
            image_base64: image.base64,
            mime_type: image.mimeType,
            quoted_message_id: quotedMessageId,
            raw: { key: message.key },
          });
        } catch (error) {
          logger.error({ error }, "Failed to ingest wholesale image");
        }
        continue;
      }

      if (!text) {
        continue;
      }
      if (wholesalerJids.has(chatJid)) {
        await postWebhook("wholesaler", {
          message_id: message.key.id,
          from_jid: fromJid,
          chat_jid: chatJid,
          text,
          quoted_message_id: quotedMessageId,
          raw: { key: message.key },
        });
      } else if (chatJid.endsWith("@s.whatsapp.net")) {
        await postWebhook("customer", {
          message_id: message.key.id,
          from_jid: fromJid,
          chat_jid: chatJid,
          text,
          quoted_message_id: quotedMessageId,
          raw: { key: message.key },
        });
      }
    }
  });
}

const app = express();
app.use(express.json({ limit: "30mb" }));

app.get("/health", (_request: Request, response: Response) => {
  response.json({ status: "ok", connected: Boolean(socket?.user) });
});

app.post("/api/messages/send", async (request: Request, response: Response) => {
  const { to, text, quotedMessageId } = request.body as {
    to: string;
    text: string;
    quotedMessageId?: string;
  };
  if (!to || typeof text !== "string") {
    response.status(400).json({ error: "to and text are required" });
    return;
  }
  const result = await socket.sendMessage(to, {
    text,
  }, quotedMessageId
    ? { quoted: { key: { remoteJid: to, id: quotedMessageId } } }
    : undefined);
  response.json({ messageId: result?.key?.id });
});

app.post(
  "/api/messages/send-media",
  async (request: Request, response: Response) => {
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
    const media = await fs.readFile(mediaPath);
    const mimeType =
      path.extname(mediaPath).toLowerCase() === ".png"
        ? "image/png"
        : "image/jpeg";
    const result = await socket.sendMessage(to, {
      image: media,
      mimetype: mimeType,
      caption: caption ?? "",
    }, quotedMessageId
      ? { quoted: { key: { remoteJid: to, id: quotedMessageId } } }
      : undefined);
    response.json({ messageId: result?.key?.id });
  },
);

app.post("/api/messages/typing", async (request: Request, response: Response) => {
  const { to } = request.body as { to: string };
  if (!to) {
    response.status(400).json({ error: "to is required" });
    return;
  }
  await socket.sendPresenceUpdate("composing", to);
  setTimeout(() => {
    void socket.sendPresenceUpdate("paused", to);
  }, 2500);
  response.status(204).end();
});

void startWhatsApp();
app.listen(port, "0.0.0.0", () => {
  logger.info({ port }, "WhatsApp bridge listening");
});
