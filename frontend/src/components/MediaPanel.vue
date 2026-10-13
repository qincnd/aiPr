<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import type { MediaAsset, MediaLibrary } from '../api/types'

const props = defineProps<{
  library: MediaLibrary | null
  roots: string[]
  loading: boolean
}>()

const emit = defineEmits<{ scan: [path: string] }>()

const selectedIds = defineModel<string[]>('selectedIds', { required: true })

const rootPath = ref(props.roots[0] ?? '')
const filter = ref<'all' | 'video' | 'audio' | 'image'>('all')

// 素材根目录是异步加载的，拿到后填入默认值
watch(
  () => props.roots,
  (roots) => {
    if (!rootPath.value && roots.length) {
      rootPath.value = roots[0]
    }
  },
  { immediate: true },
)

const assets = computed<MediaAsset[]>(() => {
  const list = props.library?.assets ?? []
  return filter.value === 'all' ? list : list.filter((item) => item.kind === filter.value)
})

const selected = computed(() => new Set(selectedIds.value))

function toggle(asset: MediaAsset) {
  const next = new Set(selectedIds.value)
  if (next.has(asset.id)) {
    next.delete(asset.id)
  } else {
    next.add(asset.id)
  }
  selectedIds.value = [...next]
}

function selectAll() {
  const usable = assets.value.filter((item) => item.kind === 'video' || item.kind === 'audio')
  selectedIds.value = usable.map((item) => item.id)
}

function clearSelection() {
  selectedIds.value = []
}

function formatDuration(seconds: number | null): string {
  if (seconds === null || seconds === undefined) return '时长未知'
  const minutes = Math.floor(seconds / 60)
  const rest = (seconds % 60).toFixed(1)
  return minutes > 0 ? `${minutes}分${rest}秒` : `${rest}秒`
}

function formatSize(bytes: number): string {
  if (bytes > 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`
  if (bytes > 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${bytes} B`
}

function resolution(asset: MediaAsset): string {
  if (asset.width && asset.height) return `${asset.width}×${asset.height}`
  if (asset.sample_rate) return `${asset.sample_rate} Hz`
  return '—'
}
</script>

<template>
  <section class="panel">
    <div class="panel-title">
      <span>① 素材</span>
      <span class="hint">{{ library ? `${library.assets.length} 个文件` : '未扫描' }}</span>
    </div>

    <div class="field">
      <label>素材目录</label>
      <input v-model="rootPath" type="text" placeholder="例如 E:\project\media" @keyup.enter="emit('scan', rootPath)" />
    </div>

    <div class="row">
      <button class="btn btn-primary grow" :disabled="loading || !rootPath" @click="emit('scan', rootPath)">
        {{ loading ? '扫描中…' : '扫描素材' }}
      </button>
      <button class="btn btn-sm" :disabled="!assets.length" @click="selectAll">全选</button>
      <button class="btn btn-sm" :disabled="!selectedIds.length" @click="clearSelection">清空</button>
    </div>

    <div class="row">
      <select v-model="filter" class="grow">
        <option value="all">全部类型</option>
        <option value="video">仅视频</option>
        <option value="audio">仅音频</option>
        <option value="image">仅图片</option>
      </select>
      <span class="chip">已选 {{ selectedIds.length }}</span>
    </div>
    <!-- selectedIds 通过 defineModel 双向绑定 -->

    <p v-if="roots.length" class="muted">允许扫描的根目录：{{ roots.join('、') }}</p>

    <div v-if="!library" class="empty">输入素材目录后点击「扫描素材」</div>
    <div v-else-if="!assets.length" class="empty">该目录下没有可用的媒体文件</div>

    <div v-else class="asset-list">
      <label
        v-for="asset in assets"
        :key="asset.id"
        class="asset"
        :class="{ selected: selected.has(asset.id) }"
      >
        <input
          type="checkbox"
          :checked="selected.has(asset.id)"
          @change="toggle(asset)"
        />
        <div class="grow">
          <div class="asset-name">
            {{ asset.name }}
            <span class="badge" :class="asset.kind">{{ asset.kind }}</span>
          </div>
          <div class="asset-meta">
            {{ formatDuration(asset.duration_seconds) }} · {{ resolution(asset) }} ·
            {{ formatSize(asset.size_bytes) }}
            <template v-if="asset.probe_source === 'metadata-only'"> · 未探测</template>
          </div>
        </div>
      </label>
    </div>

    <ul v-if="library?.skipped.length" class="warn-list">
      <li v-for="(item, index) in library.skipped" :key="index">{{ item }}</li>
    </ul>
  </section>
</template>
