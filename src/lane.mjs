// Upstream client for the DeepAI Code managed model lane.
//
// Reconstructed from the shipped server bundle (apps/server/dist/binCli-*.mjs):
//   src/deepai/api.ts            -> lane headers, GET /theos check-in, /models catalog
//   src/deepai/localTransport.ts -> loopback->upstream forwarding, 409 session_busy
//   src/deepai/modelTransportPolicy.ts -> watchdog + capacity wait
//
// Lane surface (under `${origin}/deepai_code`):
//   GET  /theos              allowance + check-in nonce
//   GET  /models             model catalog (per-request prices in Theos)
//   POST /chat/completions   OpenAI-compatible, SSE when `stream: true`
//   POST /responses          OpenAI Responses-compatible
//   POST /web_search         { query, num_results }
//   POST /find_service       { query, context? }
//   GET  /get_usage_summary
//
// The lane allows ONE metered turn at a time per account, and every account has
// its own check-in nonce chain — so both the slot and the nonce live per account.
import { randomBytes } from "node:crypto";
import { fetchRetry } from "./dispatcher.mjs";
import { makeSemaphore } from "./semaphore.mjs";

export const LANE_PREFIX = "/deepai_code";

export class LaneError extends Error {
  constructor(message, { status = 0, body = null, code = "lane_error", accountId = null } = {}) {
    super(message);
    this.name = "LaneError";
    this.status = status;
    this.body = body;
    this.code = code;
    this.accountId = accountId;
  }
}

export function makeLane({ config, getAccount, machineHeader, dispatcher, log = () => {} }) {
  const origin = config.apiOrigin.replace(/\/+$/, "");
  const base = `${origin}${LANE_PREFIX}`;
  const instance = randomBytes(16).toString("hex");

  /** accountId -> { checkIn, checkedAt, pending, slot } */
  const perAccount = new Map();

  let catalog = null;
  let catalogAt = 0;
  let catalogPending = null;

  const slotFor = (id) => {
    let entry = perAccount.get(id);
    if (!entry) {
      entry = { checkIn: null, checkedAt: 0, pending: null, slot: makeSemaphore(config.maxConcurrentMetered) };
      perAccount.set(id, entry);
    }
    return entry;
  };

  const resolveAccount = (account) => {
    const chosen = account ?? getAccount?.() ?? null;
    if (!chosen?.apiKey) {
      throw new LaneError("No DeepAI account is linked to the gateway yet.", { status: 401, code: "auth_required" });
    }
    return chosen;
  };

  const laneHeaders = async (account) => {
    const chosen = resolveAccount(account);
    const machine = await machineHeader();
    return {
      id: chosen.id,
      headers: {
        "Content-Type": "application/json",
        "X-DeepAI-Code-Theos": String(config.laneVersion),
        "X-DeepAI-Code-Instance": instance,
        ...(machine ? { "X-DeepAI-Code-Machine": machine } : {}),
        "api-key": chosen.apiKey,
      },
    };
  };

  const beacon = (piActive = true) =>
    [`v=${config.appVersion}`, `up=${Math.floor(process.uptime())}`, `pi=${piActive ? 1 : 0}`, "focus=1"].join(";");

  const noteCheckIn = (accountId, payload) => {
    const value = payload?.check_in;
    if (value && typeof value.nonce === "string" && /^[0-9a-f]{32}$/.test(value.nonce) && typeof value.seconds === "number" && value.seconds > 0) {
      const entry = slotFor(accountId);
      entry.checkIn = { nonce: value.nonce, seconds: value.seconds };
      entry.checkedAt = Date.now();
    }
  };

  async function checkInNow(account, signal) {
    const { id, headers } = await laneHeaders(account);
    const entry = slotFor(id);
    const requestHeaders = { ...headers, "X-DeepAI-Code-Beacon": beacon() };
    if (entry.checkIn) requestHeaders["X-DeepAI-Code-Check-In"] = entry.checkIn.nonce;

    const res = await fetchRetry(`${base}/theos`, { method: "GET", headers: requestHeaders, redirect: "error", signal }, dispatcher);
    const payload = await res.json().catch(() => null);
    noteCheckIn(id, payload);
    if (!res.ok) {
      throw new LaneError("DeepAI check-in was refused.", { status: res.status, body: payload, code: "check_in_refused", accountId: id });
    }
    return payload;
  }

  /** Refresh at half the nonce lifetime, exactly like the app's ensureCheckedIn. */
  async function ensureCheckedIn(account, signal) {
    const { id } = await laneHeaders(account);
    const entry = slotFor(id);
    const fresh = entry.checkIn && Date.now() - entry.checkedAt < (entry.checkIn.seconds * 1000) / 2;
    if (fresh) return;
    entry.pending ??= checkInNow(account, signal).finally(() => {
      entry.pending = null;
    });
    await entry.pending;
  }

  async function getAllowance(account, signal) {
    const { id, headers } = await laneHeaders(account);
    const entry = slotFor(id);
    const requestHeaders = { ...headers, "X-DeepAI-Code-Beacon": beacon() };
    if (entry.checkIn) requestHeaders["X-DeepAI-Code-Check-In"] = entry.checkIn.nonce;
    const res = await fetchRetry(`${base}/theos`, { method: "GET", headers: requestHeaders, redirect: "error", signal }, dispatcher);
    const body = await res.json().catch(() => null);
    noteCheckIn(id, body);
    return body;
  }

  async function getCatalog({ force = false } = {}) {
    if (!force && catalog && Date.now() - catalogAt < config.catalogTtlMs) return catalog;
    if (!force && catalogPending) return catalogPending;
    const run = async () => {
      const { headers } = await laneHeaders();
      const res = await fetchRetry(`${base}/models`, { method: "GET", headers, redirect: "error" }, dispatcher);
      const body = await res.json().catch(() => null);
      if (!res.ok || !Array.isArray(body?.models)) {
        throw new LaneError("DeepAI model catalog is unavailable.", { status: res.status, body, code: "catalog_unavailable" });
      }
      catalog = body.models.filter((m) => m && typeof m.id === "string" && m.id !== "gpt-oss-120b");
      catalogAt = Date.now();
      return catalog;
    };
    catalogPending = run().finally(() => {
      catalogPending = null;
    });
    return catalogPending;
  }

  /** Per-request price in Theos; unknown models are treated as metered. */
  async function priceOf(model) {
    try {
      const list = await getCatalog();
      const entry = list.find((m) => m.id === model);
      if (!entry) return 1;
      const raw = entry.theosPerRequest;
      const value = typeof raw === "number" ? raw : Number.parseFloat(String(raw ?? "1"));
      return Number.isFinite(value) ? value : 1;
    } catch {
      return 1;
    }
  }

  const sleep = (ms, signal) =>
    new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        signal?.removeEventListener("abort", onAbort);
        resolve();
      }, ms);
      const onAbort = () => {
        clearTimeout(timer);
        reject(signal?.reason ?? new Error("aborted"));
      };
      signal?.addEventListener("abort", onAbort, { once: true });
    });

  const isSessionBusy = (body) => body && typeof body === "object" && body.error?.code === "session_busy";

  /**
   * Forward one model call against a specific account. The body is passed
   * through untouched so tools, reasoning content and provider-specific fields
   * keep their meaning.
   */
  async function call(path, bodyBuffer, { account, model, signal, affinity, extraHeaders = {}, retries = 4 } = {}) {
    const chosen = resolveAccount(account);
    const entry = slotFor(chosen.id);
    const metered = model === undefined ? true : (await priceOf(model)) > 0;

    let releaseSlot = () => {};
    if (metered) releaseSlot = await entry.slot.acquire(signal);

    try {
      for (let attempt = 0; ; attempt++) {
        await ensureCheckedIn(chosen, signal);
        const { headers } = await laneHeaders(chosen);
        const requestHeaders = { ...headers, ...extraHeaders };
        if (entry.checkIn) requestHeaders["X-DeepAI-Code-Check-In"] = entry.checkIn.nonce;
        if (affinity && /^[A-Za-z0-9._:-]{1,256}$/.test(affinity)) requestHeaders["X-Session-Affinity"] = affinity;

        const res = await fetchRetry(
          `${base}${path}`,
          { method: "POST", headers: requestHeaders, body: bodyBuffer, redirect: "error", signal },
          dispatcher,
        );

        if (res.status !== 409) {
          if (res.status === 200 || res.status === 202 || res.status === 204) return res;
          const body = await res.json().catch(() => null);
          const code = body?.error?.code ?? `http_${res.status}`;
          if (body) noteCheckIn(chosen.id, body);
          throw new LaneError(`DeepAI lane rejected the request (${res.status}${code ? ` ${code}` : ""}).`, {
            status: res.status,
            body,
            code,
            accountId: chosen.id,
          });
        }

        // 409 capacity refusal: only `session_busy` is worth retrying.
        const body = await res.clone().json().catch(() => null);
        if (!isSessionBusy(body)) {
          const code = body?.error?.code ?? "http_409";
          await res.body?.cancel().catch(() => {});
          throw new LaneError(`DeepAI lane rejected the request (409 ${code}).`, { status: 409, body, code, accountId: chosen.id });
        }
        await res.body?.cancel().catch(() => {});
        if (attempt >= retries) {
          throw new LaneError("DeepAI lane is busy with another turn.", { status: 409, body, code: "session_busy", accountId: chosen.id });
        }
        const retryAfter = Number(res.headers.get("retry-after") ?? 2);
        const delay = Number.isFinite(retryAfter) ? Math.min(30, Math.max(1, retryAfter)) : 2;
        log("warn", `lane busy on ${chosen.label ?? chosen.id}, retrying in ${delay}s (attempt ${attempt + 1}/${retries})`);
        await sleep(delay * 1000, signal);
      }
    } finally {
      releaseSlot();
    }
  }

  async function json(path, body, { account, signal, extraHeaders } = {}) {
    const { id, headers } = await laneHeaders(account);
    const entry = slotFor(id);
    await ensureCheckedIn(account, signal);
    const requestHeaders = { ...headers, ...extraHeaders };
    if (entry.checkIn) requestHeaders["X-DeepAI-Code-Check-In"] = entry.checkIn.nonce;
    const res = await fetchRetry(
      `${base}${path}`,
      {
        method: body === undefined ? "GET" : "POST",
        headers: requestHeaders,
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
        redirect: "error",
        signal,
      },
      dispatcher,
    );
    const payload = await res.json().catch(() => null);
    if (!res.ok) {
      const code = payload?.error?.code ?? `http_${res.status}`;
      if (body) noteCheckIn(id, payload);
      throw new LaneError(`DeepAI lane rejected ${path} (${res.status}).`, { status: res.status, body: payload, code, accountId: id });
    }
    return payload;
  }

  return {
    origin,
    base,
    get instance() {
      return instance;
    },
    /** Raw transport, used by the OAuth exchange which targets a different prefix. */
    fetch: (url, init) => fetchRetry(url, init, dispatcher),
    ensureCheckedIn,
    getAllowance,
    getCatalog,
    resetCatalog() {
      catalog = null;
      catalogAt = 0;
      catalogPending = null;
      perAccount.clear();
    },
    priceOf,
    call,
    json,
    /** Aggregate slot view for /v1/stats. */
    get slot() {
      let available = 0;
      let waiting = 0;
      for (const entry of perAccount.values()) {
        available += entry.slot.available;
        waiting += entry.slot.waiting;
      }
      return { available, waiting, accounts: perAccount.size };
    },
    checkInFor(accountId) {
      return slotFor(accountId).checkIn;
    },
    get checkIn() {
      const first = perAccount.values().next();
      return first.done ? null : (first.value.checkIn ?? null);
    },
  };
}