import { namespaceColor } from "../lib/colors";

/** Spectrum dot — the only place a namespace hue paints a chip (§2.5). */
export function NsDot({ uid, size = 8 }: { uid: string | null; size?: number }) {
  return (
    <span
      className="dot"
      aria-hidden="true"
      style={{ background: namespaceColor(uid), width: size, height: size }}
    />
  );
}
