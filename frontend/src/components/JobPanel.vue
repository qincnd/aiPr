<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import type { JobEvent, JobRecord, JobStatus } from '../api/types'

const props = defineProps<{
  job: JobRecord | null
  events: JobEvent[]
  status: JobStatus | 'idle'
  connected: boolean
}>()

const emit = defineEmits<{ cancel: []; refresh: [] }>()

const logBox = ref<HTMLElement | null>(null)

const statusLabels: Record<JobStatus | 'idle', string> = {
  idle: '空闲',
  queued: '排队中',
  running: '执行中',
  succeeded: '成功',
  failed: '失败',
  cancelled: '已取消',
}

const statusClass = computed(() => {
  switch (props.status) {
    case 'succeeded':
      return 'ok'
    case 'failed':
      return 'err'
    case 'cancelled':
      return 'warn'
    case 'running':
      return 'ok'
    default:
      return ''
  }
})

const timeline = computed(() => props.job?.timeline ?? null)

const timelineSummary = computed(() => {
  const state = timeline.value
  if (!state) return ''
  const clipCount = state.tracks.reduce((total, track) => total + track.clips.length, 0)
  const duration = state.tracks.reduce(
    (longest, track) => Math.max(longest, ...track.clips.map((clip) => clip.end), 0),
    0,
  )
  return `${state.sequence_name} · ${clipCount} 个片段 · ${duration.toFixed(2)}s`
})

function formatTime(iso: string): string {
  return new Date(iso).toLocaleTimeString('zh-CN', { hour12: false })
}

watch(
  () => props.events.length,
  async () => {
    await nextTick()
    if (logBox.value) {
      logBox.value.scrollTop = logBox.value.scrollHeight
    }
  },
)
</script>

<template>
  <section class="panel">
    <div class="panel-title">
      <span>④ 执行日志</span>
      <div class="row">
        <span class="chip"><i class="dot" :class="statusClass"></i>{{ statusLabels[status] }}</span>
        <span class="chip">{{ connected ? '实时连接' : '已断开' }}</span>
      </div>
    </div>

    <div v-if="job" class="row">
      <span class="chip mono">{{ job.id }}</span>
      <span v-if="job.dry_run" class="chip">预演模式</span>
      <span v-if="job.media_root" class="chip" :title="job.media_root">
        {{ job.media_root }}
      </span>
    </div>

    <div class="progress">
      <div :style="{ width: `${Math.round((job?.progress ?? 0) * 100)}%` }"></div>
    </div>

    <p v-if="timelineSummary" class="muted">时间线：{{ timelineSummary }}</p>
    <p v-if="job?.error" class="muted" style="color: var(--err)">错误：{{ job.error }}</p>

    <div v-if="job?.artifacts.length" class="artifacts">
      <div class="muted">产物：</div>
      <code v-for="artifact in job.artifacts" :key="artifact">{{ artifact }}</code>
    </div>

    <div v-if="timeline" class="timeline">
      <div v-for="track in timeline.tracks" :key="track.name" class="track">
        <span class="track-name mono">{{ track.name }}</span>
        <div class="track-body">
          <span v-if="!track.clips.length" class="muted">空</span>
          <span
            v-for="clip in track.clips"
            :key="clip.id"
            class="clip"
            :title="`${clip.name}\n${clip.start.toFixed(2)}s → ${clip.end.toFixed(2)}s（源 ${clip.source_in.toFixed(2)}s → ${clip.source_out.toFixed(2)}s）`"
          >
            {{ clip.name }}
          </span>
        </div>
      </div>
      <div v-if="timeline.markers.length" class="muted">
        标记：{{ timeline.markers.map((marker) => `${marker.name}@${marker.at}s`).join('、') }}
      </div>
      <div v-if="timeline.transitions.length" class="muted">
        转场：{{ timeline.transitions.map((item) => `${item.transition}@${item.at}s`).join('、') }}
      </div>
    </div>

    <div ref="logBox" class="event-list">
      <div v-if="!events.length" class="empty">暂无日志</div>
      <div v-for="event in events" :key="event.seq" class="event" :class="event.level">
        <span class="time">{{ formatTime(event.at) }}</span>
        <span>{{ event.message }}</span>
      </div>
    </div>

    <div class="row">
      <button class="btn grow" :disabled="!job" @click="emit('refresh')">刷新状态</button>
      <button
        class="btn btn-danger"
        :disabled="!job || status === 'succeeded' || status === 'failed' || status === 'cancelled'"
        @click="emit('cancel')"
      >
        取消任务
      </button>
    </div>
  </section>
</template>

<style scoped>
.timeline {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 8px;
  background: var(--panel-2);
  border-radius: 8px;
  max-height: 22vh;
  overflow: auto;
}

.track {
  display: flex;
  align-items: center;
  gap: 8px;
}

.track-name {
  color: var(--muted);
  font-size: 11px;
  width: 24px;
}

.track-body {
  display: flex;
  gap: 4px;
  flex-wrap: wrap;
}

.clip {
  background: #24384f;
  color: #9cc4ff;
  border-radius: 4px;
  padding: 2px 6px;
  font-size: 11px;
  max-width: 150px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
