import { NativeModule, requireOptionalNativeModule } from 'expo';

import type {
  DeviceInfo,
  LocalScan,
  LocalScanMeta,
  ScannerCaptureEvents,
  StartRecordingOptions,
  UploadState,
  UploadTarget,
} from './ScannerCapture.types';

export declare class ScannerCaptureModuleType extends NativeModule<ScannerCaptureEvents> {
  isSupported(): boolean;
  /** Test hook: SCANNER_INITIAL_ROUTE launch env var (simulator screenshots). */
  initialRoute(): string | null;
  getDeviceInfo(): DeviceInfo;
  startSession(): Promise<void>;
  stopSession(): Promise<void>;
  startRecording(opts: StartRecordingOptions): Promise<{ scanId: string; dir: string }>;
  pauseRecording(): Promise<void>;
  resumeRecording(): Promise<void>;
  stopRecording(): Promise<LocalScan>;
  discardRecording(): Promise<void>;
  listLocalScans(): Promise<LocalScan[]>;
  getLocalScan(scanId: string): Promise<LocalScan | null>;
  readManifest(scanId: string): Promise<string | null>;
  setScanMeta(scanId: string, meta: LocalScanMeta): Promise<void>;
  deleteLocalScan(scanId: string): Promise<void>;
  startUpload(scanId: string, uploads: UploadTarget[], wifiOnly: boolean): Promise<void>;
  updateUploadUrls(scanId: string, uploads: UploadTarget[]): Promise<void>;
  getUploadState(scanId: string): Promise<UploadState | null>;
  getAllUploadStates(): Promise<UploadState[]>;
  resumeUploads(): Promise<void>;
  setWifiOnly(on: boolean): Promise<void>;
  cancelUpload(scanId: string): Promise<void>;
  forgetUpload(scanId: string): Promise<void>;
}

/** null when the native module isn't linked (e.g. web, Expo Go, jest). */
export default requireOptionalNativeModule<ScannerCaptureModuleType>('ScannerCapture');
