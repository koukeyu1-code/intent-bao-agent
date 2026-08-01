import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";


test("business API shares the auth base and sends Bearer without cookies", async () => {
  const [source, launcher] = await Promise.all([
    readFile(new URL("../app/api-client.ts", import.meta.url), "utf8"),
    readFile(new URL("../scripts/start-local.ps1", import.meta.url), "utf8"),
  ]);
  assert.match(source, /export const API_BASE = resolveAuthApiBase\(\)/);
  assert.doesNotMatch(source, /credentials:\s*"include"/);
  assert.equal(
    source.match(/credentials:\s*"omit"/g)?.length,
    3,
    "JSON, material upload, and Excel preflight must all reject cookies",
  );
  assert.ok(
    (source.match(/bearerHeaders\(\)/g)?.length ?? 0) >= 3,
    "every business fetch path must attach the session Bearer token",
  );
  assert.match(
    source,
    /status === 401[\s\S]{0,160}clearAccessToken\(\)[\s\S]{0,160}intent-bao:auth-expired/,
  );
  assert.match(
    launcher,
    /NEXT_PUBLIC_INTENT_BAO_API_BASE = "http:\/\/127\.0\.0\.1:\$BackendPort\/api"/,
  );
  assert.doesNotMatch(launcher, /HttpOnly|SameSite|login cookie/i);
});

test("intent review fields and platform path use one atomic API request", async () => {
  const [clientSource, appSource] = await Promise.all([
    readFile(new URL("../app/api-client.ts", import.meta.url), "utf8"),
    readFile(new URL("../app/intent-bao-app.tsx", import.meta.url), "utf8"),
  ]);

  assert.match(
    clientSource,
    /export const updateIntentReviewDraft[\s\S]*\/review-draft/,
  );
  assert.match(
    appSource,
    /await updateIntentReviewDraft\(editing, preparedPlatformDraft\)/,
  );
  assert.doesNotMatch(appSource, /saveObservationPlatformDraft\(/);
});
