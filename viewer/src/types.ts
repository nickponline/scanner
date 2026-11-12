// scene.v1 (CONTRACTS.md §4). Every field the viewer reads is optional-tolerant.

export type LayerKind =
  | "model"
  | "splat"
  | "mesh"
  | "points"
  | "cameras"
  | "trajectory"
  | "plan"
  | "ground";

export interface LayerDef {
  id: string;
  kind: LayerKind | string;
  label: string;
  url?: string;
  fallback_url?: string;
  visible?: boolean;
  summary?: string;
  stats?: Record<string, number>;
  modes?: string[];
  mode?: string;
  styles?: { id: string; url: string; label?: string }[];
  style?: string;
  cell?: number;
  fade?: number;
}

export interface SceneJson {
  schema: string;
  scan_id: string;
  version: number;
  name: string;
  captured_at?: string;
  created_at?: string;
  units?: string;
  up?: string;
  north_deg?: number;
  bounds: { min: number[]; max: number[] };
  levels?: { floor: number; ceiling: number; ceiling_max?: number };
  default_view?: { position: number[]; target: number[] };
  layers: LayerDef[];
  building_model_url?: string;
  stats?: Record<string, number>;
  /** Set by the API: the ".../v<NN>/" prefix; relative URLs in scene.json resolve against it. */
  base_url?: string;
}

export interface CameraDef {
  id: string;
  kind: "keyframe" | "hero" | string;
  t: number;
  full: string;
  thumb: string;
  w: number;
  h: number;
  fx: number;
  fy: number;
  cx: number;
  cy: number;
  /** 4x4 row-major camera->world, OpenCV camera, World frame (Z up). */
  c2w: number[];
  room_id?: string | null;
}

export interface Trajectory {
  t: number[];
  p: number[][];
}

export interface BuildingModel {
  storeys: {
    id: string;
    rooms?: { id: string; name: string; type?: string; area_m2?: number; polygon: number[][] }[];
    furniture?: { id: string; label: string; room_id?: string }[];
    walls?: { id: string }[];
    openings?: { id: string; type: string }[];
  }[];
}
