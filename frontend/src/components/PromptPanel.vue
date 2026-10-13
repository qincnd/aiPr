<script setup lang="ts">
import { reactive, ref } from 'vue'
import type { PromptPayload } from '../api/types'

defineProps<{ busy: boolean; llmConfigured: boolean }>()

const emit = defineEmits<{
  generate: [payload: PromptPayload]
  run: [payload: PromptPayload]
}>()

const prompt = ref('把素材剪成一个 30 秒的短片，镜头之间加叠化过渡，节奏要快，配一段背景音乐，最后导出成片。')

const form = reactive({
  useLlm: true,
  targetDuration: 30,
  maxClips: 12,
  width: 1920,
  height: 1080,
  fps: 30,
  preset: '',
  extra: '',
})

const presets = [
  { label: '1920×1080 30fps', width: 1920, height: 1080, fps: 30 },
  { label: '1920×1080 25fps', width: 1920, height: 1080, fps: 25 },
  { label: '3840×2160 30fps', width: 3840, height: 2160, fps: 30 },
  { label: '1080×1920 30fps（竖屏）', width: 1080, height: 1920, fps: 30 },
  { label: '1280×720 30fps', width: 1280, height: 720, fps: 30 },
]

function applyPreset(event: Event) {
  const value = (event.target as HTMLSelectElement).value
  const preset = presets.find((item) => item.label === value)
  if (!preset) return
  form.width = preset.width
  form.height = preset.height
  form.fps = preset.fps
}

function buildPayload(): PromptPayload {
  return {
    prompt: prompt.value.trim(),
    options: {
      use_llm: form.useLlm,
      target_duration: form.targetDuration || null,
      max_clips: form.maxClips,
      width: form.width,
      height: form.height,
      fps: form.fps,
      preset: form.preset.trim() || null,
      extra_instructions: form.extra.trim(),
    },
  }
}
</script>

<template>
  <section class="panel">
    <div class="panel-title">
      <span>② 提示词</span>
      <span class="hint">大模型会把它翻译成剪辑方案</span>
    </div>

    <textarea v-model="prompt" placeholder="用自然语言描述你想要的成片，例如：把素材剪成 30 秒，快节奏，加字幕占位和叠化过渡" />

    <div class="row">
      <label class="checkbox">
        <input v-model="form.useLlm" type="checkbox" />
        使用大模型生成方案
      </label>
      <span v-if="!llmConfigured" class="muted">（未配置 API Key，将使用内置启发式排片）</span>
    </div>

    <div class="row">
      <div class="field grow">
        <label>目标时长（秒）</label>
        <input v-model.number="form.targetDuration" type="number" min="1" max="3600" />
      </div>
      <div class="field grow">
        <label>最多片段数</label>
        <input v-model.number="form.maxClips" type="number" min="1" max="200" />
      </div>
    </div>

    <div class="field">
      <label>序列规格</label>
      <select :value="`${form.width}×${form.height} ${form.fps}fps`" @change="applyPreset">
        <option v-for="preset in presets" :key="preset.label" :value="preset.label">
          {{ preset.label }}
        </option>
      </select>
    </div>

    <div class="field">
      <label>额外要求（可选）</label>
      <input v-model="form.extra" type="text" placeholder="例如：不要用到 shot_c，开头留 1 秒黑场" />
    </div>

    <div class="row">
      <button class="btn grow" :disabled="busy || !prompt.trim()" @click="emit('generate', buildPayload())">
        生成方案
      </button>
      <button
        class="btn btn-primary grow"
        :disabled="busy || !prompt.trim()"
        @click="emit('run', buildPayload())"
      >
        生成并执行
      </button>
    </div>
  </section>
</template>
