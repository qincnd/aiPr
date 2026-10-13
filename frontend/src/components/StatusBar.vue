<script setup lang="ts">
import { computed } from 'vue'
import type { SystemInfo } from '../api/types'

const props = defineProps<{ system: SystemInfo | null }>()

const premiereState = computed(() => {
  const info = props.system?.premiere
  if (!info) return { cls: '', text: '后端未连接' }
  if (!info.available) return { cls: 'err', text: `${info.backend} 不可用` }
  if (!info.connected) return { cls: 'warn', text: `${info.backend} 待连接` }
  return { cls: 'ok', text: `${info.backend} 已就绪` }
})

const llmState = computed(() => {
  if (!props.system) return { cls: '', text: '大模型未检测' }
  if (!props.system.llm_enabled) return { cls: 'warn', text: '大模型已禁用' }
  if (!props.system.llm_configured) return { cls: 'warn', text: '未配置 API Key' }
  return { cls: 'ok', text: props.system.llm_model }
})
</script>

<template>
  <div class="status-bar">
    <span class="chip" :title="system?.premiere.message">
      <i class="dot" :class="premiereState.cls"></i>Premiere：{{ premiereState.text }}
    </span>
    <span class="chip" :title="system?.llm_base_url">
      <i class="dot" :class="llmState.cls"></i>大模型：{{ llmState.text }}
    </span>
    <span class="chip">
      <i class="dot" :class="system?.ffprobe_available ? 'ok' : 'warn'"></i>
      ffprobe：{{ system?.ffprobe_available ? '可用' : '缺失（时长按估算）' }}
    </span>
    <span v-if="system" class="chip">Python {{ system.python_version.split(' ')[0] }}</span>
  </div>
</template>

<style scoped>
.status-bar {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  justify-content: flex-end;
}
</style>
