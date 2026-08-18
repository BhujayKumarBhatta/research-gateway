import { expect, test } from "@playwright/test";

test("reviewer can move from overview to evidence filters", async ({ page }) => {
  await page.route("**/api/v1/studies", route => route.fulfill({
    json: [{study_id: "s1", name: "Study one"}],
  }));
  await page.route("**/api/v1/studies/s1/topics", route => route.fulfill({
    json: [{topic_id: "t1", name: "Topic one"}],
  }));
  await page.route("**/api/v1/status", route => route.fulfill({
    json: {
      summary: {total: 3, unreviewed: 2, included: 0, excluded: 0, final_count: 1, search_runs: 4},
      sources: [{name: "scopus", available: true, configured: true}],
    },
  }));
  await page.route("**/api/v1/evidence**", route => {
    if (new URL(route.request().url()).pathname.endsWith("/evidence/e1")) {
      return route.fulfill({json: {
        evidence_id: "e1", evidence_code: "E000001", title: "Traceable research result",
        authors: [{name: "Ada Example"}], author_names: "Ada Example", year: 2025,
        publication: "Journal of Evidence", document_type: "article",
        publication_type: "journal_article", review_status: "peer_reviewed",
        normalized_doi: "10.1000/example", screening_status: "unreviewed", final_corpus: false,
        publication_type: "journal_article", review_status: "peer_reviewed",
        identifiers: [{identifier_type:"doi", identifier_value:"10.1000/example", source_provider:"scopus"}],
        discoveries: [{search_run_id:"r1", search_code:"Q0001", provider:"scopus", rank:1, discovered_at:"2026-01-01"}],
        screening_history: [], notes: [],
      }});
    }
    return route.fulfill({json: {
      total: 1,
      items: [{
        evidence_id: "e1", evidence_code: "E000001", title: "Traceable research result",
        author_names: "Ada Example", year: 2025, publication: "Journal of Evidence",
        normalized_doi: "10.1000/example", screening_status: "unreviewed", final_corpus: false,
      }],
    }});
  });
  await page.goto("./");
  await expect(page.getByRole("heading", {name: "Research at a glance"})).toBeVisible();
  await expect(page.getByText("3", {exact: true}).first()).toBeVisible();
  await page.getByRole("link", {name: "Evidence"}).click();
  await expect(page.getByRole("heading", {name: "Evidence corpus"})).toBeVisible();
  await expect(page.getByText("Traceable research result")).toBeVisible();
  await expect(page.getByLabel("Search evidence")).toBeVisible();
  await page.getByLabel("Study").selectOption("s1");
  await page.getByLabel("Topic").selectOption("t1");
  await page.getByLabel("Source").selectOption("scopus");
  await page.getByLabel("Search ID").fill("Q0001");
  await page.getByLabel("Discovery from").fill("2026-01-01");
  await page.getByLabel("Discovery to").fill("2026-12-31");
  await page.getByLabel("Screening status").selectOption("unreviewed");
  const filtered = page.waitForRequest(request => {
    const url = new URL(request.url());
    return url.pathname.endsWith("/evidence")
      && url.searchParams.get("study_id") === "s1"
      && url.searchParams.get("topic_id") === "t1"
      && url.searchParams.get("provider") === "scopus"
      && url.searchParams.get("search_code") === "Q0001"
      && url.searchParams.get("discovered_from") === "2026-01-01"
      && url.searchParams.get("discovered_to") === "2026-12-31"
      && url.searchParams.get("status") === "unreviewed"
      && url.searchParams.get("final") === "false";
  });
  await page.getByLabel("Final corpus").selectOption("false");
  await filtered;
  await page.getByRole("link", {name: "Traceable research result"}).click();
  await expect(page.getByRole("heading", {name: "Traceable research result"})).toBeVisible();
  await expect(page.getByText("10.1000/example").first()).toBeVisible();
});

test("reviewer can inspect an exact search run and its discoveries", async ({page}) => {
  await page.route("**/api/v1/studies", route => route.fulfill({json: []}));
  await page.route("**/api/v1/search-runs**", route => {
    if (new URL(route.request().url()).pathname.endsWith("/search-runs/r1")) {
      return route.fulfill({json: {
        search_run_id:"r1", search_code:"Q0001", study_id:"s1", topic_id:"t1",
        provider:"scopus", mode:"save", label:"baseline", search_intent:"Find failures",
        provider_query:"TITLE-ABS-KEY(failure)", status:"completed",
        executed_at_utc:"2026-01-01T00:00:00Z", retrieved_count:1,
        new_evidence_count:1, existing_evidence_count:0, filters:{}, sort:{}, pagination:{},
        provider_metadata:{}, hits:[{search_hit_id:"h1", evidence_id:"e1", evidence_code:"E000001", title:"Traceable research result", rank:1, provider_record_id:"2-s2.0-1"}],
      }});
    }
    return route.fulfill({json: [{
      search_run_id:"r1", search_code:"Q0001", study_id:"s1", topic_id:"t1",
      provider:"scopus", mode:"save", label:"baseline", search_intent:"Find failures",
      provider_query:"TITLE-ABS-KEY(failure)", status:"completed",
      executed_at_utc:"2026-01-01T00:00:00Z", retrieved_count:1,
      new_evidence_count:1, existing_evidence_count:0,
    }]});
  });
  await page.goto("./search-runs");
  await expect(page.getByRole("heading", {name:"Search runs"})).toBeVisible();
  await page.getByRole("link", {name:"Q0001"}).click();
  await expect(page.getByRole("heading", {name:"Q0001"})).toBeVisible();
  await expect(page.getByText("TITLE-ABS-KEY(failure)")).toBeVisible();
  await expect(page.getByRole("link", {name:/Traceable research result/})).toBeVisible();
});
