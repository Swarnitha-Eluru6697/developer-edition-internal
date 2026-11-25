/**
 * Pralai master logo artwork (Pralai Logo Guidelines v1.0, single-P system).
 * PralaiSymbol: the standalone three-colour P, for compact places (runner headers, icons).
 * PralaiLockup: the P symbol followed by the outlined "ralai" wordmark, with an optional
 * product descriptor underneath (e.g. "AGENT STUDIO"), as in Pralai Agent Studio.
 * The paths are the supplied vector master: never retype, recolour or stretch them.
 */
import type { CSSProperties } from 'react';

const STEM = "M16 12 Q16 8 20 8 H31 Q35 8 35 12 V84 Q35 88 31 88 H20 Q16 88 16 84 Z";
const BOWL = "M30 8 H54 C76 8 88 21 88 39 C88 59 75 70 54 70 H30 V52 H53 C63 52 69 47 69 39 C69 31 63 26 53 26 H30 Z";
const BAR = "M36 35 H63 V43 H36 Z";
const RALAI =
  "M29.606328125 0.0H36.051640625000005V-13.6640625C36.051640625000005 -16.650390625 38.221562500000005 -18.69140625 41.186406250000005 -18.69140625C42.131718750000005 -18.69140625 43.399296875000005 -18.541015625 43.979375000000005 -18.369140625V-24.126953125C43.399296875000005 -24.255859375 42.561406250000005 -24.341796875 41.895390625000005 -24.341796875C39.188359375000005 -24.341796875 36.975468750000005 -22.794921875 36.094609375000005 -19.830078125H35.836796875000005V-24.01953125H29.606328125Z M53.347421875 0.47265625C56.89234375 0.47265625 59.21265625 -1.095703125 60.3728125 -3.330078125H60.58765625V0.0H66.68921875000001V-16.177734375C66.68921875000001 -22.0 61.661875 -24.3203125 56.505625 -24.3203125C51.13453125 -24.3203125 47.26734375 -21.935546875 46.042734375 -17.767578125L51.9509375 -16.779296875C52.4665625 -18.326171875 53.991953125 -19.55078125 56.484140625 -19.55078125C58.890390625 -19.55078125 60.24390625 -18.390625 60.24390625 -16.349609375V-16.2421875C60.24390625 -14.673828125 58.546640625 -14.501953125 54.421640625 -14.09375C49.695078125 -13.642578125 45.355234375 -12.095703125 45.355234375 -6.724609375C45.355234375 -1.9765625 48.77125 0.47265625 53.347421875 0.47265625ZM55.17359375 -4.060546875C53.046640625 -4.060546875 51.542734375 -5.02734375 51.542734375 -6.896484375C51.542734375 -8.830078125 53.132578125 -9.75390625 55.51734375 -10.09765625C56.9353125 -10.291015625 59.491953125 -10.634765625 60.286875 -11.193359375V-8.59375C60.286875 -6.05859375 58.18140625 -4.060546875 55.17359375 -4.060546875Z M77.75453125 -32.01171875H71.30921875V0.0H77.75453125Z M89.056171875 0.47265625C92.60109375 0.47265625 94.92140625 -1.095703125 96.0815625 -3.330078125H96.29640625V0.0H102.39796875V-16.177734375C102.39796875 -22.0 97.370625 -24.3203125 92.214375 -24.3203125C86.84328125 -24.3203125 82.97609375 -21.935546875 81.751484375 -17.767578125L87.6596875 -16.779296875C88.1753125 -18.326171875 89.700703125 -19.55078125 92.192890625 -19.55078125C94.599140625 -19.55078125 95.95265625 -18.390625 95.95265625 -16.349609375V-16.2421875C95.95265625 -14.673828125 94.255390625 -14.501953125 90.130390625 -14.09375C85.403828125 -13.642578125 81.063984375 -12.095703125 81.063984375 -6.724609375C81.063984375 -1.9765625 84.48 0.47265625 89.056171875 0.47265625ZM90.88234375 -4.060546875C88.755390625 -4.060546875 87.251484375 -5.02734375 87.251484375 -6.896484375C87.251484375 -8.830078125 88.841328125 -9.75390625 91.22609375 -10.09765625C92.6440625 -10.291015625 95.200703125 -10.634765625 95.995625 -11.193359375V-8.59375C95.995625 -6.05859375 93.89015625 -4.060546875 90.88234375 -4.060546875Z M107.01796875000001 0.0H113.46328125000001V-24.01953125H107.01796875000001Z";

const FOREST = "#0B3B2E";
const EMERALD = "#12B886";
const AMBER = "#F2B134";

function PMark({ stem = FOREST }: { stem?: string }) {
  return (
    <>
      <path d={STEM} fill={stem} />
      <path d={BOWL} fill={EMERALD} />
      <path d={BAR} fill={AMBER} />
    </>
  );
}

/** The standalone P symbol. `reversed` = white stem, for forest backgrounds. */
export function PralaiSymbol({ size = 24, reversed = false, style }: { size?: number; reversed?: boolean; style?: CSSProperties }) {
  return (
    <svg viewBox="0 0 96 96" width={size} height={size} role="img" aria-label="Pralai" style={{ display: "block", flex: "none", ...style }}>
      <PMark stem={reversed ? "#FFFFFF" : FOREST} />
    </svg>
  );
}

/** One P symbol + "ralai", with an optional product descriptor below the wordmark. */
export function PralaiLockup({ height = 34, label, reversed = false }: { height?: number; label?: string; reversed?: boolean }) {
  const width = (height * 150) / 64;
  const ink = reversed ? "#FFFFFF" : FOREST;
  return (
    <span style={{ display: "inline-flex", flexDirection: "column", alignItems: "flex-start", lineHeight: 1, minWidth: 0 }}>
      <svg viewBox="0 0 150 64" width={width} height={height} style={{ display: "block", flex: "none", width, height, maxWidth: "none" }}>
        <title>Pralai</title>
        <g transform="translate(-6.96 5.12) scale(0.56)"><PMark stem={ink} /></g>
        <g transform="translate(26 43.88)"><path d={RALAI} fill={ink} /></g>
        <circle cx="136.24" cy="13.42" r="3.53" fill={AMBER} />
      </svg>
      {label && (
        <span
          style={{
            display: "block", font: "700 9px/1 Inter, system-ui, sans-serif", letterSpacing: ".16em", textTransform: "uppercase",
            color: reversed ? "#CFE8DD" : "#0A7D5A", marginTop: -Math.round(height * 0.12), marginLeft: Math.round(width * 0.37), whiteSpace: "nowrap",
          }}
        >
          {label}
        </span>
      )}
    </span>
  );
}
