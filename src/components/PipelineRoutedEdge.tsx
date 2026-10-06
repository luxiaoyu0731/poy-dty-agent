import { pipelineCardGeometry, pipelineLaneGeometry, pipelineRowGeometry } from "./pipelineCardGeometry";
import { BaseEdge, getSmoothStepPath, type EdgeProps } from "@xyflow/react";

type Point = [number, number];

// Presentation-only routing: API edge identities stay unchanged. Dedicated tracks
// follow the fixed three-row layout, including its horizontal drawer compression.
function roundedPath(points: Point[], radius = 8): string {
  let path = `M ${points[0].join(" ")}`;
  for (let i = 1; i < points.length - 1; i++) {
    const [x, y] = points[i];
    const before = points[i - 1], after = points[i + 1];
    const incoming = Math.hypot(x - before[0], y - before[1]);
    const outgoing = Math.hypot(after[0] - x, after[1] - y);
    if (!incoming || !outgoing) continue;
    const r = Math.min(radius, incoming / 2, outgoing / 2);
    const a = [x + (before[0] - x) * r / incoming, y + (before[1] - y) * r / incoming];
    const b = [x + (after[0] - x) * r / outgoing, y + (after[1] - y) * r / outgoing];
    path += ` L ${a.join(" ")} Q ${x} ${y} ${b.join(" ")}`;
  }
  return `${path} L ${points[points.length - 1].join(" ")}`;
}

export function PipelineRoutedEdge(props: EdgeProps) {
  const { sourceX: sx, sourceY: sy, targetX: tx, targetY: ty, source, target, data } = props;
  const scale = data?.compact ? 0.7 : 1;
  const right = 2364 * scale;
  const kind = data?.routeKind;
  let points: Point[] | undefined;
  let labelX = (sx + tx) / 2, labelY = (sy + ty) / 2;

  if (kind === "flow") {
    if (Math.abs(sy - ty) < 1) {
      points = [[sx, sy], [tx, ty]];
    } else if (ty > sy) {
      // Cross-row mainline stays outside both lane rectangles. The horizontal
      // portion runs between rows, away from the next row's title and chips.
      const track = pipelineCardGeometry(source).y + pipelineCardGeometry(source).height + pipelineLaneGeometry.gutterOffset;
      const outer = right + 36;
      points = [[sx, sy], [outer, sy], [outer, track], [-36, track], [-36, ty], [tx, ty]];
    }
  } else if (kind === "feedback") {
    const track = Math.min(sy, ty) - (target === "political_analysis" ? 10 : 3);
    points = [[sx, sy], [sx, track], [tx, track], [tx, ty]];
    labelX = target === "political_analysis" ? 1250 * scale : 1750 * scale;
    labelY = track;
  } else if (kind === "interaction" && target === "assistant") {
    const entry = (source === "event_summary" ? 1982 : source === "skeptic_review" ? 1960 : 1938) * scale;
    if (source === "event_summary") {
      const outer = right + 68;
      const firstRowGutter = pipelineRowGeometry.preparation.height + pipelineLaneGeometry.gutterOffset;
      const secondRowGutter = pipelineRowGeometry.prediction.y + pipelineRowGeometry.prediction.height + pipelineLaneGeometry.gutterOffset;
      points = [[sx, sy], [sx, firstRowGutter], [outer, firstRowGutter], [outer, secondRowGutter], [entry, secondRowGutter], [entry, ty], [tx, ty]];
      labelX = outer; labelY = 620;
    } else {
      const track = pipelineCardGeometry(source).y + pipelineCardGeometry(source).height + pipelineLaneGeometry.gutterOffset;
      points = [[sx, sy], [sx, track], [entry, track], [entry, ty], [tx, ty]];
      labelX = (sx + entry) / 2; labelY = track;
    }
  }

  const path = points ? roundedPath(points) : getSmoothStepPath(props)[0];
  return <BaseEdge id={props.id} path={path} markerEnd={props.markerEnd} style={props.style}
    label={props.label} labelX={labelX} labelY={labelY}
    labelStyle={props.labelStyle} labelBgStyle={props.labelBgStyle}
    labelBgPadding={[6, 4]} labelBgBorderRadius={4} />;
}

export const pipelineEdgeTypes = { pipelineRouted: PipelineRoutedEdge };
