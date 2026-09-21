// Snapshot loader worker (§2.4 perf): fetch + JSON parse + columnar pack
// off the main thread. Typed arrays go back transferable, so the 15k-node
// snapshot crosses the boundary without a copy. Falls back to the
// deterministic mock when the real endpoint is not shipped yet (M1 pending)
// — same downstream path, so switching to real data needs no UI changes.

import { packSnapshot } from "./pack";
import { buildMockSnapshot } from "./mock";
import type { LoadProgress, RawMapSnapshot } from "./types";

interface LoadRequest {
  type: "load";
  url: string;
  withPreview: boolean;
  /** force the mock generator regardless of the endpoint (debug flag) */
  mock?: boolean;
}

type WorkerEvent =
  | { type: "progress"; progress: LoadProgress }
  | { type: "meta"; meta: { version: string; nodeCount: number; edgeCount: number; mock: boolean } }
  | { type: "done"; packed: unknown; transfer: ArrayBuffer[] }
  | { type: "fail"; message: string; mockAvailable: boolean };

const post = (message: WorkerEvent): void => self.postMessage(message);

/** Streamed download with byte progress (gzip makes content-length fuzzy). */
async function downloadJson(url: string, onProgress: (fraction: number) => void): Promise<unknown> {
  const res = await fetch(url, { headers: { Accept: "application/json" } });
  if (!res.ok) throw new Error(`HTTP ${res.status} ${res.statusText}`);

  const totalHeader = Number(res.headers.get("content-length") ?? 0);
  if (!res.body || !totalHeader) return await res.json();

  const reader = res.body.getReader();
  const chunks: Uint8Array[] = [];
  let received = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    chunks.push(value);
    received += value.byteLength;
    onProgress(Math.min(0.98, received / totalHeader));
  }

  const merged = new Uint8Array(received);
  let cursor = 0;
  for (const chunk of chunks) {
    merged.set(chunk, cursor);
    cursor += chunk.byteLength;
  }
  return JSON.parse(new TextDecoder().decode(merged));
}

/** Read the raw snapshot from the wire, or synthesize the mock twin. */
async function loadRaw(request: LoadRequest): Promise<{ raw: RawMapSnapshot; mock: boolean }> {
  if (request.mock) {
    post({ type: "progress", progress: { phase: "mock", fraction: 0.2 } });
    const raw = buildMockSnapshot();
    post({ type: "progress", progress: { phase: "pack", fraction: 0.75 } });
    return { raw, mock: true };
  }

  post({ type: "progress", progress: { phase: "connect", fraction: 0.02 } });
  let raw: RawMapSnapshot;
  try {
    raw = (await downloadJson(request.url, (fraction) => {
      post({ type: "progress", progress: { phase: "download", fraction } });
    })) as RawMapSnapshot;
  } catch {
    // M1/M2 not deployed yet (or offline dev) — the mock keeps the map alive
    post({ type: "progress", progress: { phase: "mock", fraction: 0.2 } });
    const fallback = buildMockSnapshot();
    post({ type: "progress", progress: { phase: "pack", fraction: 0.75 } });
    return { raw: fallback, mock: true };
  }

  if (!raw || !Array.isArray(raw.nodes) || raw.nodes.length === 0) {
    throw new Error("snapshot payload malformed");
  }
  post({ type: "progress", progress: { phase: "parse", fraction: 0.55 } });
  return { raw, mock: false };
}

self.onmessage = async (event: MessageEvent<LoadRequest>) => {
  if (event.data?.type !== "load") return;
  try {
    const { raw, mock } = await loadRaw(event.data);
    const packed = packSnapshot(raw, event.data.withPreview);
    post({ type: "progress", progress: { phase: "pack", fraction: 0.98 } });
    post({
      type: "meta",
      meta: { version: packed.version, nodeCount: packed.nodeCount, edgeCount: packed.edgeCount, mock },
    });
    // every typed array crosses zero-copy: list the backing buffers
    const transfer = [
      packed.nodeStrBytes.buffer,
      packed.nodeStrOffsets.buffer,
      packed.nodeMeta.buffer,
      packed.nodePositions.buffer,
      packed.edgeData.buffer,
      packed.edgeWeights.buffer,
      packed.adjOffsets.buffer,
      packed.adjList.buffer,
      packed.clusterNs.buffer,
    ] as ArrayBuffer[];
    post({ type: "done", packed, transfer });
  } catch (error) {
    post({
      type: "fail",
      message: error instanceof Error ? error.message : "snapshot load failed",
      mockAvailable: true,
    });
  }
};
