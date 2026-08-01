import type { AuthRole } from "./auth-api";
import type { ViewKey } from "./types";

const DEFAULT_VIEW: ViewKey = "overview";
const VIEWER_VIEWS = new Set<ViewKey>([
  "overview",
  "technology",
  "customer",
  "competition",
  "history",
  "dictionary",
]);

export function canAccessView(role: AuthRole, view: ViewKey) {
  if (role === "admin") return true;
  if (role === "viewer") return VIEWER_VIEWS.has(view);
  return view !== "settings";
}

/**
 * Keeps authorization synchronous with the authenticated user. This is used
 * for both navigation requests and rendering, so a stale internal selection
 * can never expose an administrator-only page for a normal user.
 */
export function resolveAuthorizedView(
  role: AuthRole,
  requestedView: ViewKey,
): ViewKey {
  return canAccessView(role, requestedView) ? requestedView : DEFAULT_VIEW;
}
