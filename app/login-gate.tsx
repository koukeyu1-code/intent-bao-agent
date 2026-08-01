"use client";

import {
  createContext,
  type CSSProperties,
  type FormEvent,
  type ReactNode,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";

import {
  AuthApiError,
  type AuthUser,
  getCurrentUser,
  login,
  logout,
} from "./auth-api";

export interface AuthContextValue {
  user: AuthUser;
  logout: () => Promise<void>;
  refresh: () => Promise<void>;
}

export interface LoginGateProps {
  children: ReactNode | ((auth: AuthContextValue) => ReactNode);
}

type AuthPhase = "checking" | "unauthenticated" | "authenticated";

const AuthContext = createContext<AuthContextValue | null>(null);

export const useAuth = (): AuthContextValue => {
  const value = useContext(AuthContext);
  if (!value) {
    throw new Error("useAuth必须在LoginGate内部使用");
  }
  return value;
};

const styles: Record<string, CSSProperties> = {
  viewport: {
    alignItems: "center",
    background:
      "radial-gradient(circle at 15% 15%, #eaf3ff 0, transparent 38%), #f5f7fb",
    color: "#17243d",
    display: "flex",
    justifyContent: "center",
    minHeight: "100vh",
    padding: "24px",
  },
  card: {
    background: "rgba(255, 255, 255, 0.96)",
    border: "1px solid #dbe4f0",
    borderRadius: "18px",
    boxShadow: "0 18px 55px rgba(27, 55, 96, 0.13)",
    maxWidth: "420px",
    padding: "32px",
    width: "100%",
  },
  heading: {
    fontSize: "28px",
    lineHeight: 1.25,
    margin: "0 0 8px",
  },
  description: {
    color: "#68758a",
    lineHeight: 1.65,
    margin: "0 0 24px",
  },
  field: {
    display: "grid",
    gap: "8px",
    marginBottom: "16px",
  },
  label: {
    fontSize: "14px",
    fontWeight: 650,
  },
  input: {
    background: "#fff",
    border: "1px solid #c9d4e3",
    borderRadius: "10px",
    boxSizing: "border-box",
    color: "#17243d",
    font: "inherit",
    minHeight: "44px",
    outlineColor: "#2f66ad",
    padding: "0 12px",
    width: "100%",
  },
  submit: {
    background: "#2f66ad",
    border: 0,
    borderRadius: "10px",
    color: "#fff",
    cursor: "pointer",
    font: "inherit",
    fontWeight: 700,
    minHeight: "46px",
    padding: "0 18px",
    width: "100%",
  },
  hint: {
    color: "#78869a",
    fontSize: "13px",
    lineHeight: 1.6,
    margin: "16px 0 0",
    textAlign: "center",
  },
  error: {
    background: "#fff1f0",
    border: "1px solid #ffccc7",
    borderRadius: "9px",
    color: "#a61d24",
    fontSize: "14px",
    lineHeight: 1.5,
    margin: "0 0 16px",
    padding: "10px 12px",
  },
  loading: {
    color: "#53647c",
    fontSize: "15px",
    lineHeight: 1.6,
    margin: 0,
    textAlign: "center",
  },
  retry: {
    background: "transparent",
    border: 0,
    color: "#2f66ad",
    cursor: "pointer",
    font: "inherit",
    marginTop: "8px",
    padding: "6px",
    textDecoration: "underline",
  },
};

const messageFromError = (error: unknown): string => {
  if (error instanceof AuthApiError) {
    return error.message;
  }
  if (error instanceof DOMException && error.name === "AbortError") {
    return "";
  }
  return "暂时无法连接认证服务，请确认后端已启动后重试。";
};

export default function LoginGate({ children }: LoginGateProps) {
  const [phase, setPhase] = useState<AuthPhase>("checking");
  const [user, setUser] = useState<AuthUser | null>(null);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [errorMessage, setErrorMessage] = useState("");
  const [isSubmitting, setIsSubmitting] = useState(false);

  const checkSession = useCallback(async (signal?: AbortSignal) => {
    try {
      const currentUser = await getCurrentUser({ signal });
      if (signal?.aborted) {
        return;
      }
      setUser(currentUser);
      setPhase(currentUser ? "authenticated" : "unauthenticated");
    } catch (error) {
      if (signal?.aborted) {
        return;
      }
      setUser(null);
      setErrorMessage(messageFromError(error));
      setPhase("unauthenticated");
    }
  }, []);

  const refresh = useCallback(async () => {
    setPhase("checking");
    setErrorMessage("");
    await checkSession();
  }, [checkSession]);

  useEffect(() => {
    const controller = new AbortController();
    void getCurrentUser({ signal: controller.signal })
      .then((currentUser) => {
        if (controller.signal.aborted) {
          return;
        }
        setUser(currentUser);
        setPhase(currentUser ? "authenticated" : "unauthenticated");
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) {
          return;
        }
        setUser(null);
        setErrorMessage(messageFromError(error));
        setPhase("unauthenticated");
      });
    return () => controller.abort();
  }, []);

  useEffect(() => {
    const handleExpiredSession = () => {
      setUser(null);
      setPassword("");
      setErrorMessage("登录状态已失效，请重新登录。");
      setPhase("unauthenticated");
    };
    window.addEventListener("intent-bao:auth-expired", handleExpiredSession);
    return () =>
      window.removeEventListener(
        "intent-bao:auth-expired",
        handleExpiredSession,
      );
  }, []);

  const handleLogin = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (isSubmitting) {
      return;
    }
    setIsSubmitting(true);
    setErrorMessage("");
    try {
      const result = await login({ username, password });
      setUser(result.user);
      setPassword("");
      setPhase("authenticated");
    } catch (error) {
      setErrorMessage(messageFromError(error));
    } finally {
      setIsSubmitting(false);
    }
  };

  const handleLogout = useCallback(async () => {
    try {
      await logout();
    } catch (error) {
      if (!(error instanceof AuthApiError && error.status === 401)) {
        throw error;
      }
    }
    setUser(null);
    setPassword("");
    setPhase("unauthenticated");
  }, []);

  const auth = useMemo<AuthContextValue | null>(
    () =>
      user
        ? {
            user,
            logout: handleLogout,
            refresh: () => refresh(),
          }
        : null,
    [handleLogout, refresh, user],
  );

  if (phase === "checking") {
    return (
      <main style={styles.viewport}>
        <section aria-live="polite" style={styles.card}>
          <p style={styles.loading} role="status">
            正在检查登录状态…
          </p>
        </section>
      </main>
    );
  }

  if (phase === "unauthenticated" || !auth) {
    return (
      <main style={styles.viewport}>
        <section aria-labelledby="login-heading" style={styles.card}>
          <h1 id="login-heading" style={styles.heading}>
            登录意向宝
          </h1>
          <p style={styles.description}>
            使用本地账户进入客户意向智能体。
          </p>

          <form onSubmit={handleLogin}>
            {errorMessage ? (
              <div aria-live="assertive" role="alert" style={styles.error}>
                {errorMessage}
              </div>
            ) : null}

            <div style={styles.field}>
              <label htmlFor="intent-bao-username" style={styles.label}>
                用户名
              </label>
              <input
                autoComplete="username"
                disabled={isSubmitting}
                id="intent-bao-username"
                maxLength={100}
                name="username"
                onChange={(event) => setUsername(event.target.value)}
                required
                style={styles.input}
                type="text"
                value={username}
              />
            </div>

            <div style={styles.field}>
              <label htmlFor="intent-bao-password" style={styles.label}>
                密码
              </label>
              <input
                autoComplete="current-password"
                disabled={isSubmitting}
                id="intent-bao-password"
                maxLength={1024}
                name="password"
                onChange={(event) => setPassword(event.target.value)}
                required
                style={styles.input}
                type="password"
                value={password}
              />
            </div>

            <button
              aria-busy={isSubmitting}
              disabled={isSubmitting}
              style={{
                ...styles.submit,
                ...(isSubmitting ? { cursor: "wait", opacity: 0.72 } : {}),
              }}
              type="submit"
            >
              {isSubmitting ? "正在登录…" : "登录"}
            </button>
          </form>

          <p style={styles.hint}>测试账号：admin（管理员）/ planner（普通用户）</p>
          {errorMessage ? (
            <div style={{ textAlign: "center" }}>
              <button
                onClick={() => void refresh()}
                style={styles.retry}
                type="button"
              >
                重新检查服务
              </button>
            </div>
          ) : null}
        </section>
      </main>
    );
  }

  return (
    <AuthContext.Provider value={auth}>
      {typeof children === "function" ? children(auth) : children}
    </AuthContext.Provider>
  );
}
