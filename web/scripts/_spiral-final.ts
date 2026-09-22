import { readFileSync } from "node:fs";
import { packSnapshot, unpackNodeString } from "../src/lib/fullmap/pack";
import { spiralLayout, FULL_SPIRAL } from "../src/lib/fullmap/layout";
import type { RawMapSnapshot } from "../src/lib/fullmap/types";
const raw = JSON.parse(readFileSync("E:/tmp/selti-map-dump.json", "utf8")) as RawMapSnapshot;
const packed = packSnapshot(raw, true);
const uuids: string[] = [];
for (let i = 0; i < packed.nodeCount; i++) uuids.push(unpackNodeString(packed, i, 0));
const pos = spiralLayout(uuids, packed.nodePositions, FULL_SPIRAL);
let maxR = 0, nan = 0;
const eye = [0, 700, 1150];
let dMin = Infinity, dMax = 0;
for (let i = 0; i < packed.nodeCount; i++) {
  const x = pos[i*3], y = pos[i*3+1], z = pos[i*3+2];
  if (Number.isNaN(x)) nan++;
  maxR = Math.max(maxR, Math.hypot(x, z));
  const dx = x-eye[0], dy = y-eye[1], dz = z-eye[2];
  const d = Math.sqrt(dx*dx+dy*dy+dz*dz);
  dMin = Math.min(dMin, d); dMax = Math.max(dMax, d);
}
console.log(`nan=${nan} maxR=${maxR.toFixed(0)} | camera(0,700,1150) dist ${dMin.toFixed(0)}..${dMax.toFixed(0)} (fade 900..2200)`);
