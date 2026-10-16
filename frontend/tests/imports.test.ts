import { beforeEach, afterEach, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { createMemoryHistory, createRouter } from "vue-router";
import {
  importsApi,
  importState,
  type ImportDetail,
  type ImportIntent,
} from "../src/api/historyImports";
import HistoryImportsView from "../src/views/HistoryImportsView.vue";
import LegacyMessages from "../src/components/LegacyMessages.vue";
import { useWorkbench } from "../src/stores/workbench";
const preview: ImportDetail = {
  run_id: "preview",
  session_id: "control",
  project_id: "one",
  action: "preview",
  status: "completed",
  imported: false,
  result: null,
  error: null,
  events_url: "/events",
  preview: {
    sha256: "a".repeat(64),
    bytes: 100,
    counts: { sessions: 1, runs: 1, messages: 1, events: 1 },
  },
};
beforeEach(() => {
  sessionStorage.clear();
  setActivePinia(createPinia());
  const store = useWorkbench();
  store.workspaces = [
    { workspace_id: "one", project_id: "one", name: "one", path: "/one" },
    { workspace_id: "two", project_id: "two", name: "two", path: "/two" },
  ];
  store.selectedWorkspace = "one";
});
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});
async function render() {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: "/imports", component: HistoryImportsView },
      { path: "/history", component: { template: "<p>history</p>" } },
    ],
  });
  await router.push("/imports?operation=preview");
  await router.isReady();
  const wrapper = mount(HistoryImportsView, { global: { plugins: [router] } });
  await flushPromises();
  return wrapper;
}
it("requires review and submits the exact preview hash", async () => {
  vi.spyOn(importsApi, "list").mockResolvedValue([preview]);
  vi.spyOn(importsApi, "detail").mockResolvedValue(preview);
  const submit = vi
    .spyOn(importsApi, "submit")
    .mockRejectedValue(new Error("fixture"));
  const wrapper = await render();
  const button = wrapper
    .findAll("button")
    .find((b) => b.text() === "确认导入历史")!;
  expect(button.attributes("disabled")).toBeDefined();
  await wrapper.get('input[type="checkbox"]').setValue(true);
  await button.trigger("click");
  await flushPromises();
  expect(submit).toHaveBeenCalledWith("one", {
    action: "import",
    body: {
      preview_id: "preview",
      archive_sha256: "a".repeat(64),
      confirmed: true,
    },
  });
  wrapper.unmount();
});
it("does not display a foreign project's preview or late previous-project result", async () => {
  let finish!: (value: ImportDetail) => void;
  vi.spyOn(importsApi, "list").mockResolvedValue([]);
  vi.spyOn(importsApi, "detail").mockReturnValue(
    new Promise((resolve) => {
      finish = resolve;
    }),
  );
  const wrapper = await render();
  useWorkbench().selectedWorkspace = "two";
  await flushPromises();
  finish(preview);
  await flushPromises();
  expect(wrapper.find('input[type="checkbox"]').exists()).toBe(false);
  expect(wrapper.text()).not.toContain("内容 SHA256");
  wrapper.unmount();
});
it("unknown import acceptance keeps the original body and idempotency key", async () => {
  const fetch = vi
    .fn()
    .mockRejectedValueOnce(new TypeError("lost"))
    .mockResolvedValue(Response.json({ run_id: "r" }));
  vi.stubGlobal("fetch", fetch);
  const intent: ImportIntent = {
    action: "import",
    body: {
      preview_id: "preview",
      archive_sha256: "a".repeat(64),
      confirmed: true,
    },
  };
  await expect(importsApi.submit("one", intent)).rejects.toThrow();
  expect(importState("one").pending).toEqual(intent);
  await expect(
    importsApi.submit("one", { action: "preview", body: { source: "/other" } }),
  ).rejects.toThrow("尚未确认");
  await importsApi.submit("one", importState("one").pending!);
  expect(fetch).toHaveBeenCalledTimes(2);
  expect(fetch.mock.calls[0]![1].body).toBe(fetch.mock.calls[1]![1].body);
  expect(
    new Headers(fetch.mock.calls[0]![1].headers).get("Idempotency-Key"),
  ).toBe(new Headers(fetch.mock.calls[1]![1].headers).get("Idempotency-Key"));
});
it("pages immutable legacy messages and renders content as text", async () => {
  const fetch = vi
    .fn()
    .mockResolvedValueOnce(
      Response.json(
        Array.from({ length: 50 }, (_, i) => ({
          seq: i + 10,
          role: "user",
          content: "<script>historical</script>",
        })),
      ),
    )
    .mockResolvedValueOnce(
      Response.json([{ seq: 2, role: "assistant", content: "older" }]),
    );
  vi.stubGlobal("fetch", fetch);
  const wrapper = mount(LegacyMessages, { props: { session: "legacy" } });
  await flushPromises();
  expect(wrapper.findAll("pre")).toHaveLength(50);
  expect(wrapper.find("script").exists()).toBe(false);
  await wrapper.get("button").trigger("click");
  await flushPromises();
  expect(fetch.mock.calls[1]![0]).toContain("before=10");
  expect(wrapper.findAll("pre")[0]!.text()).toBe("older");
  expect(wrapper.find("button").exists()).toBe(false);
  wrapper.unmount();
});
it("drops late message pages after changing sessions", async () => {
  let finish!: (value: Response) => void;
  vi.stubGlobal(
    "fetch",
    vi
      .fn()
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            finish = resolve;
          }),
      )
      .mockResolvedValue(
        Response.json([{ seq: 1, role: "user", content: "new session" }]),
      ),
  );
  const wrapper = mount(LegacyMessages, { props: { session: "old" } });
  await wrapper.setProps({ session: "new" });
  await flushPromises();
  finish(Response.json([{ seq: 1, role: "user", content: "old session" }]));
  await flushPromises();
  expect(wrapper.text()).toContain("new session");
  expect(wrapper.text()).not.toContain("old session");
  wrapper.unmount();
});
