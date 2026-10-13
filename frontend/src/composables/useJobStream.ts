/** 订阅任务日志流：WebSocket 断了就用 HTTP 轮询兜底。 */

import { onUnmounted, ref } from 'vue'
import { api, jobSocketUrl } from '../api/client'
import type { JobEvent, JobRecord, JobStatus } from '../api/types'

const TERMINAL: JobStatus[] = ['succeeded', 'failed', 'cancelled']

export function useJobStream() {
  const events = ref<JobEvent[]>([])
  const job = ref<JobRecord | null>(null)
  const status = ref<JobStatus | 'idle'>('idle')
  const connected = ref(false)

  let socket: WebSocket | null = null
  let pollTimer: number | null = null
  let currentJobId: string | null = null

  function stopPolling() {
    if (pollTimer !== null) {
      window.clearInterval(pollTimer)
      pollTimer = null
    }
  }

  function disconnect() {
    stopPolling()
    if (socket) {
      socket.onclose = null
      socket.close()
      socket = null
    }
    connected.value = false
  }

  async function refresh() {
    if (!currentJobId) return
    try {
      const { job: latest } = await api.getJob(currentJobId)
      job.value = latest
      status.value = latest.status
      if (TERMINAL.includes(latest.status)) {
        stopPolling()
      }
    } catch {
      stopPolling()
    }
  }

  function startPolling() {
    if (pollTimer !== null) return
    pollTimer = window.setInterval(refresh, 1200)
  }

  function connect(jobId: string) {
    disconnect()
    currentJobId = jobId
    events.value = []
    job.value = null
    status.value = 'queued'

    const ws = new WebSocket(jobSocketUrl(jobId))
    socket = ws

    ws.onopen = () => {
      connected.value = true
    }
    ws.onmessage = (event) => {
      const message = JSON.parse(event.data as string) as {
        type: 'snapshot' | 'event' | 'done'
        job?: JobRecord
        event?: JobEvent
        status?: JobStatus
      }
      if (message.type === 'snapshot' && message.job) {
        job.value = message.job
        status.value = message.job.status
      } else if (message.type === 'event' && message.event) {
        if (!events.value.some((item) => item.seq === message.event!.seq)) {
          events.value.push(message.event)
        }
      } else if (message.type === 'done') {
        status.value = message.status ?? status.value
        connected.value = false
        void refresh()
      }
    }
    ws.onerror = () => {
      connected.value = false
      startPolling()
    }
    ws.onclose = () => {
      connected.value = false
      // 后端重启或任务结束时连接会关闭，用轮询补齐最终状态
      if (!job.value || !TERMINAL.includes(status.value as JobStatus)) {
        startPolling()
        void refresh()
      }
    }
  }

  onUnmounted(disconnect)

  return { events, job, status, connected, connect, disconnect, refresh }
}
