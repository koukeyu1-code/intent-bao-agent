import assert from "node:assert/strict";
import test from "node:test";

import {
  canAccessView,
  resolveAuthorizedView,
} from "../app/view-permissions.ts";

test("only administrators can access model settings", () => {
  assert.equal(canAccessView("admin", "settings"), true);
  assert.equal(canAccessView("user", "settings"), false);
  assert.equal(canAccessView("viewer", "settings"), false);
});

test("normal users keep access to ordinary application views", () => {
  assert.equal(canAccessView("user", "overview"), true);
  assert.equal(canAccessView("user", "insights"), true);
  assert.equal(canAccessView("user", "dictionary"), true);
});

test("viewers see analysis views but not workflow mutation pages", () => {
  assert.equal(canAccessView("viewer", "overview"), true);
  assert.equal(canAccessView("viewer", "technology"), true);
  assert.equal(canAccessView("viewer", "competition"), true);
  assert.equal(canAccessView("viewer", "history"), true);
  assert.equal(canAccessView("viewer", "dictionary"), true);
  assert.equal(canAccessView("viewer", "meetings"), false);
  assert.equal(canAccessView("viewer", "speakers"), false);
  assert.equal(canAccessView("viewer", "insights"), false);
});

test("a stale or internal settings selection resolves to overview", () => {
  assert.equal(resolveAuthorizedView("user", "settings"), "overview");
  assert.equal(resolveAuthorizedView("viewer", "settings"), "overview");
  assert.equal(resolveAuthorizedView("admin", "settings"), "settings");
  assert.equal(resolveAuthorizedView("user", "technology"), "technology");
});
