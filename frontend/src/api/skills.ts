import type { components } from "./schema";
import { request } from "./client";
import { durableMutation } from "./mutations";

export type PackageInput = components["schemas"]["PackageBody"];
export type SkillConfiguration =
  components["schemas"]["SkillConfigurationBody"];
export interface Diagnostic {
  code: string;
  message: string;
  source: string;
  severity: string;
}
export interface SkillPackage {
  name: string;
  description: string;
  version: string;
  manifest: Array<{ path: string; size: number; sha256: string }>;
  diagnostics: Diagnostic[];
}
export interface PackagePreview extends SkillPackage {
  valid: boolean;
}
export interface Catalog {
  skills: Array<SkillPackage & { skill_id: string; scope: string }>;
  selected: string[];
  diagnostics: Diagnostic[];
}
export interface SkillStatus {
  installed: SkillPackage[];
  configuration: SkillConfiguration;
  configuration_hash: string;
  enabled: Catalog;
  project_packages: Catalog;
}
export interface RunSkills {
  run_id: string;
  snapshot: Catalog | null;
  loaded: Array<{
    skill_id: string;
    version: string;
    characters: number;
    seq: number;
  }>;
}
export interface SkillDiff {
  added: string[];
  removed: string[];
  changed: string[];
  instructions_diff: string;
  diff_truncated: boolean;
}
const id = encodeURIComponent;
const root = (workspace: string) => `/api/workspaces/${id(workspace)}/skills`;

const mutate = <T>(path: string, method: string, body: unknown) =>
  durableMutation<T>("skill", path, method, body);
export const skillsApi = {
  status: (workspace: string) => request<SkillStatus>(root(workspace)),
  bundled: () => request<PackagePreview[]>("/api/skills/bundled"),
  preview: (workspace: string, body: PackageInput) =>
    request<PackagePreview>(`${root(workspace)}/preview`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  import: (workspace: string, body: PackageInput) =>
    mutate<SkillPackage>(`${root(workspace)}/import`, "POST", body),
  configure: (workspace: string, body: SkillConfiguration) =>
    mutate<SkillConfiguration>(`${root(workspace)}/configuration`, "PUT", body),
  run: (run: string) => request<RunSkills>(`/api/runs/${id(run)}/skills`),
  diff: (name: string, before: string, after: string) =>
    request<SkillDiff>(
      `/api/skills/${id(name)}/versions/diff?${new URLSearchParams({ before, after })}`,
    ),
  resource: (name: string, version: string, path: string, offset = 0) =>
    request<{ content: string }>(
      `/api/skills/${id(name)}/versions/${id(version)}/resource?${new URLSearchParams({ path, offset: String(offset), limit: "12000" })}`,
    ),
};
