// Required onboarding step: register the user's email address. Every install
// asks for one, not skippable (operator decision) — `emailBlocksContinue`
// is the pure client-side gate `Onboarding.jsx` folds into `continueBlocked`
// beside `projectsBlockContinue`, and `submitEmail` is the network call
// `advance()` wraps in the wizard's existing `guard()`.
//
// `isWellFormedEmail` is a *basic* well-formedness check, not an RFC 5322
// validator: non-empty, no whitespace, exactly one "@", both sides non-empty,
// and a domain that itself looks like a domain (contains a dot, does not
// start or end with one). `src/no_human/api/app.py`'s `_well_formed_email`
// mirrors this exact rule server-side, because the client gate is
// bypassable and the server must not trust it.

export const EMAIL_MAX_LEN = 254;
export const EMAIL_MAX_LOCAL_LEN = 64;

export const EMAIL_REJECT_MESSAGE = "Please enter a valid email address";

export function isWellFormedEmail(value) {
  const s = String(value || "").trim();
  if (!s || /\s/.test(s)) return false;
  // Mirrors the server's bounds exactly (RFC 5321: 254 total, 64 local). The
  // server is authoritative, but matching here means a user is told at the
  // input instead of by a 422 after Continue.
  if (s.length > EMAIL_MAX_LEN) return false;
  // eslint-disable-next-line no-control-regex
  if (/[\u0000-\u001f\u007f\u202a-\u202e\u2066-\u2069]/.test(s)) return false;
  const parts = s.split("@");
  if (parts.length !== 2) return false;
  const [local, domain] = parts;
  if (local.length > EMAIL_MAX_LOCAL_LEN) return false;
  return Boolean(
    local && domain && domain.includes(".") && !domain.startsWith(".") && !domain.endsWith("."),
  );
}

// Same `null | string` contract as `projectsBlockContinue` in
// onboardingProjects.js: null means "Continue is allowed", a string is the
// message to show.
export function emailBlocksContinue(value) {
  return isWellFormedEmail(value) ? null : EMAIL_REJECT_MESSAGE;
}

// Deliberately no local error handling: `Onboarding.jsx`'s `advance()` calls
// this through `guard()`, which is the wizard's ONE failure-handling contract
// (classifies network vs. HTTP failures, never rethrows to block `next()`).
// Swallowing a rejection here would create a second, divergent failure path.
export async function submitEmail(value, { registerOnboardingEmail }) {
  await registerOnboardingEmail(String(value || "").trim());
}
