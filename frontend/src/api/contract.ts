/**
 * MK-00 runtime contract identity.
 *
 * The workbench boots against exactly one backend contract version. Network
 * faults and server errors are reported as availability problems — never as a
 * version mismatch — while an unreadable or different contract version asks
 * for an upgrade and blocks writes at the shell level.
 */
export const SUPPORTED_CONTRACT_VERSION = 1;

export type RuntimeContract = {
  contract_version: number;
  schema_version: number;
  runtime_version: string;
  capabilities: string[];
};

export type ContractCheck =
  | { kind: "compatible"; contract: RuntimeContract }
  | {
      kind: "incompatible";
      message: string;
      serverVersion: number | null;
      server?: RuntimeContract;
    }
  | { kind: "unavailable"; message: string };

function incompatible(
  message: string,
  serverVersion: number | null = null,
  server?: RuntimeContract,
): ContractCheck {
  return { kind: "incompatible", message, serverVersion, server };
}

function asContract(body: unknown): RuntimeContract | null {
  if (typeof body !== "object" || body === null) return null;
  const value = body as Record<string, unknown>;
  const version = value.contract_version;
  if (typeof version !== "number" || !Number.isSafeInteger(version))
    return null;
  if (
    typeof value.schema_version !== "number" ||
    !Number.isSafeInteger(value.schema_version) ||
    value.schema_version < 1 ||
    typeof value.runtime_version !== "string" ||
    !value.runtime_version ||
    !Array.isArray(value.capabilities) ||
    !value.capabilities.every((item) => typeof item === "string")
  )
    return null;
  return {
    contract_version: version,
    schema_version: value.schema_version,
    runtime_version: value.runtime_version,
    capabilities: value.capabilities,
  };
}

export async function checkRuntimeContract(
  fetchImpl: typeof fetch = fetch,
): Promise<ContractCheck> {
  let response: Response;
  try {
    response = await fetchImpl("/api/runtime/contract", {
      signal: AbortSignal.timeout(15000),
      headers: { Accept: "application/json" },
    });
  } catch {
    // Transport failure: no HTTP answer at all, so nothing can be said about versions.
    return {
      kind: "unavailable",
      message:
        "无法连接本机服务，无法读取契约版本。请确认服务正在运行后重试；这不是版本不匹配。",
    };
  }
  if (response.status === 404) {
    return incompatible(
      "服务端未提供契约版本接口（HTTP 404），版本过旧或不是 Eventide 服务。请升级服务端 Eventide 后重试。",
    );
  }
  if (!response.ok) {
    return {
      kind: "unavailable",
      message: `服务暂时不可用（HTTP ${response.status}），无法读取契约版本。这不是版本不匹配，可稍后重试。`,
    };
  }
  let body: unknown;
  try {
    body = await response.json();
  } catch {
    return {
      kind: "unavailable",
      message: "服务信息未能完整读取或解析，暂时无法确认兼容性，请重试。",
    };
  }
  const contract = asContract(body);
  if (!contract) {
    return {
      kind: "unavailable",
      message: "服务返回的兼容性信息缺失或格式无效，无法确认版本，请重试。",
    };
  }
  if (contract.contract_version === SUPPORTED_CONTRACT_VERSION) {
    return { kind: "compatible", contract };
  }
  if (contract.contract_version > SUPPORTED_CONTRACT_VERSION) {
    return incompatible(
      `服务端契约版本为 v${contract.contract_version}，当前前端支持 v${SUPPORTED_CONTRACT_VERSION}。请升级前端（刷新或重新部署工作台产物）后重试。`,
      contract.contract_version,
      contract,
    );
  }
  return incompatible(
    `前端要求契约版本 v${SUPPORTED_CONTRACT_VERSION}，服务端仅提供 v${contract.contract_version}。请升级服务端 Eventide 后重试。`,
    contract.contract_version,
    contract,
  );
}
