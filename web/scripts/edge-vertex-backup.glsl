// Старое тело EDGE_VERTEX (до бисекта) — восстановить в shaders.ts после диагностики.
// Полная копия была в shaders.ts до коммита бисекта; ключевая часть:
//
// void main() {
//   vec4 clipA = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
//   vec4 clipB = projectionMatrix * modelViewMatrix * vec4(aOther, 1.0);
//   vec4 clipSelf = mix(clipA, clipB, aEnd);
//   float dist = -mix(modelViewMatrix * vec4(position, 1.0), modelViewMatrix * vec4(aOther, 1.0), aEnd).z;
//   vec2 ndcA = clipA.xy / max(clipA.w, 0.0001);
//   vec2 ndcB = clipB.xy / max(clipB.w, 0.0001);
//   vec2 screenDir = ndcB - ndcA;
//   screenDir.x *= uViewport.x * 0.5;
//   screenDir.y *= uViewport.y * 0.5;
//   float len = length(screenDir);
//   vec2 perpPx = (len > 0.0001) ? vec2(-screenDir.y, screenDir.x) / len : vec2(1.0, 0.0);
//   vec2 ndcPerp = perpPx / vec2(uViewport.x * 0.5, uViewport.y * 0.5);
//   float halfWidth = uEdgeWidth * 0.5;
//   vec4 clip = clipSelf + vec4(ndcPerp * aSide * halfWidth * 2.0 * clipSelf.w, 0.0, 0.0);
//   float fade = 1.0 - smoothstep(FADE_START, FADE_END, dist);
//   float base = mix(0.75, 1.0, clamp((aWeight - 1.0) / 2.0, 0.0, 1.0));
//   if (aKind > 1.5) base = 1.0;
//   vAlpha = base * fade * (1.0 + aHighlight * 1.6);
//   vPhase = dot(position, vec3(0.0137, 0.0171, 0.0113));
//   vEnd = aEnd; vKind = aKind; vColor = aColor;
//   gl_Position = clip;
// }
