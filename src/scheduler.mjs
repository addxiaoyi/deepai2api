// Account scheduler.
//
// What a subscription-to-API gateway needs beyond a single token (sub2api does
// this): pick an account per request, prefer the one a conversation already
// used (sticky sessions), skip accounts that are cooling down, and fail over to
// the next account when the upstream refuses.
//
// Failure classes and their cooldowns:
//   auth_* / 401 / 403  -> account is broken, park it for a while
//   429 / 5xx          -> transient, short cool-down
//   session_busy       -> handled inside the lane (retry), never a failover

const STICKY_TTL_MS = 30 * 60 * 1000;
const COOLDOWN_AUTH_MS = 10 * 60 * 1000;
const COOLDOWN_TRANSIENT_MS = 45 * 1000;
const MAX_ATTEMPTS = 3;

export function cooldownFor(error) {
  const status = Number(error?.status ?? 0);
  const code = String(error?.code ?? "");
  if (status === 401 || status === 403 || code.startsWith("auth_") || ["machine_required", "machine_bound", "network_bound", "check_in_refused"].includes(code)) {
    return COOLDOWN_AUTH_MS;
  }
  if (status === 429 || status >= 500 || ["rate_limit_exceeded", "theos_exhausted", "theos_insufficient"].includes(code)) {
    return COOLDOWN_TRANSIENT_MS;
  }
  return 0;
}

export function makeScheduler({ store, now = () => Date.now() }) {
  /** `${keyId}|${model}` -> { accountId, at } */
  const sticky = new Map();

  const pruneSticky = (t) => {
    for (const [key, value] of sticky) {
      if (t - value.at > STICKY_TTL_MS) sticky.delete(key);
    }
  };

  const isUsable = (account, t) => account.healthy !== false && (account.cooldownUntil ?? 0) <= t;

  /**
   * Candidate order for one request: sticky account first, then least-recently
   * used so traffic spreads evenly, and never more than MAX_ATTEMPTS.
   */
  function candidates({ keyId, model } = {}) {
    const t = now();
    pruneSticky(t);
    const usable = store.accounts.filter((account) => isUsable(account, t));
    if (!usable.length) return [];

    const stickyKey = keyId ? `${keyId}|${model ?? ""}` : null;
    const pinned = stickyKey ? sticky.get(stickyKey) : null;
    const ordered = [...usable].sort((a, b) => {
      const aUsed = Date.parse(a.lastUsedAt ?? 0) || 0;
      const bUsed = Date.parse(b.lastUsedAt ?? 0) || 0;
      return aUsed - bUsed;
    });
    if (pinned) {
      const index = ordered.findIndex((account) => account.id === pinned.accountId);
      if (index > 0) ordered.unshift(...ordered.splice(index, 1));
    }
    return ordered.slice(0, MAX_ATTEMPTS);
  }

  /** Remember which account served a conversation so the next turn stays there. */
  function pin({ keyId, model, accountId }) {
    if (!keyId || !accountId) return;
    sticky.set(`${keyId}|${model ?? ""}`, { accountId, at: now() });
    if (sticky.size > 1024) {
      const oldest = [...sticky.entries()].sort((a, b) => a[1].at - b[1].at)[0];
      if (oldest) sticky.delete(oldest[0]);
    }
  }

  function record({ accountId, ok, usage, error }) {
    store.recordAccount(accountId, { ok, usage, error: error?.message ?? error, cooldownMs: ok ? 0 : cooldownFor(error) });
  }

  function status() {
    const t = now();
    return store.accounts.map((account) => ({
      id: account.id,
      label: account.label,
      username: account.username,
      healthy: account.healthy !== false,
      cooling: (account.cooldownUntil ?? 0) > t,
      cooldownUntil: account.cooldownUntil ?? 0,
      lastError: account.lastError ?? null,
      requests: account.requests ?? 0,
    }));
  }

  return { candidates, pin, record, status };
}