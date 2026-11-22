import assert from "node:assert/strict";
import test from "node:test";

import { decryptAuthFile, encryptAuthFile } from "./vercel_runtime.js";

test("WhatsApp auth files are encrypted and can be restored", () => {
  const key = Buffer.alloc(32, 7);
  const contents = JSON.stringify({
    creds: "private session material",
    signalKey: Buffer.from("key bytes").toString("base64"),
  });

  const encrypted = encryptAuthFile(contents, key);

  assert.notEqual(encrypted, contents);
  assert.equal(decryptAuthFile(encrypted, key), contents);
});

test("WhatsApp auth files reject an incorrect encryption key", () => {
  const encrypted = encryptAuthFile("session data", Buffer.alloc(32, 1));

  assert.throws(() => decryptAuthFile(encrypted, Buffer.alloc(32, 2)));
});
