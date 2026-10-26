import { afterEach, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import {
  runToolsApi,
  type RunToolCatalog,
  type RunToolDetail,
} from "../src/api/runTools";
import RunToolsPanel from "../src/components/RunToolsPanel.vue";

const item = (extra: Record<string, unknown> = {}) => ({
  name: "mcp__demo__alpha",
  source: "mcp:demo",
  version: "a".repeat(64),
  description: "Reads demo paths",
  effect: "readonly",
  environment: "local",
  status: "available",
  disabled_reason: null,
  provided: false,
  schema_status: "full",
  schema_chars: null,
  loaded: false,
  loaded_seq: null,
  load_record: null,
  ...extra,
});
const catalog = (extra: Partial<RunToolCatalog> = {}): RunToolCatalog => ({
  run_id: "r1",
  catalog_frozen: true,
  catalog_version: "c".repeat(64),
  frozen_seq: 4,
  total: 2,
  catalog_total: 2,
  offset: 0,
  limit: 20,
  items: [
    item(),
    item({ name: "mcp__demo__beta", source: "mcp:demo", effect: "effect" }),
  ],
  loaded_count: 0,
  failed_loads: [],
  stale_receipts: 0,
  notes: [],
  ...extra,
});
const detail = (extra: Partial<RunToolDetail> = {}): RunToolDetail => ({
  ...item(),
  input_schema: {
    type: "object",
    properties: { path: { type: "string" } },
    required: ["path"],
  },
  ...extra,
});
afterEach(() => {
  vi.restoreAllMocks();
});

function mountPanel(props: Record<string, unknown> = {}) {
  return mount(RunToolsPanel, {
    props: { runId: "r1", refreshKey: "running:1", ...props },
    attachTo: document.body,
  });
}

it("renders frozen catalog with states and never an execute button", async () => {
  vi.spyOn(runToolsApi, "catalog").mockResolvedValue(
    catalog({
      items: [
        item({
          loaded: true,
          loaded_seq: 9,
          load_record: { schema_digest: "a".repeat(64) },
        }),
        item({ name: "read_file", source: "builtin", provided: true }),
        item({ name: "mcp__demo__beta", effect: "effect" }),
      ],
      loaded_count: 1,
      total: 3,
      catalog_total: 3,
    }),
  );
  const wrapper = mountPanel();
  await flushPromises();
  expect(wrapper.text()).toContain("工具目录（Run 冻结事实）");
  expect(wrapper.text()).toContain("已加载 1");
  expect(wrapper.text()).toContain("常驻");
  const rows = wrapper.findAll(".run-tool-row");
  expect(rows.length).toBe(3);
  expect(wrapper.find("button.execute").exists()).toBe(false);
  expect(wrapper.text()).toContain("展示与发现不等于授权");
  wrapper.unmount();
});

it("expands the precise parameter contract and required permission", async () => {
  const detailSpy = vi.spyOn(runToolsApi, "detail").mockResolvedValue(detail());
  vi.spyOn(runToolsApi, "catalog").mockResolvedValue(catalog());
  const wrapper = mountPanel();
  await flushPromises();
  await wrapper.findAll(".run-tool-row")[0].trigger("click");
  await flushPromises();
  expect(detailSpy).toHaveBeenCalledWith("r1", "mcp__demo__alpha");
  expect(wrapper.text()).toContain('"required"');
  expect(wrapper.text()).toContain("只读");
  // Collapse again clears the open contract.
  await wrapper.findAll(".run-tool-row")[0].trigger("click");
  expect(wrapper.find(".run-tool-schema").exists()).toBe(false);
  wrapper.unmount();
});

it("filters by query, source and status", async () => {
  const catalogSpy = vi
    .spyOn(runToolsApi, "catalog")
    .mockResolvedValue(catalog());
  const wrapper = mountPanel();
  await flushPromises();
  await wrapper.find('input[aria-label="搜索工具"]').setValue("alpha");
  await flushPromises();
  expect(catalogSpy).toHaveBeenLastCalledWith("r1", {
    query: "alpha",
    source: "",
    status: "all",
    offset: 0,
    limit: 20,
  });
  await wrapper.find('select[aria-label="按状态筛选"]').setValue("loaded");
  await flushPromises();
  expect(catalogSpy).toHaveBeenLastCalledWith("r1", {
    query: "alpha",
    source: "",
    status: "loaded",
    offset: 0,
    limit: 20,
  });
  wrapper.unmount();
});

it("shows missing history, stale receipts and failures explicitly", async () => {
  vi.spyOn(runToolsApi, "catalog").mockResolvedValue(
    catalog({
      catalog_frozen: false,
      catalog_version: null,
      frozen_seq: null,
      items: [],
      total: 0,
      catalog_total: 0,
      loaded_count: 0,
      stale_receipts: 2,
      failed_loads: [
        {
          tool: "mcp__demo__alpha",
          code: "handler_unavailable",
          reason: "no executable handler",
          seq: 7,
          catalog_version: "c".repeat(64),
        },
      ],
      notes: [
        "此 Run 没有冻结工具目录事实（旧历史 Run，或当时没有按需工具目录）。",
      ],
    }),
  );
  const wrapper = mountPanel();
  await flushPromises();
  expect(wrapper.text()).toContain("没有冻结工具目录事实");
  expect(wrapper.text()).toContain("2 条加载事实与冻结目录版本不一致");
  expect(wrapper.text()).toContain("加载失败（第 7 项事实）");
  expect(wrapper.text()).toContain("handler_unavailable");
  wrapper.unmount();
});

it("keeps a late catalog response from overwriting the switched run", async () => {
  const pending: { release: (() => void) | null } = { release: null };
  const catalogSpy = vi
    .spyOn(runToolsApi, "catalog")
    .mockImplementation((runId: string) => {
      if (runId === "r1") {
        return new Promise<RunToolCatalog>((resolve) => {
          pending.release = () => resolve(catalog({ run_id: "r1", total: 99 }));
        });
      }
      return Promise.resolve(catalog({ run_id: "r2", total: 1, items: [] }));
    });
  const wrapper = mountPanel({ runId: "r1" });
  await flushPromises();
  expect(catalogSpy).toHaveBeenLastCalledWith(
    "r1",
    expect.objectContaining({ limit: 20 }),
  );
  await wrapper.setProps({ runId: "r2", refreshKey: "running:2" });
  await flushPromises();
  expect(wrapper.text()).not.toContain("99");
  pending.release?.();
  await flushPromises();
  expect(wrapper.text()).not.toContain("99");
  expect(wrapper.find('[aria-label="本 Run 工具目录"]').exists()).toBe(true);
  wrapper.unmount();
});

it("paginates bounded pages instead of pretending completeness", async () => {
  const many = Array.from({ length: 30 }, (_unused, index) =>
    item({ name: `mcp__demo__tool_${index}` }),
  );
  vi.spyOn(runToolsApi, "catalog").mockResolvedValue(
    catalog({
      items: many.slice(0, 20),
      total: 30,
      catalog_total: 30,
    }),
  );
  const wrapper = mountPanel();
  await flushPromises();
  expect(wrapper.text()).toContain("第 1-20 / 30 条");
  const spy = vi.spyOn(runToolsApi, "catalog");
  await wrapper
    .findAll("button")
    .find((b) => b.text() === "下一页")!
    .trigger("click");
  expect(spy).toHaveBeenLastCalledWith(
    "r1",
    expect.objectContaining({ offset: 20, limit: 20 }),
  );
  wrapper.unmount();
});

it("keeps an expanded contract across event refresh and clears prior run immediately", async () => {
  vi.spyOn(runToolsApi, "catalog").mockResolvedValue(catalog());
  vi.spyOn(runToolsApi, "detail").mockResolvedValue(detail());
  const wrapper = mountPanel();
  await flushPromises();
  await wrapper.findAll(".run-tool-row")[0]!.trigger("click");
  await flushPromises();
  expect(wrapper.find(".run-tool-schema").exists()).toBe(true);
  await wrapper.setProps({ refreshKey: "running:2" });
  await flushPromises();
  expect(wrapper.find(".run-tool-schema").exists()).toBe(true);
  vi.mocked(runToolsApi.catalog).mockImplementationOnce(
    () => new Promise(() => {}),
  );
  await wrapper.setProps({ runId: "other" });
  expect(wrapper.findAll(".run-tool-row")).toHaveLength(0);
  expect(wrapper.find(".run-tool-schema").exists()).toBe(false);
  wrapper.unmount();
});
