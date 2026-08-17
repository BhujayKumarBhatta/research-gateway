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
