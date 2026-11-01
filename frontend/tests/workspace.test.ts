import assert from "node:assert/strict";
import test from "node:test";
import { phaseLabels, healthLabels, seconds, RequestEvents, sessionCaption, recipientLabels } from "../src/workspace_state.ts";

test("every backend activity has a readable label", () => {
  for (const phase of ["idle", "recognizing", "processing", "switching", "generating", "error"]) {
    assert.ok(phaseLabels[phase]);
  }
});

test("late speech and waiting events cannot alter a newer request", () => {
  const events = new RequestEvents();
  assert.equal(events.accept({request_id: 2}), true);
  assert.equal(events.accept({request_id: 1}), false);
  assert.equal(events.accept({request_id: 3}), true);
  events.reset();
  assert.equal(events.accept({request_id: 1}), true);
  for (const phase of ["listening", "waiting", "speaking", "interrupted"]) assert.ok(phaseLabels[phase]);
});
test("model eligibility is not presented as verified availability", () => {
  assert.notEqual(healthLabels.unknown, healthLabels.ready);
  for (const status of ["open", "probing", "cancelled", "failed"]) assert.ok(healthLabels[status]);
});
test("zero latency and absent measurements stay distinct", () => {
  assert.equal(seconds(0), "0.00 с");
  assert.equal(seconds(null), "—");
  assert.equal(seconds(1.235), "1.24 с");
});

test("session captions distinguish follow-up listening from a mandatory wake word", () => {
  assert.match(sessionCaption({active: true, listening: true, requires_wake: false}), /продолжение без имени/);
  assert.match(sessionCaption({active: true, listening: false, requires_wake: false}), /активна/);
  assert.match(sessionCaption({active: false, listening: false, requires_wake: true}), /wake-word/);
});

test("recipient states have plain labels and stale task updates are rejected", () => {
  for (const state of ["direct", "continuation", "waiting", "task", "task_running", "ambiguous"] as const) assert.ok(recipientLabels[state]);
  const events = new RequestEvents();
  assert.ok(events.accept({request_id: 4}));
  assert.equal(events.accept({request_id: 3}), false);
});
