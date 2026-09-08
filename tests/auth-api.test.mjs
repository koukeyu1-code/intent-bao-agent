import assert from "node:assert/strict";
import test from "node:test";
import * as authApi from "../app/auth-api.ts";
import { componentHarness, loadModule, renderedText } from "./helpers/component-harness.mjs";

import {
  AuthApiError,
  getAccessToken,
  getCurrentUser,
  login,
  logout,
  resolveAuthApiBase,
  storeAccessToken,
} from "../app/auth-api.ts";


const withMockFetch = async (implementation, callback) => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = implementation;
  try {
    await callback();
  } finally {
    globalThis.fetch = originalFetch;
  }
};


const withBrowserSession = async (callback) => {
  const originalWindow = globalThis.window;
  const values = new Map();
  globalThis.window = {
    addEventListener() {},
    removeEventListener() {},
    sessionStorage: {
      getItem: (key) => values.get(key) ?? null,
      setItem: (key, value) => values.set(key, String(value)),
      removeItem: (key) => values.delete(key),
    },
  };
  try {
    await callback();
  } finally {
    if (originalWindow === undefined) {
      delete globalThis.window;
    } else {
      globalThis.window = originalWindow;
    }
  }
};


test("auth and business clients share the fixed 127.0.0.1 API origin", () => {
  assert.equal(resolveAuthApiBase(), "http://127.0.0.1:8000/api");
});


test("current-user request uses the session token as Bearer and omits cookies", async () => {
  const requests = [];
  await withBrowserSession(async () => {
    storeAccessToken("browser-session-token");
    await withMockFetch(
      async (url, init) => {
        requests.push({ url: String(url), init });
        return Response.json({
          id: "user-1",
          username: "planner",
          display_name: "产品规划负责人",
          role: "user",
        });
      },
      async () => {
        const user = await getCurrentUser();
        assert.deepEqual(user, {
          id: "user-1",
          username: "planner",
          displayName: "产品规划负责人",
          role: "user",
        });
      },
    );
  });

  assert.equal(requests.length, 1);
  assert.equal(requests[0].init.credentials, "omit");
  assert.equal(requests[0].init.cache, "no-store");
  assert.equal(
    requests[0].init.headers.Authorization,
    "Bearer browser-session-token",
  );
  assert.match(requests[0].url, /127\.0\.0\.1:8000\/api\/auth\/me$/);
});


test("missing local token avoids a request and a 401 clears a stale token", async () => {
  await withBrowserSession(async () => {
    let requestCount = 0;
    await withMockFetch(
      async () => {
        requestCount += 1;
        return Response.json({ detail: "请先登录" }, { status: 401 });
      },
      async () => {
        assert.equal(await getCurrentUser(), null);
        assert.equal(requestCount, 0);
        storeAccessToken("stale-token");
        assert.equal(await getCurrentUser(), null);
        assert.equal(requestCount, 1);
        assert.equal(getAccessToken(), null);
      },
    );
  });
});


test("login trims the username, sends no old token, and stores access_token", async () => {
  let capturedBody = "";
  let capturedInit;
  await withBrowserSession(async () => {
    storeAccessToken("old-token");
    await withMockFetch(
      async (_url, init) => {
        capturedBody = String(init.body);
        capturedInit = init;
        return Response.json({
          user: {
            id: "admin-1",
            username: "admin",
            display_name: "系统管理员",
            role: "admin",
          },
          expires_at: "2026-08-01T12:00:00Z",
          access_token: "new-random-token",
          token_type: "bearer",
        });
      },
      async () => {
        const result = await login({
          username: " admin ",
          password: "local-secret",
        });
        assert.equal(result.user.displayName, "系统管理员");
        assert.equal(result.expiresAt, "2026-08-01T12:00:00Z");
        assert.equal(getAccessToken(), "new-random-token");
      },
    );
  });
  assert.deepEqual(JSON.parse(capturedBody), {
    username: "admin",
    password: "local-secret",
  });
  assert.equal(capturedInit.credentials, "omit");
  assert.equal(capturedInit.headers.Authorization, undefined);
});


test("logout sends Bearer, omits cookies, and always clears local session", async () => {
  const requests = [];
  await withBrowserSession(async () => {
    storeAccessToken("logout-token");
    await withMockFetch(
      async (_url, init) => {
        requests.push(init);
        return Response.json({ message: "已退出登录" });
      },
      async () => logout(),
    );
    assert.equal(getAccessToken(), null);
  });
  assert.equal(requests[0].method, "POST");
  assert.equal(requests[0].credentials, "omit");
  assert.equal(requests[0].headers.Authorization, "Bearer logout-token");

  await withBrowserSession(async () => {
    storeAccessToken("expired-token");
    await withMockFetch(
      async () => Response.json({ detail: "请先登录" }, { status: 401 }),
      async () => {
        await assert.rejects(
          () => logout(),
          (error) => error instanceof AuthApiError && error.status === 401,
        );
      },
    );
    assert.equal(getAccessToken(), null);
  });
});


test("API errors retain status and message", async () => {
  await withBrowserSession(async () => {
    await withMockFetch(
      async () => Response.json({ detail: "服务不可用" }, { status: 503 }),
      async () => {
        await assert.rejects(
          () => login({ username: "planner", password: "bad" }),
          (error) =>
            error instanceof AuthApiError &&
            error.status === 503 &&
            error.message === "服务不可用",
        );
      },
    );
  });
});

test("logout removes protected content for success, expired sessions, and server/network failures", async () => {
  for (const outcome of [200, 401, 503, "offline"]) {
    await withBrowserSession(async () => {
      storeAccessToken("temporary-component-test-token");
      await withMockFetch(async (url) => {
        if (String(url).endsWith("/auth/me")) {
          return Response.json({ id: "test-user", username: "planner", display_name: "测试", role: "user" });
        }
        if (outcome === "offline") throw new TypeError("Failed to fetch");
        return Response.json({ message: "已注销", detail: "认证服务不可用" }, { status: outcome });
      }, async () => {
        const harness = componentHarness();
        const { default: LoginGate } = await loadModule("../../app/login-gate.tsx", {
          react: harness.hooks,
          "./auth-api": authApi,
        });
        let authenticated;
        let protectedRenders = 0;
        const props = { children: (auth) => {
          authenticated = auth;
          protectedRenders += 1;
          return "受保护的客户资料";
        } };
        harness.render(LoginGate, props);
        await harness.settle();
        assert.match(renderedText(harness.render(LoginGate, props)), /受保护的客户资料/);
        await authenticated.logout();
        const afterLogout = renderedText(harness.render(LoginGate, props));
        assert.equal(getAccessToken(), null, String(outcome));
        assert.match(afterLogout, /登录意向宝/, String(outcome));
        assert.doesNotMatch(afterLogout, /受保护的客户资料/, String(outcome));
        assert.equal(protectedRenders, 1, "authenticated children must not render after logout");
        if (outcome === 503 || outcome === "offline") {
          assert.match(afterLogout, /已退出本地登录.*服务端会话注销未确认/);
        } else {
          assert.doesNotMatch(afterLogout, /服务端会话注销未确认/);
        }
      });
    });
  }
});
