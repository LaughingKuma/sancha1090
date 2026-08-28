import type { Fetched } from "../resource";

// One wording of a section's degradation (vanilla views keep their own strings until PR7): an outage outranks
// a missing mart, which outranks "genuinely empty". `what` is the outage noun, `empty` the caller's line.
export function Empty({ what, state, empty }: { what?: string; state?: Fetched<unknown>["state"]; empty?: string }) {
  if (state === "outage" || state === "unreachable")
    return <div class="wb-empty">{what ? `${what} unavailable` : "unavailable"}</div>;
  return <div class="wb-empty">{empty ?? `no ${what} match`}</div>;
}
