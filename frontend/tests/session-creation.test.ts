import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { api } from "../src/api/client";
import { useWorkbench } from "../src/stores/workbench";

beforeEach(() => {
  setActivePinia(createPinia());
  sessionStorage.clear();
});
afterEach(() => vi.restoreAllMocks());

it("does not select a late created Session after changing projects", async () => {
  const store = useWorkbench();
  store.selectedWorkspace = "first";
  let finish!: (value: { session_id: string }) => void;
  vi.spyOn(api, "createSession").mockImplementation(
    () =>
      new Promise((resolve) => {
        finish = resolve;
      }),
  );
  vi.spyOn(api, "sessions").mockResolvedValue([]);
  const select = vi.spyOn(api, "session");
  const pending = store.createSession();
  await store.selectWorkspace("second");
  finish({ session_id: "late-first" });
  await pending;
  expect(store.selectedWorkspace).toBe("second");
  expect(store.selectedSession).toBe("");
  expect(select).not.toHaveBeenCalled();
  expect(store.creating).toBe(false);
  store.dispose();
});

it("ignores a late project listing after another project is selected", async () => {
  const store = useWorkbench();
  let finish!: (value: []) => void;
  vi.spyOn(api, "sessions")
    .mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    )
    .mockResolvedValue([]);
  const first = store.selectWorkspace("first");
  await store.selectWorkspace("second");
  finish([]);
  await first;
  expect(store.selectedWorkspace).toBe("second");
  expect(store.selectedSession).toBe("");
  store.dispose();
});
