import { ApiError, request } from "./client";

// Keep only request fingerprints and identities. Uncertain retries retain their
// identity across refreshes without persisting paths, credentials or payloads.
export async function durableMutation<T>(
  namespace: string,
  path: string,
  method: string,
  body: unknown,
): Promise<T> {
  const encoded = JSON.stringify(body);
  const digest = await crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(`${method}:${path}:${encoded}`),
  );
  const fingerprint = Array.from(new Uint8Array(digest), (b) =>
    b.toString(16).padStart(2, "0"),
  ).join("");
  const storage = `eventide.${namespace}-request.${fingerprint}`;
  const key = sessionStorage.getItem(storage) || crypto.randomUUID();
  sessionStorage.setItem(storage, key);
  try {
    const result = await request<T>(path, {
      method,
      body: encoded,
      headers: { "Idempotency-Key": key },
    });
    sessionStorage.removeItem(storage);
    return result;
  } catch (error) {
    if (
      error instanceof ApiError &&
      ([403, 404, 422].includes(error.status) ||
        error.mutationOutcome === "rejected")
    )
      sessionStorage.removeItem(storage);
    // Unmarked 409 responses may mean a write is still unresolved. Only an
    // explicit server guarantee of no effect permits a fresh intent identity.
    throw error;
  }
}
