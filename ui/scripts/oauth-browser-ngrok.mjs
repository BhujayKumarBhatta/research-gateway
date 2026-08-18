import { chromium } from "playwright";

let payload = "";
for await (const chunk of process.stdin) {
  payload += chunk;
}
const input = JSON.parse(payload);
const events = [];
let phase = "launch";
let browser;
let page;

function record(response) {
  const url = new URL(response.url());
  if (![input.gateway_origin, input.callback_origin].includes(url.origin)) {
    return;
  }
  events.push({
    method: response.request().method(),
    path: url.pathname,
    status: response.status(),
  });
}

try {
  browser = await chromium.launch({ headless: true });
  const context = await browser.newContext();
  page = await context.newPage();
  page.on("response", record);

  phase = "authorization-navigation";
  await page.goto(input.authorization_url, { waitUntil: "domcontentloaded" });

  let interstitialVisited = false;
  const password = page.locator('input[name="password"]');
  const visitSite = page.getByText(/Visit Site/i).first();
  await Promise.any([
    password.waitFor({ state: "visible", timeout: 30_000 }),
    visitSite.waitFor({ state: "visible", timeout: 30_000 }),
  ]);
  if (await visitSite.isVisible()) {
    interstitialVisited = true;
    await Promise.all([
      page.waitForURL((url) => url.origin === input.gateway_origin, { timeout: 30_000 }),
      visitSite.click(),
    ]);
  }

  phase = "approval-form";
  await password.waitFor({ state: "visible", timeout: 30_000 });
  await password.fill(input.password);
  const allow = page.locator('button[name="decision"][value="allow"]');
  await allow.waitFor({ state: "visible", timeout: 10_000 });

  phase = "approval-submit";
  let allowClicks = 0;
  allowClicks += 1;
  await Promise.all([
    page.waitForURL(
      (url) => url.origin === input.callback_origin && url.pathname === input.callback_path,
      { timeout: 30_000 },
    ),
    allow.click(),
  ]);
  await page.locator("[data-oauth-callback='received']").waitFor({ timeout: 10_000 });
  if (allowClicks !== 1) {
    throw new Error("unexpected approval click count");
  }

  console.log(
    JSON.stringify({
      ok: true,
      allow_clicks: allowClicks,
      interstitial_visited: interstitialVisited,
      events,
    }),
  );
} catch (error) {
  const errorText = String(error);
  const errorKind = [
    "ERR_CONNECTION_REFUSED",
    "ERR_FAILED",
    "ERR_ABORTED",
    "TimeoutError",
  ].find((marker) => errorText.includes(marker)) ?? "unknown";
  let location = null;
  try {
    const current = new URL(page.url());
    location = { origin: current.origin, path: current.pathname };
  } catch {
    // Keep diagnostics query-free and optional.
  }
  console.log(JSON.stringify({ ok: false, phase, error_kind: errorKind, location, events }));
  process.exitCode = 1;
} finally {
  if (browser) {
    await browser.close();
  }
}
