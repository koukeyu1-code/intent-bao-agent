export type AuthRole = "admin" | "user" | "viewer";

export interface AuthUser {
  id: string;
  username: string;
  displayName: string;
  role: AuthRole;
}

export interface LoginCredentials {
  username: string;
  password: string;
}

export interface LoginResult {
  user: AuthUser;
  expiresAt: string;
}

export interface AuthRequestOptions {
  signal?: AbortSignal;
}

interface AuthUserApiResponse {
  id: string;
  username: string;
  display_name: string;
  role: AuthRole;
}

interface LoginApiResponse {
  user: AuthUserApiResponse;
  expires_at: string;
  access_token: string;
  token_type: "bearer";
}

interface ApiErrorBody {
  detail?: string | Array<{ msg?: string }>;
}

const FALLBACK_API_BASE = "http://127.0.0.1:8000/api";
const ACCESS_TOKEN_STORAGE_KEY = "intent-bao.access-token";

export class AuthApiError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "AuthApiError";
    this.status = status;
  }
}

export const resolveAuthApiBase = (): string => {
  const configured = process.env.NEXT_PUBLIC_INTENT_BAO_API_BASE?.trim();
  if (configured) {
    return configured.replace(/\/$/, "");
  }
  return FALLBACK_API_BASE;
};

export const getAccessToken = (): string | null => {
  if (typeof window === "undefined") {
    return null;
  }
  try {
    return window.sessionStorage.getItem(ACCESS_TOKEN_STORAGE_KEY);
  } catch {
    return null;
  }
};

export const storeAccessToken = (token: string): void => {
  if (typeof window === "undefined") {
    return;
  }
  window.sessionStorage.setItem(ACCESS_TOKEN_STORAGE_KEY, token);
};

export const clearAccessToken = (): void => {
  if (typeof window === "undefined") {
    return;
  }
  try {
    window.sessionStorage.removeItem(ACCESS_TOKEN_STORAGE_KEY);
  } catch {
    // A blocked storage backend is equivalent to an absent local session.
  }
};

export const bearerHeaders = (): Record<string, string> => {
  const token = getAccessToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
};

const normalizeUser = (user: AuthUserApiResponse): AuthUser => ({
  id: user.id,
  username: user.username,
  displayName: user.display_name,
  role: user.role,
});

const errorMessage = (body: ApiErrorBody | null, status: number): string => {
  if (typeof body?.detail === "string" && body.detail.trim()) {
    return body.detail;
  }
  if (Array.isArray(body?.detail)) {
    const messages = body.detail
      .map((item) => item.msg?.trim())
      .filter((item): item is string => Boolean(item));
    if (messages.length > 0) {
      return messages.join("；");
    }
  }
  return `认证服务请求失败（${status}）`;
};

const authRequest = async <T>(
  path: string,
  init: RequestInit = {},
  authenticated = true,
): Promise<T> => {
  const response = await fetch(`${resolveAuthApiBase()}${path}`, {
    ...init,
    cache: "no-store",
    credentials: "omit",
    headers: {
      ...(init.body ? { "Content-Type": "application/json" } : {}),
      ...(authenticated ? bearerHeaders() : {}),
      ...(init.headers ?? {}),
    },
  });

  if (!response.ok) {
    if (response.status === 401) {
      clearAccessToken();
    }
    const body = (await response.json().catch(() => null)) as ApiErrorBody | null;
    throw new AuthApiError(errorMessage(body, response.status), response.status);
  }

  return (await response.json()) as T;
};

export const getCurrentUser = async (
  options: AuthRequestOptions = {},
): Promise<AuthUser | null> => {
  if (!getAccessToken()) {
    return null;
  }
  try {
    const user = await authRequest<AuthUserApiResponse>("/auth/me", {
      method: "GET",
      signal: options.signal,
    });
    return normalizeUser(user);
  } catch (error) {
    if (error instanceof AuthApiError && error.status === 401) {
      return null;
    }
    throw error;
  }
};

export const login = async (
  credentials: LoginCredentials,
  options: AuthRequestOptions = {},
): Promise<LoginResult> => {
  const response = await authRequest<LoginApiResponse>("/auth/login", {
    method: "POST",
    signal: options.signal,
    body: JSON.stringify({
      username: credentials.username.trim(),
      password: credentials.password,
    }),
  }, false);
  storeAccessToken(response.access_token);
  return {
    user: normalizeUser(response.user),
    expiresAt: response.expires_at,
  };
};

export const logout = async (
  options: AuthRequestOptions = {},
): Promise<void> => {
  try {
    await authRequest<{ message: string }>("/auth/logout", {
      method: "POST",
      signal: options.signal,
    });
  } finally {
    clearAccessToken();
  }
};
