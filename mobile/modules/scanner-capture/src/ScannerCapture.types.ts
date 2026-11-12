import type { StyleProp, ViewStyle } from 'react-native';

export type CaptureState = 'idle' | 'preview' | 'recording' | 'paused' | 'finishing';

export type CaptureWarning =
  | 'fast_motion'
  | 'low_light'
  | 'tracking_limited'
  | 'tracking_lost'
  | 'featureless'
  | 'too_close'
  | 'thermal_serious'
  | 'thermal_critical'
  | 'storage_low';

/** ~4 Hz while the AR session runs. */
export type CaptureStatus = {
  state: CaptureState;
  tracking: string; // normal | limited:<reason> | notAvailable
  fps: number;
  frames: number;
  droppedFrames: number;
  stills: number;
  segments: number;
  elapsedS: number;
  distanceM: number;
  bytesWritten: number;
  freeBytes: number;
  thermal: 'nominal' | 'fair' | 'serious' | 'critical';
  ambientLux: number;
  linearVelocity: number;
  angularVelocity: number;
  warnings: CaptureWarning[];
  /** ARKit world frame (Y up). Top-down map uses x,z. yaw = heading of camera forward in the XZ plane. */
  pose: { x: number; y: number; z: number; yaw: number };
  error?: string;
};

/** ~1 Hz: flat [x0,z0,x1,z1,…] arrays in ARKit metres. */
export type CaptureMap = { trajectory: number[]; coverage: number[]; cell: number };

export type CaptureEvent =
  | { type: 'paused' | 'resumed' | 'interrupted' | 'interruptionEnded' | 'finalizing'; [k: string]: unknown }
  | { type: 'error'; message: string };

export type LocalScanMeta = {
  phase?: LocalPhase;
  serverScanId?: string;
  error?: string;
  updatedAt?: string;
  [k: string]: unknown;
};

/** recorded → creating → uploading → completing → completed (local files deleted after completed). */
export type LocalPhase = 'recording' | 'recorded' | 'creating' | 'uploading' | 'completing' | 'completed' | 'failed';

export type LocalScan = {
  scanId: string;
  dir: string;
  sizeBytes: number;
  hasManifest: boolean;
  name?: string;
  createdAt?: string;
  durationS?: number;
  thumbnailUri?: string;
  meta: LocalScanMeta;
};

export type UploadTarget = { path: string; url: string };

export type UploadJobState = 'uploading' | 'done' | 'needsUrls' | 'error' | 'paused';

export type UploadFileState = {
  path: string;
  size: number;
  committed: number;
  status: 'pending' | 'uploading' | 'done' | 'expired' | 'failed';
  error?: string | null;
};

export type UploadState = {
  scanId: string;
  state: UploadJobState;
  wifiOnly: boolean;
  sentBytes: number;
  totalBytes: number;
  filesDone: number;
  filesTotal: number;
  files: UploadFileState[];
  expiredPaths: string[];
  error?: string;
};

export type UploadProgress = {
  scanId: string;
  sentBytes: number;
  totalBytes: number;
  filesDone: number;
  filesTotal: number;
};

export type DeviceInfo = {
  model: string;
  os: string;
  appVersion: string;
  supported: boolean;
  supportsMesh: boolean;
  videoFormat: { w: number; h: number; fps: number } | null;
  freeBytes: number;
};

export type StartRecordingOptions = {
  name: string;
  stillSpacingM?: number;
  stillAngleDeg?: number;
  stills?: boolean;
};

export type ScannerCaptureEvents = {
  onStatus: (s: CaptureStatus) => void;
  onMap: (m: CaptureMap) => void;
  onCaptureEvent: (e: CaptureEvent) => void;
  onUploadProgress: (p: UploadProgress) => void;
  onUploadState: (s: UploadState) => void;
};

export type CapturePreviewProps = {
  style?: StyleProp<ViewStyle>;
  showMesh?: boolean;
};
