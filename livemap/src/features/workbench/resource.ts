import { effect, signal, useSignal, type ReadonlySignal } from "@preact/signals";
import { useEffect } from "preact/hooks";

// superseded ≠ failure: a newer key retired the last answer and its replacement is in flight. `prev` carries
// the retired envelope so charts keep drawing it until the answer lands; lists render nothing for it.
export type Fetched<T> =
  | { state: "idle" }
  | { state: "ok"; data: T }
  | { state: "outage" }
  | { state: "unreachable" }
  | { state: "superseded"; prev?: T };

export interface Resource<T> {
  value: ReadonlySignal<Fetched<T>>;
  dispose(): void;
}

// The key is the params serialized over exactly the fields the endpoint reads, so an equal key never re-fetches
export function resource<T, P>(
  paramsFn: () => P | null,
  fetcher: (params: P, signal: AbortSignal) => Promise<T | null>,
): Resource<T> {
  const out = signal<Fetched<T>>({ state: "idle" });
  let seq = 0;
  let last: string | null | undefined;
  let ctl: AbortController | null = null;
  const dispose = effect(() => {
    const params = paramsFn();
    const key = params == null ? null : JSON.stringify(params);
    if (key === last) return;
    const first = last === undefined;
    last = key;
    const my = ++seq;
    ctl?.abort();
    ctl = params == null ? null : new AbortController();
    if (!ctl) {
      out.value = { state: "idle" };
      return;
    }
    if (!first) {
      const was = out.peek();
      out.value = {
        state: "superseded",
        prev: was.state === "ok" ? was.data : was.state === "superseded" ? was.prev : undefined,
      };
    }
    fetcher(params as P, ctl.signal).then(
      (data) => {
        if (my === seq) out.value = data == null ? { state: "outage" } : { state: "ok", data };
      },
      () => {
        if (my === seq) out.value = { state: "unreachable" };
      },
    );
  });
  return {
    value: out,
    dispose: () => {
      seq++;
      ctl?.abort();
      dispose();
    },
  };
}

// Created in a committed effect, never in render: a mount+unmount inside one frame would never register
// the disposer. Callbacks must be module-level — an inline lambda re-creates the resource on every render.
export function useResource<T, P>(
  paramsFn: () => P | null,
  fetcher: (params: P, signal: AbortSignal) => Promise<T | null>,
): Fetched<T> {
  const out = useSignal<Fetched<T>>({ state: "idle" });
  useEffect(() => {
    const res = resource(paramsFn, fetcher);
    const stop = effect(() => {
      out.value = res.value.value;
    });
    return () => {
      stop();
      res.dispose();
    };
  }, [paramsFn, fetcher, out]);
  return out.value;
}
