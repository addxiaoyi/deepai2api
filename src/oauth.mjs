// OAuth sign-in against DeepAI, reconstructed from the shipped app
// (startDeepAISignIn / exchangeDeepAICode / deepAISignInUrl in the server bundle).
//
// RFC 8252 loopback flow with PKCE:
//   1. verifier = 32 random bytes (base64url), challenge = sha256(verifier) (base64url)
//   2. listen on 127.0.0.1 and open
//        https://deepai.org/deepai_code/desktop_auth/
//          ?state=<32 random bytes hex>&port=<loopback port>
//          &code_challenge=<challenge>&code_challenge_method=S256
//   3. the page redirects to http://127.0.0.1:<port>/auth-callback?code=&state=
//   4. POST {origin}/deepai_code/desktop_auth/exchange/v2/ {code, code_verifier}
//      (404 -> retry without the /v2/ segment, which is what the app does)
//   5. response: { api_key, username, is_pro, connect_token?, connect_expires_at? }
import { createHash, randomBytes, timingSafeEqual } from "node:crypto";

export const CALLBACK_PATH = "/auth-callback";
export const EXCHANGE_PATH = "/deepai_code/desktop_auth/exchange/";

const sameSecret = (a, b) => {
  const x = Buffer.from(String(a ?? ""));
  const y = Buffer.from(String(b ?? ""));
  return x.length === y.length && x.length > 0 && timingSafeEqual(x, y);
};

export class SignInError extends Error {
  constructor(message, options) {
    super(message, options);
    this.name = "SignInError";
  }
}

/** Fresh PKCE material for one sign-in attempt. */
export function beginSignIn() {
  const verifier = randomBytes(32).toString("base64url");
  return {
    state: randomBytes(32).toString("hex"),
    verifier,
    challenge: createHash("sha256").update(verifier).digest("base64url"),
    startedAt: Date.now(),
  };
}

/** The page the operator's browser must visit. `port` is the loopback port. */
export function signInUrl({ state, challenge, port, origin = "https://deepai.org" }) {
  const url = new URL("/deepai_code/desktop_auth/", origin);
  url.search = new URLSearchParams({ state, port: String(port) }).toString();
  url.searchParams.set("code_challenge", challenge);
  url.searchParams.set("code_challenge_method", "S256");
  return url.href;
}

/**
 * Validate a callback and trade the code for account credentials.
 * Returns the shape the rest of the gateway uses: { apiKey, username, isPro, connect }.
 */
export async function exchangeCode({ code, verifier, origin, fetchImpl = fetch, appVersion = "0.3.50", signal }) {
  if (!code || typeof code !== "string" || code.length > 4096) throw new SignInError("Sign-in callback carried no usable code.");
  if (!verifier) throw new SignInError("Sign-in lost its PKCE verifier; start sign-in again.");

  const base = `${origin.replace(/\/+$/, "")}${EXCHANGE_PATH}`;
  let path = "v2/";

  for (let attempt = 0; attempt < 3; attempt++) {
    const response = await fetchImpl(`${base}${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code, code_verifier: verifier }),
      redirect: "error",
      signal,
    });

    // An older deployment answers only the unversioned route.
    if (response.status === 404 && path === "v2/") {
      await response.body?.cancel().catch(() => {});
      path = "";
      continue;
    }
    if ([409, 500, 502, 503, 504].includes(response.status) && attempt < 2) {
      await response.body?.cancel().catch(() => {});
      await new Promise((resolve) => setTimeout(resolve, 400 * (attempt + 1)));
      continue;
    }

    if (!response.ok) {
      const detail = await response.json().catch(() => null);
      throw new SignInError(describeRefusal(response.status, detail));
    }

    const payload = await response.json().catch(() => {
      throw new SignInError("DeepAI returned an unreadable sign-in response.");
    });
    if (!payload || typeof payload.api_key !== "string" || !payload.api_key || /[\r\n]/.test(payload.api_key)) {
      throw new SignInError("DeepAI returned an invalid api key.");
    }
    return {
      apiKey: payload.api_key,
      username: typeof payload.username === "string" ? payload.username : "",
      isPro: payload.is_pro === true,
      connect:
        typeof payload.connect_token === "string" && typeof payload.connect_expires_at === "string"
          ? { token: payload.connect_token, expiresAt: payload.connect_expires_at }
          : null,
    };
  }
  throw new SignInError("DeepAI sign-in exchange did not complete.");
}

function describeRefusal(status, detail) {
  const code = detail?.error?.code ?? detail?.code ?? "";
  if (status === 400 || status === 401) return "This sign-in link expired or was already used. Start sign-in again.";
  if (status === 429) return "Too many sign-in attempts. Wait a moment, then try again.";
  if (code === "email_unverified") return "Verify your DeepAI email address, then sign in again.";
  if (code === "social_login_required") return "This account must sign in with its social provider first.";
  return `DeepAI refused the sign-in (HTTP ${status}${code ? ` ${code}` : ""}).`;
}

/** Check the callback's `state` against the pending attempt. */
export function stateMatches(expected, received) {
  return sameSecret(expected, received);
}