import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import DiffViewer from "../src/components/DiffViewer.vue";
import FileOperationPanel from "../src/components/FileOperationPanel.vue";
import ToolActivity from "../src/components/ToolActivity.vue";
import { ApiError } from "../src/api/client";
import {
  fileOperationsApi,
  type FileOperation,
  type FileOperationDetail,
} from "../src/api/fileOperations";
const patch = [
  "--- a/app.py",
  "+++ b/app.py",
  "@@ -4,2 +4,2 @@",
  " user's prior change",
  "-before <script>alert(1)</script>",
  "+after <img src=x onerror=alert(1)>",
  "",
].join("\n");
const summary = (
  status: FileOperation["status"] = "completed",
): FileOperation => ({
  operation_id: "op1",
  session_id: "s1",
  workspace_id: "w1",
  run_id: "r1",
  call_id: "c1",
  path: "app.py",
  tool: "edit_file",
  status,
  change_kind: "modified",
  before: { sha256: "before" },
  after: { sha256: "after" },
});
const detail = (
  status: FileOperation["status"] = "completed",
): FileOperationDetail => ({
  ...summary(status),
  patch,
  diff_status: "available",
  diff_message: null,
});
beforeEach(() => sessionStorage.clear());
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});
async function openPanel() {
  const wrapper = mount(FileOperationPanel, { props: { sessionId: "s1" } });
  expect(fileOperationsApi.list).not.toHaveBeenCalled();
  (wrapper.get("details").element as HTMLDetailsElement).open = true;
  await flushPromises();
  return wrapper;
}
async function inspect(wrapper: Awaited<ReturnType<typeof openPanel>>) {
  await wrapper.get(".operation-list button").trigger("click");
  await flushPromises();
}
it("shows supplied operation lines with old/new numbers and inert source text", () => {
  const wrapper = mount(DiffViewer, { props: { patch } });
  expect(wrapper.get('[aria-label="改动文件列表"]').text()).toContain("app.py");
  expect(wrapper.get(".diff-removed").text()).toContain("before <script>");
  expect(wrapper.get(".diff-added").text()).toContain("after <img");
  expect(wrapper.get(".diff-context").text()).toContain("user's prior change");
  expect(
    wrapper
      .get(".diff-removed")
      .findAll(".line-number")
      .map((item) => item.text()),
  ).toEqual(["5", ""]);
  expect(
    wrapper
      .get(".diff-added")
      .findAll(".line-number")
      .map((item) => item.text()),
  ).toEqual(["", "5"]);
  expect(wrapper.find("script").exists()).toBe(false);
  expect(wrapper.find("img").exists()).toBe(false);
});
it("distinguishes new files, deleted files and binary content", async () => {
  const wrapper = mount(DiffViewer, {
    props: {
      patch: [
        "diff --git a/new.py b/new.py",
        "new file mode 100644",
        "--- /dev/null",
        "+++ b/new.py",
        "@@ -0,0 +1 @@",
        "+new",
        "diff --git a/old.py b/old.py",
        "deleted file mode 100644",
        "--- a/old.py",
        "+++ /dev/null",
        "@@ -1 +0,0 @@",
        "-old",
        "diff --git a/image.png b/image.png",
        "Binary files a/image.png and b/image.png differ",
        "",
      ].join("\n"),
    },
  });
  const files = wrapper.findAll("details.diff-file");
  expect(files[0]!.get("summary").text()).toContain("新增文件");
  expect(files[1]!.get("summary").text()).toContain("删除文件");
  expect(files[1]!.find("table").exists()).toBe(false);
  (files[2]!.element as HTMLDetailsElement).open = true;
  await files[2]!.trigger("toggle");
  expect(files[2]!.text()).toContain("二进制");
});
it("renders long diffs in bounded pages and explains empty, unsupported and oversized diffs", async () => {
  const long = `--- a/large\n+++ b/large\n@@ -0,0 +1,500 @@\n${Array.from({ length: 500 }, (_, i) => `+${i}`).join("\n")}`;
  const wrapper = mount(DiffViewer, { props: { patch: long } });
  expect(wrapper.findAll("tr")).toHaveLength(200);
  await wrapper
    .findAll("button")
    .find((item) => item.text().startsWith("再显示 200 行"))!
    .trigger("click");
  expect(wrapper.findAll("tr")).toHaveLength(400);
  await wrapper.setProps({ patch: "" });
  expect(wrapper.text()).toContain("无内容差异");
  await wrapper.setProps({ patch: "unrecognized" });
  expect(wrapper.text()).toContain("无法解析");
  await wrapper.setProps({ patch: "a".repeat(2_000_001) });
  expect(wrapper.text()).toContain("超过页面展示上限");
  expect(wrapper.find("table").exists()).toBe(false);
});
it("loads session operations and one detail only on request, then explicitly undoes once", async () => {
  vi.spyOn(fileOperationsApi, "list").mockResolvedValue({
    operations: [summary()],
  });
  const get = vi.spyOn(fileOperationsApi, "detail").mockResolvedValue(detail());
  let finish!: (value: FileOperation) => void;
  const undo = vi.spyOn(fileOperationsApi, "undo").mockImplementation(
    () =>
      new Promise((resolve) => {
        finish = resolve;
      }),
  );
  const wrapper = await openPanel();
  expect(get).not.toHaveBeenCalled();
  await inspect(wrapper);
  expect(get).toHaveBeenCalledWith("s1", "op1");
  expect(wrapper.text()).toContain("user's prior change");
  expect(undo).not.toHaveBeenCalled();
  const button = wrapper
    .findAll("button")
    .find((item) => item.text() === "撤销本次修改")!;
  await button.trigger("click");
  await button.trigger("click");
  expect(undo).toHaveBeenCalledTimes(1);
  finish(summary("undone"));
  await flushPromises();
  expect(wrapper.text()).toContain("本次修改已撤销");
  expect(
    wrapper.findAll("button").some((item) => item.text() === "撤销本次修改"),
  ).toBe(false);
});
it("shows a conflict without claiming success and refreshes persisted status", async () => {
  const list = vi
    .spyOn(fileOperationsApi, "list")
    .mockResolvedValue({ operations: [summary()] });
  vi.spyOn(fileOperationsApi, "detail").mockResolvedValue(detail());
  vi.spyOn(fileOperationsApi, "undo").mockRejectedValue(
    new ApiError(409, "Current file fingerprint conflict", "rejected"),
  );
  const wrapper = await openPanel();
  await inspect(wrapper);
  await wrapper
    .findAll("button")
    .find((item) => item.text() === "撤销本次修改")!
    .trigger("click");
  await flushPromises();
  expect(wrapper.get('[role="alert"]').text()).toContain("撤销冲突");
  expect(wrapper.text()).toContain("未覆盖当前文件");
  expect(wrapper.text()).not.toContain("本次修改已撤销");
  expect(list).toHaveBeenCalledTimes(2);
});
it.each([
  "prepared",
  "needs_review",
  "undo_prepared",
  "failed",
  "no_change",
  "undone",
] as const)(
  "does not offer undo for persisted %s after refresh or remount",
  async (status) => {
    vi.spyOn(fileOperationsApi, "list").mockResolvedValue({
      operations: [summary(status)],
    });
    vi.spyOn(fileOperationsApi, "detail").mockResolvedValue({
      ...detail(status),
      patch: null,
      diff_status: "unavailable",
      diff_message: "未确认完整写入，请核验。",
    });
    const wrapper = await openPanel();
    await inspect(wrapper);
    expect(wrapper.text()).toContain("未确认完整写入，请核验");
    expect(
      wrapper.findAll("button").some((item) => item.text() === "撤销本次修改"),
    ).toBe(false);
    wrapper.unmount();
  },
);
it("shows added operation identity and explains unavailable large diffs", async () => {
  vi.spyOn(fileOperationsApi, "list").mockResolvedValue({
    operations: [{ ...summary(), change_kind: "added" }],
  });
  vi.spyOn(fileOperationsApi, "detail").mockResolvedValue({
    ...detail(),
    change_kind: "added",
    patch: null,
    diff_status: "too_large",
    diff_message: "差异过大，无法展示。",
  });
  const wrapper = await openPanel();
  await inspect(wrapper);
  expect(wrapper.text()).toContain("新增文件");
  expect(wrapper.text()).toContain("差异过大，无法展示");
  expect(wrapper.text()).toContain("r1");
  expect(wrapper.text()).toContain("c1");
  expect(wrapper.text()).toContain("w1");
});
it("ignores a late detail response from a previous session", async () => {
  vi.spyOn(fileOperationsApi, "list").mockResolvedValue({
    operations: [summary()],
  });
  let finish!: (value: FileOperationDetail) => void;
  vi.spyOn(fileOperationsApi, "detail").mockImplementation(
    () =>
      new Promise((resolve) => {
        finish = resolve;
      }),
  );
  const wrapper = await openPanel();
  await wrapper.get(".operation-list button").trigger("click");
  await wrapper.setProps({ sessionId: "s2" });
  finish(detail());
  await flushPromises();
  expect(wrapper.find('[aria-label="单次文件操作详情"]').exists()).toBe(false);
});
it("exposes persisted operations inside ordinary tool activity even for an unknown outcome", () => {
  const wrapper = mount(ToolActivity, {
    props: {
      sessionId: "s1",
      tools: { c1: { name: "write_file", status: "outcome_unknown" } },
    },
  });
  expect(wrapper.text()).toContain("结果待确认");
  expect(wrapper.findComponent(FileOperationPanel).props("sessionId")).toBe(
    "s1",
  );
});
it("uses a durable identity for uncertain undo retries without sending a body", async () => {
  const fetcher = vi
    .fn()
    .mockRejectedValueOnce(new TypeError("connection lost"))
    .mockResolvedValueOnce({ ok: true, json: async () => summary("undone") });
  vi.stubGlobal("fetch", fetcher);
  await expect(fileOperationsApi.undo("s/1", "op/1")).rejects.toThrow(
    "connection lost",
  );
  const first = fetcher.mock.calls[0]!;
  await fileOperationsApi.undo("s/1", "op/1");
  expect(first[0]).toBe("/api/sessions/s%2F1/file-operations/op%2F1/undo");
  expect(first[1].body).toBeUndefined();
  expect(first[1].headers["Idempotency-Key"]).toBe(
    fetcher.mock.calls[1]![1].headers["Idempotency-Key"],
  );
});

it("bounds the initial file list and loads further delivery files explicitly", async () => {
  const wrapper = mount(DiffViewer, {
    props: {
      patch: Array.from(
        { length: 60 },
        (_, index) =>
          `diff --git a/file${index} b/file${index}\n--- a/file${index}\n+++ b/file${index}\n@@ -1 +1 @@\n-old\n+new`,
      ).join("\n"),
    },
  });
  expect(wrapper.findAll("details.diff-file")).toHaveLength(50);
  const more = wrapper
    .findAll("button")
    .find((item) => item.text().includes("再显示 50 个文件"))!;
  await more.trigger("click");
  expect(wrapper.findAll("details.diff-file")).toHaveLength(60);
  expect(wrapper.findAll("table")).toHaveLength(1);
});
it("gets the persisted undone state after remount and displays an empty operation list", async () => {
  const list = vi
    .spyOn(fileOperationsApi, "list")
    .mockResolvedValue({ operations: [summary("undone")] });
  vi.spyOn(fileOperationsApi, "detail").mockResolvedValue(detail("undone"));
  const first = await openPanel();
  await inspect(first);
  expect(first.text()).toContain("已撤销");
  first.unmount();
  list.mockClear();
  list.mockResolvedValue({ operations: [] });
  const refreshed = await openPanel();
  expect(refreshed.text()).toContain("尚无受控内置文件操作记录");
});

it("opens a listed file without replacing the application's hash route", async () => {
  window.location.hash = "#/deliveries?delivery=selected";
  const before = window.location.hash;
  const wrapper = mount(DiffViewer, {
    attachTo: document.body,
    props: {
      patch: `diff --git a/first b/first\n${patch}\ndiff --git a/second b/second\n--- a/second\n+++ b/second\n@@ -1 +1 @@\n-before\n+after\n`,
    },
  });
  await wrapper.findAll(".diff-files button")[1]!.trigger("click");
  await flushPromises();
  expect(window.location.hash).toBe(before);
  expect(
    (wrapper.findAll("details.diff-file")[1]!.element as HTMLDetailsElement)
      .open,
  ).toBe(true);
  expect(wrapper.findAll("details.diff-file")[1]!.text()).toContain("after");
  wrapper.unmount();
});

it("bounds parsing for patches with excessive tiny lines", () => {
  const wrapper = mount(DiffViewer, {
    props: { patch: patch + "\n+tiny".repeat(20_001) },
  });
  expect(wrapper.text()).toContain("展示上限");
  expect(wrapper.findAll("tr")).toHaveLength(0);
});
