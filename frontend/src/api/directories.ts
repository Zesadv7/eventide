import { request } from "./client";

export type DirectoryChoice =
  | { status: "selected"; path: string }
  | { status: "cancelled" | "unavailable" | "busy" | "timed_out" };

export function chooseDirectory(signal: AbortSignal): Promise<DirectoryChoice> {
  // Interactive selection has a server-side 120 second deadline; no 30s API default.
  return request<DirectoryChoice>("/api/directories/choose", {
    method: "POST",
    signal,
  });
}
