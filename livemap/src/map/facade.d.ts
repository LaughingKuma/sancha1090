// The typed seam between the imperative map and the feature islands: a feature receives a MapFacade at
// init and nothing else. The implementation is livemap/static/facade.js until the map itself is bundled.
export interface PathPoint { lon: number; lat: number; ts: number }

// The map's shared state cell, narrowed to the fields the facade owns.
export interface PathState {
  pathFetchSeq: number;
  histPathN: number;
  histProvisional: boolean;
  histPts: PathPoint[];
  dimLive: number;
}

export interface MapBounds {
  getWest(): number;
  getEast(): number;
  getSouth(): number;
  getNorth(): number;
}
export interface MapLike {
  getBounds(): MapBounds;
  fitBounds(
    bounds: [[number, number], [number, number]],
    opts: { padding: number; maxZoom: number; duration: number },
  ): void;
}

export interface FacadeDeps {
  S: PathState;
  map: MapLike;
  mapEl: HTMLElement | null;
  setHistPath(points: unknown[]): number;
  clearHistPath(): void;
  clearSelection(): void;
}

// superseded is not a failure: a newer owner claimed the pipeline and already owns what is drawn.
export type PathStatus = "ok" | "empty" | "superseded" | "failed";
export interface PathResult { status: PathStatus; n: number }

export interface MapFacade {
  showFlightPath(flightId: string, opts?: { fit?: boolean }): Promise<PathResult>;
  clearPath(): void;
  dimLive(x: number): void;
  clearSelection(): void;
  guardMapClicks(on: boolean): void;
}
