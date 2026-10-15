import { beforeEach, describe, expect, it, vi } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import SkillsView from "../src/views/SkillsView.vue";
import {
  skillsApi,
  type SkillStatus,
  type PackagePreview,
} from "../src/api/skills";
import { useWorkbench } from "../src/stores/workbench";
const preview: PackagePreview = {
  name: "code-review",
  version: "a".repeat(64),
  description: "Review changes",
  manifest: [{ path: "SKILL.md", size: 40, sha256: "b".repeat(64) }],
  diagnostics: [],
  valid: true,
};
const initial: SkillStatus = {
  installed: [preview],
  configuration: { enabled_user: {}, disabled_project: [], selected: [] },
  configuration_hash: "c".repeat(64),
  enabled: { skills: [], selected: [], diagnostics: [] },
  project_packages: { skills: [], selected: [], diagnostics: [] },
};
beforeEach(() => {
  sessionStorage.clear();
  setActivePinia(createPinia());
  useWorkbench().selectedWorkspace = "one";
  vi.spyOn(skillsApi, "status").mockImplementation(async () =>
    structuredClone(initial),
  );
  vi.spyOn(skillsApi, "bundled").mockResolvedValue([preview]);
});
describe("Skill management", () => {
  it("reviews exact content before importing and never implicitly enables a user package", async () => {
    vi.spyOn(skillsApi, "preview").mockResolvedValue(preview);
    vi.spyOn(skillsApi, "import").mockResolvedValue(preview);
    vi.spyOn(skillsApi, "configure");
    const wrapper = mount(SkillsView);
    await flushPromises();
    await wrapper
      .findAll("button")
      .find((b) => b.text() === "检查导入内容")!
      .trigger("click");
    await flushPromises();
    await wrapper
      .findAll("button")
      .find((b) => b.text() === "导入已检查版本")!
      .trigger("click");
    await flushPromises();
    expect(skillsApi.import).toHaveBeenCalledWith("one", {
      bundled: "code-review",
      scope: "user",
      expected_version: preview.version,
    });
    expect(skillsApi.configure).not.toHaveBeenCalled();
    expect(wrapper.text()).toContain("已安装固定版本");
    wrapper.unmount();
  });
  it("saves an explicit version and selection with the observed configuration revision", async () => {
    vi.spyOn(skillsApi, "configure").mockResolvedValue({});
    const wrapper = mount(SkillsView);
    await flushPromises();
    await wrapper
      .find('[aria-label="code-review 固定版本"]')
      .setValue(preview.version);
    await wrapper.find('input[type="checkbox"]').setValue(true);
    await wrapper
      .findAll("button")
      .find((b) => b.text() === "保存项目配置")!
      .trigger("click");
    await flushPromises();
    expect(skillsApi.configure).toHaveBeenCalledWith("one", {
      enabled_user: { "code-review": preview.version },
      disabled_project: [],
      selected: ["user:code-review"],
      expected_configuration_hash: initial.configuration_hash,
    });
    wrapper.unmount();
  });
  it("invalidates review when source changes and ignores late results from another project", async () => {
    let finish!: (value: PackagePreview) => void;
    vi.spyOn(skillsApi, "preview").mockReturnValue(
      new Promise((resolve) => (finish = resolve)),
    );
    const wrapper = mount(SkillsView);
    await flushPromises();
    await wrapper
      .findAll("button")
      .find((b) => b.text() === "检查导入内容")!
      .trigger("click");
    useWorkbench().selectedWorkspace = "two";
    await flushPromises();
    finish(preview);
    await flushPromises();
    expect(wrapper.text()).not.toContain("导入已检查版本");
    vi.mocked(skillsApi.preview).mockResolvedValue(preview);
    await wrapper
      .findAll("button")
      .find((b) => b.text() === "检查导入内容")!
      .trigger("click");
    await flushPromises();
    expect(wrapper.text()).toContain("导入已检查版本");
    await wrapper.find('[aria-label="导入作用域"]').setValue("project");
    expect(wrapper.text()).not.toContain("导入已检查版本");
    wrapper.unmount();
  });
  it("shows invalid-package diagnostics and disables import", async () => {
    vi.spyOn(skillsApi, "preview").mockResolvedValue({
      ...preview,
      valid: false,
      diagnostics: [
        {
          code: "unsafe",
          message: "unsafe path",
          severity: "error",
          source: "zip",
        },
      ],
    });
    const wrapper = mount(SkillsView);
    await flushPromises();
    await wrapper
      .findAll("button")
      .find((b) => b.text() === "检查导入内容")!
      .trigger("click");
    await flushPromises();
    expect(wrapper.text()).toContain("unsafe path");
    expect(
      wrapper
        .findAll("button")
        .find((b) => b.text() === "导入已检查版本")!
        .attributes("disabled"),
    ).toBeDefined();
    wrapper.unmount();
  });
  it("retains uncertain mutation identity across retries without persisting package contents", async () => {
    const fetch = vi
      .fn()
      .mockRejectedValueOnce(new TypeError("offline"))
      .mockResolvedValue({ ok: true, json: async () => preview });
    vi.stubGlobal("fetch", fetch);
    try {
      const body = {
        scope: "user" as const,
        directory: "/private/skill-input",
        expected_version: preview.version,
      };
      await expect(skillsApi.import("one", body)).rejects.toThrow("offline");
      expect(JSON.stringify({ ...sessionStorage })).not.toContain(
        "/private/skill-input",
      );
      await skillsApi.import("one", body);
      expect(fetch.mock.calls[0]![1].headers["Idempotency-Key"]).toBe(
        fetch.mock.calls[1]![1].headers["Idempotency-Key"],
      );
      expect(sessionStorage.length).toBe(0);
    } finally {
      vi.unstubAllGlobals();
    }
  });
});
