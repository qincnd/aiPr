/** 与后端 pydantic 模型对应的类型定义（只保留前端会用到的字段）。 */

export type MediaKind = 'video' | 'audio' | 'image' | 'project' | 'other'

export interface MediaAsset {
  id: string
  path: string
  name: string
  stem: string
  relative_path: string
  kind: MediaKind
  extension: string
  size_bytes: number
  duration_seconds: number | null
  width: number | null
  height: number | null
  fps: number | null
  codec: string | null
  sample_rate: number | null
  channels: number | null
  probe_source: 'ffprobe' | 'metadata-only'
}

export interface MediaLibrary {
  root: string
  scanned_at: string
  recursive: boolean
  probed: boolean
  assets: MediaAsset[]
  skipped: string[]
  totals: Record<string, number>
}

export interface MediaScanResponse {
  library: MediaLibrary
  ffprobe_available: boolean
}

export interface BackendInfo {
  backend: string
  available: boolean
  connected: boolean
  message: string
  /** 操作名 -> 支持程度（native / simulated / partial） */
  capabilities: Record<string, string>
}

export interface SystemInfo {
  app_name: string
  version: string
  python_version: string
  premiere: BackendInfo
  llm_enabled: boolean
  llm_configured: boolean
  llm_model: string
  llm_base_url: string
  ffprobe_available: boolean
  ffprobe_path: string | null
  media_roots: string[]
  workspace_dir: string
}

export type OperationType =
  | 'import_media'
  | 'create_sequence'
  | 'append_clip'
  | 'insert_clip'
  | 'move_clip'
  | 'trim_clip'
  | 'razor'
  | 'delete_range'
  | 'apply_transition'
  | 'apply_effect'
  | 'set_volume'
  | 'add_marker'
  | 'export_sequence'

export interface Operation {
  op: OperationType
  note?: string | null
  enabled?: boolean
  [key: string]: unknown
}

export interface SequenceSpec {
  name: string
  width: number
  height: number
  fps: number
  preset: string | null
}

export interface EditPlan {
  version: string
  project_name: string
  summary: string
  sequence: SequenceSpec
  operations: Operation[]
  warnings: string[]
  source: 'llm' | 'heuristic' | 'manual'
}

export interface PlanResponse {
  plan: EditPlan
  library: MediaLibrary
  planner: 'llm' | 'heuristic'
  warnings: string[]
}

export interface PlanOptions {
  use_llm: boolean
  target_duration: number | null
  max_clips: number
  width: number
  height: number
  fps: number
  preset: string | null
  extra_instructions: string
}

export type JobStatus = 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled'
export type EventLevel = 'debug' | 'info' | 'success' | 'warning' | 'error'

export interface JobEvent {
  seq: number
  at: string
  level: EventLevel
  message: string
  op_index: number | null
  op: string | null
  data: Record<string, unknown>
}

export interface TimelineClip {
  id: string
  name: string
  media_ref: string
  track: string
  start: number
  end: number
  source_in: number
  source_out: number
}

export interface TimelineTrack {
  name: string
  kind: 'video' | 'audio'
  clips: TimelineClip[]
}

export interface TimelineState {
  sequence_name: string
  width: number
  height: number
  fps: number
  tracks: TimelineTrack[]
  markers: { at: number; name: string; color: string; duration: number }[]
  effects: { clip_id: string; effect: string; params: Record<string, unknown> }[]
  transitions: { track: string; at: number; transition: string; duration: number }[]
}

export interface JobRecord {
  id: string
  status: JobStatus
  created_at: string
  started_at: string | null
  finished_at: string | null
  media_root: string | null
  dry_run: boolean
  plan: EditPlan | null
  progress: number
  error: string | null
  artifacts: string[]
  timeline: TimelineState | null
  run_dir: string | null
}

export interface PromptPayload {
  prompt: string
  options: Partial<PlanOptions>
}

export interface JobCreatePayload {
  prompt?: string
  plan?: EditPlan
  media_root?: string | null
  media_ids?: string[]
  options?: Partial<PlanOptions>
  dry_run?: boolean
  continue_on_error?: boolean
  auto_export?: boolean
}
