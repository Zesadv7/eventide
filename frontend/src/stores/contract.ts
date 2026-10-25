import { computed, ref } from "vue";
import { defineStore } from "pinia";
import { checkRuntimeContract, type ContractCheck } from "../api/contract";

export type ContractGateStatus =
  "checking" | "compatible" | "incompatible" | "unavailable";

/**
 * Startup compatibility gate: the shell stays blocked until the backend
 * contract matches the version this build supports, or an explicit availability
 * failure is shown with a retry. Retries never auto-bury a version mismatch.
 */
export const useContractGate = defineStore("contract-gate", () => {
  const status = ref<ContractGateStatus>("checking");
  const result = ref<ContractCheck | null>(null);
  let inflight: Promise<void> | null = null;

  async function runCheck(): Promise<void> {
    if (inflight) return inflight;
    status.value = "checking";
    inflight = (async () => {
      try {
        const check = await checkRuntimeContract();
        result.value = check;
        status.value = check.kind === "compatible" ? "compatible" : check.kind;
      } finally {
        inflight = null;
      }
    })();
    return inflight;
  }

  const compatible = computed(() => status.value === "compatible");
  const blocking = computed(() => status.value !== "compatible");
  return { status, result, runCheck, compatible, blocking };
});
