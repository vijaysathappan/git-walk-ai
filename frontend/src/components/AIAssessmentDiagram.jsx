import React from "react";

const TONE = {
  LOW: "#1f883d", MEDIUM: "#9a6700", HIGH: "#cf222e", CRITICAL: "#a40e26",
};

function Box({ x, y, w, h, title, subtitle, fill, stroke }) {
  return (
    <g>
      <rect x={x} y={y} width={w} height={h} rx={8} fill={fill} stroke={stroke} strokeWidth={1.2} />
      <text x={x + w / 2} y={y + h / 2 - 4} textAnchor="middle" fontSize="10.5" fontWeight="700" fill="#0d1117">{title}</text>
      <text x={x + w / 2} y={y + h / 2 + 11} textAnchor="middle" fontSize="8.5" fill="#57606a">{subtitle}</text>
    </g>
  );
}

function Arrow({ x1, x2, y }) {
  return (
    <g>
      <line x1={x1} y1={y} x2={x2 - 6} y2={y} stroke="#8c959f" strokeWidth={1.4} />
      <polygon points={`${x2},${y} ${x2 - 7},${y - 4} ${x2 - 7},${y + 4}`} fill="#8c959f" />
    </g>
  );
}

/**
 * A small, plain block diagram of how this one AI risk assessment was
 * produced — hand-rolled inline SVG, matching every other diagram in this
 * codebase (CommitGraphView, AuditGraphView, RoleFamilyTree, ...), none of
 * which pull in an external charting/diagram library. The point isn't
 * decoration: it's making the deterministic-first, AI-explains-second
 * pipeline visible at a glance, with the final box colored by the actual
 * (hard-rule-computed) risk level, not the model's own opinion.
 */
export default function AIAssessmentDiagram({ riskLevel, riskScore, recommendation, escalated }) {
  const tone = TONE[riskLevel] || TONE.MEDIUM;
  const boxW = 130, boxH = 46, gap = 26, y = 10;
  const xs = [8, 8 + boxW + gap, 8 + 2 * (boxW + gap), 8 + 3 * (boxW + gap)];
  const width = xs[3] + boxW + 8;
  return (
    <svg viewBox={`0 0 ${width} 66`} width="100%" style={{ maxWidth: 560, display: "block", margin: "8px 0 4px" }}>
      <Box x={xs[0]} y={y} w={boxW} h={boxH} title="Evidence gathered" subtitle="Changes, conflicts, cleared fields" fill="#eaf2fd" stroke="#0969da" />
      <Arrow x1={xs[0] + boxW} x2={xs[1]} y={y + boxH / 2} />
      <Box x={xs[1]} y={y} w={boxW} h={boxH} title="Deterministic engine" subtitle={`Score ${Math.round(riskScore)}/100`} fill="#f3ecfd" stroke="#8250df" />
      <Arrow x1={xs[1] + boxW} x2={xs[2]} y={y + boxH / 2} />
      <Box x={xs[2]} y={y} w={boxW} h={boxH} title="AI explains" subtitle={escalated ? "Escalated by hard rules" : "Confirms the score"} fill="#fdf3da" stroke="#9a6700" />
      <Arrow x1={xs[2] + boxW} x2={xs[3]} y={y + boxH / 2} />
      <Box x={xs[3]} y={y} w={boxW} h={boxH} title={recommendation?.replaceAll("_", " ") || "Recommendation"} subtitle={`${riskLevel} risk`} fill={`${tone}22`} stroke={tone} />
    </svg>
  );
}
