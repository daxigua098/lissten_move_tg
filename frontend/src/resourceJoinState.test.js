import assert from "node:assert/strict";
import test from "node:test";

import { isJoined, isReadable, joinState } from "./resourceJoinState.js";

test("active means readable, not joined", () => {
  const row = { resource_state: "active", join: null };
  assert.equal(isReadable(row), true);
  assert.equal(isJoined(row), false);
  assert.deepEqual(joinState(row), { label: "未加入", type: "info" });
});

test("only a successful join task marks the account as joined", () => {
  assert.equal(isJoined({ join: { action: "join", status: "success" } }), true);
  assert.equal(isJoined({ join: { action: "join", status: "pending" } }), false);
  assert.equal(isJoined({ join: { action: "leave", status: "success" } }), false);
  assert.equal(isJoined({ join: { action: "leave", status: "pending" } }), true);
});
