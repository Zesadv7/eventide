import { ApiError, request } from "./client";
import { durableMutation } from "./mutations";
import type { components } from "./schema";
export type Delivery = components["schemas"]["DeliveryDetail"];
export type Operation = components["schemas"]["DeliveryOperation"];
export type Receipt = components["schemas"]["DeliveryReceipt"];
export type Patch = components["schemas"]["DeliveryPatch"];
export type Intent =
  | { action: "prepare"; body: components["schemas"]["DeliveryPrepareBody"] }
  | {
      action: "repair";
      delivery: string;
      body: components["schemas"]["DeliveryRepairBody"];
    }
  | {
      action: "apply";
      delivery: string;
      body: components["schemas"]["DeliveryApplyBody"];
    };
const prefix = "eventide.delivery-pending.v1.";
const active = new Set<string>();
const listeners = new Set<() => void>();
const receipts = new Map<string, Receipt>();
export function deliveryState(project: string) {
  let pending: Intent | null = null;
  try {
    const value = JSON.parse(
      sessionStorage.getItem(prefix + project) || "null",
    );
    if (
      value &&
      ["prepare", "repair", "apply"].includes(value.action) &&
      value.body &&
      (value.action === "prepare" ||
        /^delivery_[a-f0-9]{16}$/.test(value.delivery))
    )
      pending = value;
  } catch {
    /* Invalid local state is not an executable request. */
  }
  return {
    pending,
    active: active.has(project),
    receipt: receipts.get(project),
  };
}
function changed() {
  for (const listener of listeners) listener();
}
export function observeDeliveries(listener: () => void) {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}
export const deliveriesApi = {
  list: (project: string) =>
    request<Delivery[]>(
      `/api/projects/${encodeURIComponent(project)}/deliveries`,
    ),
  operations: (project: string, before?: string) =>
    request<Operation[]>(
      `/api/projects/${encodeURIComponent(project)}/delivery-operations?limit=20${before ? `&before=${encodeURIComponent(before)}` : ""}`,
    ),
  operation: (id: string) =>
    request<Operation>(`/api/delivery-operations/${encodeURIComponent(id)}`),
  detail: (id: string) =>
    request<Delivery>(`/api/deliveries/${encodeURIComponent(id)}`),
  patch: (id: string) =>
    request<Patch>(`/api/deliveries/${encodeURIComponent(id)}/patch`),
  cancel: (id: string) =>
    durableMutation<Operation>(
      "delivery-cancel",
      `/api/delivery-operations/${encodeURIComponent(id)}/cancel`,
      "POST",
      {},
    ),
  async submit(project: string, intent: Intent): Promise<Receipt> {
    if (active.has(project)) throw new Error("交付请求正在接收，请等待结果。");
    const pending = deliveryState(project).pending;
    if (pending && JSON.stringify(pending) !== JSON.stringify(intent))
      throw new Error("接收结果尚未确认，请重试原交付请求。");
    sessionStorage.setItem(prefix + project, JSON.stringify(intent));
    active.add(project);
    changed();
    const path =
      intent.action === "prepare"
        ? `/api/projects/${encodeURIComponent(project)}/deliveries`
        : `/api/deliveries/${encodeURIComponent(intent.delivery)}/${intent.action}`;
    try {
      const receipt = await durableMutation<Receipt>(
        "delivery",
        path,
        "POST",
        intent.body,
      );
      receipts.set(project, receipt);
      sessionStorage.removeItem(prefix + project);
      return receipt;
    } catch (e) {
      if (
        e instanceof ApiError &&
        ([403, 404, 422].includes(e.status) || e.mutationOutcome === "rejected")
      )
        sessionStorage.removeItem(prefix + project);
      throw e;
    } finally {
      active.delete(project);
      changed();
    }
  },
};
