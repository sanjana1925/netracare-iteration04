// Thin wrapper around the FastAPI backend. It only carries data and error messages:
// every rule is decided by the backend, the screens just show what the backend says.
const BASE = "/api";
let token = sessionStorage.getItem("nc_token") || "";

export const setToken = (t) => {
  token = t || "";
  t ? sessionStorage.setItem("nc_token", t) : sessionStorage.removeItem("nc_token");
};
export const hasToken = () => !!token;

export class ApiError extends Error {
  constructor(message, code, toStaff, status, emergency = null) {
    super(message);
    this.code = code; this.toStaff = toStaff; this.status = status;
    this.emergency = emergency; // set when the reason was a red flag, even if the booking itself was refused
  }
}

export async function api(path, { method = "GET", body } = {}) {
  let res;
  try {
    res = await fetch(BASE + path, {
      method,
      headers: { "Content-Type": "application/json", ...(token ? { Authorization: `Bearer ${token}` } : {}) },
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch {
    throw new ApiError("Cannot reach the clinic server. Is the backend running?", "NETWORK", false, 0);
  }
  const data = await res.json().catch(() => null);
  if (!res.ok) {
    const d = data?.detail;
    if (d && typeof d === "object" && !Array.isArray(d)) throw new ApiError(d.message, d.code, d.to_staff, res.status, d.emergency || null);
    if (Array.isArray(d)) throw new ApiError("Please check the details you entered.", "VALIDATION", false, res.status);
    throw new ApiError("Something went wrong.", "UNKNOWN", false, res.status);
  }
  return data;
}

/* Server-sent events (the streamed doctor summary). `onEvent` is called once per event as it arrives.
   Access is checked by the backend before the stream starts, so a refusal comes back as a normal error. */
export async function streamEvents(path, onEvent, signal, options = {}) {
  let res;
  try {
    const headers = token ? { Authorization: `Bearer ${token}` } : {};
    if (options.body) headers["Content-Type"] = "application/json";   // a POST that streams its answer (the chat)
    res = await fetch(BASE + path, { method: options.method || "GET", headers, body: options.body ? JSON.stringify(options.body) : undefined, signal });
  } catch (e) {
    if (e.name === "AbortError") return;
    throw new ApiError("Cannot reach the clinic server. Is the backend running?", "NETWORK", false, 0);
  }
  if (!res.ok) {
    const d = (await res.json().catch(() => null))?.detail;
    throw new ApiError(d && typeof d === "object" && !Array.isArray(d) ? d.message : "Something went wrong.", d?.code || "UNKNOWN", !!d?.to_staff, res.status);
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let cut;
      while ((cut = buffer.indexOf("\n\n")) >= 0) {
        const line = buffer.slice(0, cut).trim();
        buffer = buffer.slice(cut + 2);
        if (line.startsWith("data:")) onEvent(JSON.parse(line.slice(5).trim()));
      }
    }
  } catch (e) {
    if (e.name !== "AbortError") throw e;
  }
}
