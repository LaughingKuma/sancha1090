import { test } from "node:test";
import assert from "node:assert/strict";
import { emergencyOf } from "../../livemap/static/telemetry.js";

test("an emergency squawk alerts on its own", () => {
  assert.deepEqual(emergencyOf({ squawk: "7700" }), { code: "7700", label: "General" });
  assert.deepEqual(emergencyOf({ squawk: " 7500 " }), { code: "7500", label: "Hijack" });
  assert.equal(emergencyOf({ squawk: "2467" }), null);
  assert.equal(emergencyOf({ squawk: "toString" }), null); // prototype members are not codes
});

test("a transmitted status alerts with no 7x00 squawk (the SKY022 shape: 'general' under squawk 2467)", () => {
  assert.deepEqual(emergencyOf({ squawk: "2467", emergency: "general" }), { code: "EMERG", label: "General" });
  assert.deepEqual(emergencyOf({ emergency: "minfuel" }), { code: "EMERG", label: "Min Fuel" });
  assert.deepEqual(emergencyOf({ emergency: "lifeguard" }), { code: "EMERG", label: "Medical" }); // readsb spelling
  assert.deepEqual(emergencyOf({ emergency: "nocomm" }), { code: "EMERG", label: "No Comm" });
  assert.deepEqual(emergencyOf({ emergency: "nordo" }), { code: "EMERG", label: "No Comm" });
  assert.deepEqual(emergencyOf({ emergency: "unlawful" }), { code: "EMERG", label: "Unlawful" });
  assert.deepEqual(emergencyOf({ emergency: "downed" }), { code: "EMERG", label: "Downed" });
});

test("'none', null and an absent field are not an emergency", () => {
  assert.equal(emergencyOf({ squawk: "1200", emergency: "none" }), null);
  assert.equal(emergencyOf({ squawk: "1200", emergency: null }), null);
  assert.equal(emergencyOf({ squawk: "1200" }), null);
  assert.equal(emergencyOf({ emergency: "  " }), null);
  assert.equal(emergencyOf(null), null);
});

test("any other non-'none' value still alerts, labelled as sent", () => {
  assert.deepEqual(emergencyOf({ emergency: "reserved" }), { code: "EMERG", label: "Reserved" });
  assert.deepEqual(emergencyOf({ emergency: "constructor" }), { code: "EMERG", label: "Constructor" }); // no prototype lookup
});

test("the squawk owns the code slot when both alert", () => {
  assert.deepEqual(emergencyOf({ squawk: "7600", emergency: "general" }), { code: "7600", label: "Radio Fail" });
});
