/** 后端接口封装。 */

import type {
  JobCreatePayload,
  JobRecord,
  MediaScanResponse,
  PlanOptions,
  PlanResponse,
  SystemInfo,
} from './types'

const API_BASE = (import.meta.env.VITE_API_BASE ?? '').replace(/\/$/, '')

export class ApiError extends Error {
  readonly code: string
  readonly status: number

  constructor(message: string, code: string, status: number) {
    super(message)
    this.name = 'ApiError'
    this.code = code
    this.status = status
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  })
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`
    let code = 'http_error'
    try {
      const body = (await response.json()) as { message?: string; detail?: string; code?: string }
      message = body.message ?? body.detail ?? message
      code = body.code ?? code
    } catch {
      /* 响应体不是 JSON，直接用状态码 */
    }
    throw new ApiError(message, code, response.status)
  }
  if (response.status === 204) {
    return undefined as T
  }
  return (await response.json()) as T
}

export interface PlanPayload {
  prompt: string
  media_root: string
  media_ids: string[]
  options: Partial<PlanOptions>
}

export const api = {
  system: () => request<SystemInfo>('/api/system'),

  mediaRoots: () => request<{ roots: string[]; ffprobe_available: boolean }>('/api/media/roots'),

  scan: (path: string, probe = true) =>
    request<MediaScanResponse>('/api/media/scan', {
      method: 'POST',
      body: JSON.stringify({ path, recursive: true, probe }),
    }),

  createPlan: (payload: PlanPayload) =>
    request<PlanResponse>('/api/plan', { method: 'POST', body: JSON.stringify(payload) }),

  createJob: (payload: JobCreatePayload) =>
    request<{ job: JobRecord }>('/api/jobs', { method: 'POST', body: JSON.stringify(payload) }),

  getJob: (jobId: string) => request<{ job: JobRecord }>(`/api/jobs/${jobId}`),

  cancelJob: (jobId: string) =>
    request<{ job: JobRecord }>(`/api/jobs/${jobId}/cancel`, { method: 'POST' }),
}

/** 计算任务日志的 WebSocket 地址（开发环境走 Vite 代理）。 */
export function jobSocketUrl(jobId: string): string {
  if (API_BASE) {
    return `${API_BASE.replace(/^http/, 'ws')}/api/ws/jobs/${jobId}`
  }
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${protocol}//${window.location.host}/api/ws/jobs/${jobId}`
}
