import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, test, vi } from "vitest";
import App from "./App";

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn(async () => ({
    ok: true,
    json: async () => ({summary:{total:2,unreviewed:1,included:0,excluded:0,final_count:1,search_runs:3},sources:[]}),
  })));
});

test("shows the evidence workspace map and navigation", async () => {
  render(<MemoryRouter><App /></MemoryRouter>);
  expect(await screen.findByText("Research at a glance")).toBeInTheDocument();
  expect(screen.getByText(/The Evidence Store is the local source of truth/)).toBeInTheDocument();
  expect(screen.getByRole("link", {name:"Evidence"})).toBeInTheDocument();
  expect(screen.getByText("Remote UI is disabled")).toBeInTheDocument();
});


test("evidence workspace exposes the complete local review filters", async () => {
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    const payload = url.includes("/studies")
      ? [{study_id:"s1", name:"Study one"}]
      : url.includes("/topics")
        ? [{topic_id:"t1", name:"Topic one"}]
        : {items:[], total:0, offset:0, limit:50};
    return {ok:true, json:async()=>payload};
  }));
  render(<MemoryRouter initialEntries={["/evidence"]}><App /></MemoryRouter>);
  expect(await screen.findByRole("heading", {name:"Evidence corpus"})).toBeInTheDocument();
  for (const label of [
    "Study", "Topic", "Source", "Search ID", "Discovery from", "Discovery to",
    "Screening status", "Final corpus", "Year", "Publication type", "Review status", "Page size",
  ]) {
    expect(screen.getByLabelText(label)).toBeInTheDocument();
  }
});


test("search run workspace exposes reproducibility filters", async () => {
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    const payload = url.includes("/studies") ? [] : [];
    return {ok:true, json:async()=>payload};
  }));
  render(<MemoryRouter initialEntries={["/search-runs"]}><App /></MemoryRouter>);
  expect(await screen.findByRole("heading", {name:"Search runs"})).toBeInTheDocument();
  for (const label of ["Study", "Topic", "Provider", "Mode", "Run status", "Search ID", "Label", "From date", "To date"]) {
    expect(screen.getByLabelText(label)).toBeInTheDocument();
  }
});
