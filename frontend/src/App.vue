<script setup lang="ts">
import { onMounted, ref } from 'vue'

import JobPanel from './components/JobPanel.vue'
import MediaPanel from './components/MediaPanel.vue'
import PlanPanel from './components/PlanPanel.vue'
import PromptPanel from './components/PromptPanel.vue'
import StatusBar from './components/StatusBar.vue'

import { ApiError, api } from './api/client'
import { useJobStream } from './composables/useJobStream'
import type { EditPlan, MediaLibrary, PromptPayload, SystemInfo } from './api/types'

const system = ref<SystemInfo | null>(null)
const mediaRoots = ref<string[]>([])
const library = ref<MediaLibrary | null>(null)
const selectedIds = ref<string[]>([])
const plan = ref<EditPlan | null>(null)
const planner = ref('')
const warnings = ref<string[]>([])

const scanning = ref(false)
const busy = ref(false)
const errorMessage = ref('')

const { events, job, status, connected, connect, refresh, disconnect } = useJobStream()

onMounted(async () => {
  try {
    system.value = await api.system()
    const roots = await api.mediaRoots()
    mediaRoots.value = roots.roots
  } catch (error) {
    errorMessage.value = describe(error)
  }
})

function describe(error: unknown): string {
  if (error instanceof ApiError) return `${error.message}（${error.code}）`
  return (error as Error).message ?? String(error)
}

async function onScan(path: string) {
  if (!path.trim()) return
  scanning.value = true
  errorMessage.value = ''
  try {
    const response = await api.scan(path.trim())
    library.value = response.library
    selectedIds.value = response.library.assets
      .filter((asset) => asset.kind === 'video' || asset.kind === 'audio')
      .map((asset) => asset.id)
    if (!response.ffprobe_available) {
      warnings.value = ['未检测到 ffprobe，素材时长按估算处理；安装 ffmpeg 后可获得精确时长']
    }
  } catch (error) {
    errorMessage.value = describe(error)
  } finally {
    scanning.value = false
  }
}

async function onGeneratePlan(payload: PromptPayload) {
  if (!library.value) {
    errorMessage.value = '请先扫描素材目录'
    return
  }
  busy.value = true
  errorMessage.value = ''
  try {
    const response = await api.createPlan({
      prompt: payload.prompt,
      media_root: library.value.root,
      media_ids: selectedIds.value,
      options: payload.options,
    })
    plan.value = response.plan
    planner.value = response.planner
    warnings.value = response.warnings
  } catch (error) {
    errorMessage.value = describe(error)
  } finally {
    busy.value = false
  }
}

async function submitJob(payload: Record<string, unknown>) {
  busy.value = true
  errorMessage.value = ''
  try {
    const response = await api.createJob(payload)
    connect(response.job.id)
  } catch (error) {
    errorMessage.value = describe(error)
  } finally {
    busy.value = false
  }
}

async function onRunFromPrompt(payload: PromptPayload) {
  if (!library.value) {
    errorMessage.value = '请先扫描素材目录'
    return
  }
  await submitJob({
    prompt: payload.prompt,
    media_root: library.value.root,
    media_ids: selectedIds.value,
    options: payload.options,
    auto_export: true,
  })
}

async function onRunPlan() {
  if (!plan.value || !library.value) return
  await submitJob({
    plan: plan.value,
    media_root: library.value.root,
  })
}

function clearPlan() {
  plan.value = null
  planner.value = ''
  warnings.value = []
}

async function onCancel() {
  if (!job.value) return
  try {
    await api.cancelJob(job.value.id)
  } catch (error) {
    errorMessage.value = describe(error)
  } finally {
    await refresh()
  }
}

function onRefresh() {
  void refresh()
}

function onClose() {
  disconnect()
}
</script>

<template>
  <div class="app">
    <header class="app-header">
      <div class="brand">
        <span style="font-size: 24px">🎬</span>
        <div>
          <h1>aiPr</h1>
          <p>用大模型驱动 Adobe Premiere Pro 完成剪辑 · 提示词 + 素材目录 → 方案 → 执行</p>
        </div>
      </div>
      <StatusBar :system="system" />
    </header>

    <div v-if="errorMessage" class="alert">
      {{ errorMessage }}
      <button class="btn btn-sm" style="float: right" @click="errorMessage = ''">关闭</button>
    </div>

    <main class="layout">
      <MediaPanel
        v-model:selectedIds="selectedIds"
        class="col"
        :library="library"
        :roots="mediaRoots.length ? mediaRoots : (system?.media_roots ?? [])"
        :loading="scanning"
        @scan="onScan"
      />

      <div class="col">
        <PromptPanel
          :busy="busy"
          :llm-configured="system?.llm_configured ?? false"
          @generate="onGeneratePlan"
          @run="onRunFromPrompt"
        />
        <PlanPanel
          :plan="plan"
          :planner="planner"
          :warnings="warnings"
          :busy="busy"
          @run="onRunPlan"
          @clear="clearPlan"
          @update:plan="plan = $event"
        />
      </div>

      <JobPanel
        class="col"
        :job="job"
        :events="events"
        :status="status"
        :connected="connected"
        @cancel="onCancel"
        @refresh="onRefresh"
      />
    </main>

    <footer class="muted" style="padding: 0 20px 12px">
      后端：{{ system?.llm_base_url ?? '未连接' }} · 工作区：{{ system?.workspace_dir ?? '—' }}
      <button v-if="connected" class="btn btn-sm" style="margin-left: 8px" @click="onClose">
        断开日志连接
      </button>
    </footer>
  </div>
</template>
