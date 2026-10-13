<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import type { EditPlan, Operation } from '../api/types'

const props = defineProps<{
  plan: EditPlan | null
  planner: string
  warnings: string[]
  busy: boolean
}>()

const emit = defineEmits<{
  run: []
  clear: []
  'update:plan': [plan: EditPlan]
}>()

const editing = ref(false)
const draft = ref('')
const draftError = ref('')

watch(
  () => props.plan,
  (plan) => {
    draft.value = plan ? JSON.stringify(plan, null, 2) : ''
    draftError.value = ''
  },
  { immediate: true },
)

const operationLabels: Record<string, string> = {
  import_media: '导入素材',
  create_sequence: '新建序列',
  append_clip: '追加片段',
  insert_clip: '插入片段',
  move_clip: '移动片段',
  trim_clip: '修剪片段',
  razor: '切割',
  delete_range: '删除区间',
  apply_transition: '添加转场',
  apply_effect: '添加效果',
  set_volume: '调整音量',
  add_marker: '添加标记',
  export_sequence: '导出序列',
}

const editable = computed(() => Boolean(props.plan))

function summarize(operation: Operation): string {
  const payload = operation as Record<string, unknown>
  const parts: string[] = []
  for (const key of ['media', 'track', 'in_point', 'out_point', 'at', 'duration', 'output', 'db', 'clip']) {
    const value = payload[key]
    if (value === undefined || value === null) continue
    parts.push(`${key}=${typeof value === 'object' ? JSON.stringify(value) : String(value)}`)
  }
  return parts.join('  ')
}

function applyDraft() {
  try {
    const parsed = JSON.parse(draft.value) as EditPlan
    if (!Array.isArray(parsed.operations) || parsed.operations.length === 0) {
      throw new Error('operations 不能为空')
    }
    draftError.value = ''
    editing.value = false
    emit('update:plan', parsed)
  } catch (error) {
    draftError.value = `JSON 解析失败：${(error as Error).message}`
  }
}
</script>

<template>
  <section class="panel">
    <div class="panel-title">
      <span>③ 剪辑方案</span>
      <div class="row">
        <span v-if="plan" class="chip">{{ planner === 'llm' ? '大模型生成' : '启发式生成' }}</span>
        <button v-if="editable" class="btn btn-sm" @click="editing = !editing">
          {{ editing ? '返回预览' : '编辑 JSON' }}
        </button>
      </div>
    </div>

    <div v-if="!plan" class="empty">还没有方案，先在左侧写好提示词并点击「生成方案」</div>

    <template v-else>
      <p class="muted">
        工程 <b>{{ plan.project_name }}</b> · 序列 {{ plan.sequence.name }}
        （{{ plan.sequence.width }}×{{ plan.sequence.height }} @ {{ plan.sequence.fps }}fps）·
        共 {{ plan.operations.length }} 条操作
      </p>
      <p v-if="plan.summary" class="muted">{{ plan.summary }}</p>

      <ul v-if="warnings.length" class="warn-list">
        <li v-for="(warning, index) in warnings" :key="index">{{ warning }}</li>
      </ul>

      <template v-if="editing">
        <textarea v-model="draft" class="json-editor" spellcheck="false" />
        <p v-if="draftError" class="muted" style="color: var(--err)">{{ draftError }}</p>
        <div class="row">
          <button class="btn btn-primary" @click="applyDraft">应用修改</button>
          <button class="btn" @click="editing = false">取消</button>
        </div>
      </template>

      <div v-else class="op-list">
        <div v-for="(operation, index) in plan.operations" :key="index" class="op">
          <span class="op-index">{{ index + 1 }}</span>
          <div class="op-body">
            <div class="op-name">
              {{ operationLabels[operation.op] ?? operation.op }}
              <code class="mono muted">{{ operation.op }}</code>
            </div>
            <div v-if="operation.note" class="op-note">{{ operation.note }}</div>
            <div class="op-params">{{ summarize(operation) }}</div>
          </div>
        </div>
      </div>

      <div class="row">
        <button class="btn btn-primary grow" :disabled="busy" @click="emit('run')">
          在 Premiere 中执行
        </button>
        <button class="btn" :disabled="busy" @click="emit('clear')">清空</button>
      </div>
    </template>
  </section>
</template>

<style scoped>
.json-editor {
  min-height: 340px;
  font-family: Consolas, "Cascadia Mono", monospace;
  font-size: 12px;
}
</style>
