<template>
  <div class="page" v-if="run">
    <div style="display: flex; justify-content: space-between; gap: 12px; flex-wrap: wrap">
      <div>
        <h1 style="margin-bottom: 4px">{{ run.name }}</h1>
        <p class="muted" style="margin-top: 0">
          {{ run.project }} ·
          <n-tag size="small" :type="statusType">{{ statusLabel }}</n-tag>
          · version {{ run.version }}
        </p>
      </div>
      <div style="display: flex; gap: 8px">
        <n-button @click="$router.push(`/runs/${run.id}/events`)">事件时间线</n-button>
        <n-button @click="$router.push(`/runs/${run.id}/lineage`)">血缘</n-button>
      </div>
    </div>

    <div class="card" style="margin-bottom: 16px">
      <div class="grid-2">
        <div>
          <div class="muted">dataset_content_sha256</div>
          <div class="mono">{{ run.dataset_content_sha256 }}</div>
        </div>
        <div>
          <div class="muted">code_commit_sha</div>
          <div class="mono">{{ run.code_commit_sha }}</div>
        </div>
        <div>
          <div class="muted">started_by / started_at</div>
          <div>{{ run.started_by }} · {{ formatTime(run.started_at) }}</div>
        </div>
        <div>
          <div class="muted">finished_at</div>
          <div>{{ run.finished_at ? formatTime(run.finished_at) : '—' }}</div>
        </div>
      </div>
      <p v-if="run.description" style="margin-top: 12px">{{ run.description }}</p>
      <p v-if="run.result_summary"><strong>结果：</strong>{{ run.result_summary }}</p>
      <p v-if="run.abort_reason"><strong>中止原因：</strong>{{ run.abort_reason }}</p>
    </div>

    <div class="grid-2" style="margin-bottom: 16px">
      <div class="card">
        <div style="display: flex; justify-content: space-between; align-items: center; gap: 8px; flex-wrap: wrap">
          <h3 style="margin: 0">指标（投影）</h3>
          <n-tag size="small" type="warning">
            重名策略：{{ duplicatePolicy === 'reject' ? '拒绝第二次（reject，不覆盖）' : duplicatePolicy || '加载中…' }}
          </n-tag>
        </div>
        <n-data-table
          size="small"
          :columns="metricCols"
          :data="run.metrics_json || []"
          :bordered="false"
        />
        <p class="muted" style="margin-bottom: 0; font-size: 12px">
          同一 Run 内指标名唯一：第二次提交同名指标会被拒绝，下方指标区保留首次值，不会被新值覆盖；
          每次提交（含被拒绝的尝试）以服务端返回为准。
        </p>
      </div>
      <div class="card">
        <h3 style="margin-top: 0">产物（投影）</h3>
        <n-data-table
          size="small"
          :columns="artifactCols"
          :data="run.artifacts_json || []"
          :bordered="false"
        />
      </div>
    </div>

    <div v-if="canWrite" class="card">
      <h3 style="margin-top: 0">命令操作区（乐观锁 expected_version = {{ run.version }}）</h3>
      <div class="grid-2">
        <div>
          <h4>RecordMetric</h4>
          <n-input v-model:value="metric.name" placeholder="指标名" style="margin-bottom: 8px" />
          <p
            v-if="metricNameDuplicate"
            class="mono"
            style="color: #b45309; font-size: 12px; margin: -4px 0 8px"
          >
            ⚠ 指标名「{{ metric.name.trim() }}」已存在，提交将按 reject 策略被拒绝（不会覆盖原值）
          </p>
          <n-input-number v-model:value="metric.value" style="width: 100%; margin-bottom: 8px" />
          <n-input-number v-model:value="metric.step" :min="0" style="width: 100%; margin-bottom: 8px" />
          <n-button type="primary" :loading="busy" @click="doMetric">记录指标</n-button>
        </div>
        <div>
          <h4>AttachArtifact</h4>
          <n-input v-model:value="artifact.name" placeholder="产物名" style="margin-bottom: 8px" />
          <n-input v-model:value="artifact.uri" placeholder="URI" style="margin-bottom: 8px" />
          <n-input v-model:value="artifact.content_sha256" placeholder="content sha256" class="mono" style="margin-bottom: 8px" />
          <n-button text type="primary" @click="artifact.content_sha256 = randomHex(32)">随机指纹</n-button>
          <div style="margin-top: 8px">
            <n-button type="primary" :loading="busy" @click="doArtifact">挂载产物</n-button>
          </div>
        </div>
      </div>
      <div style="margin-top: 20px; display: flex; gap: 12px; flex-wrap: wrap; align-items: flex-end">
        <div style="flex: 1; min-width: 220px">
          <n-input v-model:value="completeSummary" type="textarea" placeholder="完成摘要" :rows="2" />
        </div>
        <n-button type="success" :loading="busy" @click="doComplete">CompleteRun</n-button>
        <div style="flex: 1; min-width: 220px">
          <n-input v-model:value="abortReason" type="textarea" placeholder="中止原因" :rows="2" />
        </div>
        <n-button type="warning" :loading="busy" @click="doAbort">AbortRun</n-button>
      </div>
    </div>
    <div v-else class="card muted">审计员只读：可查看事件与血缘，不可发送命令。</div>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute } from 'vue-router'
import { useMessage, useDialog } from 'naive-ui'
import {
  abortRun,
  attachArtifact,
  completeRun,
  getMetricPolicy,
  getRun,
  recordMetric,
} from '../api/client'
import { useAuthStore } from '../stores/auth'

const route = useRoute()
const auth = useAuthStore()
const message = useMessage()
const dialog = useDialog()
const run = ref(null)
const busy = ref(false)
const completeSummary = ref('')
const abortReason = ref('')
// 与后端写死的策略保持一致（reject）；挂载时再从 /policies/metrics 拉取确认
const duplicatePolicy = ref('reject')

const metric = reactive({ name: 'loss', value: 0.5, step: 1 })
const artifact = reactive({
  name: 'checkpoint.pt',
  uri: 's3://lab-artifacts/checkpoint.pt',
  content_sha256: '',
  media_type: 'application/octet-stream',
})

const canWrite = computed(() => auth.role === 'researcher' && run.value?.status === 'running')
const statusLabel = computed(() => {
  const m = { running: '进行中', completed: '已完成', aborted: '已中止' }
  return m[run.value?.status] || run.value?.status
})
const statusType = computed(() => {
  const m = { running: 'info', completed: 'success', aborted: 'warning' }
  return m[run.value?.status] || 'default'
})

const metricCols = [
  { title: 'name', key: 'name' },
  { title: 'value', key: 'value' },
  { title: 'step', key: 'step' },
]
const artifactCols = [
  { title: 'name', key: 'name' },
  { title: 'uri', key: 'uri', ellipsis: { tooltip: true } },
]

// 当前 Run 已记录成功的指标名（来自投影，与事件流一致）
const existingMetricNames = computed(
  () => new Set((run.value?.metrics_json || []).map((m) => m.name)),
)
// 输入框中的指标名是否与本 Run 已有指标重名（即时提示）
const metricNameDuplicate = computed(() => {
  const name = metric.name?.trim()
  return (
    !!name &&
    duplicatePolicy.value === 'reject' &&
    existingMetricNames.value.has(name)
  )
})

function formatTime(v) {
  return v ? new Date(v).toLocaleString() : '—'
}

function randomHex(n) {
  const bytes = new Uint8Array(n)
  crypto.getRandomValues(bytes)
  return Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('')
}

async function load() {
  run.value = await getRun(route.params.id)
}

async function withBusy(fn) {
  busy.value = true
  try {
    await fn()
    message.success('命令已接受')
    await load()
  } catch (e) {
    const status = e.response?.status
    if (status === 409) {
      // 版本冲突 / 终态 / 重名拒绝：给出明确拒绝说明
      dialog.error({
        title: '命令被拒绝（409）',
        content: e.message || '版本冲突或当前状态不允许该命令',
        positiveText: '知道了',
      })
    } else {
      message.error(e.message || '命令失败')
    }
  } finally {
    busy.value = false
  }
}

function doMetric() {
  const name = metric.name?.trim()
  if (!name) {
    message.warning('请填写指标名')
    return
  }
  // 提交前本地预检：与后端 reject 策略一致，重名直接拦下并说明
  if (duplicatePolicy.value === 'reject' && existingMetricNames.value.has(name)) {
    dialog.warning({
      title: '指标名重复，已拒绝提交',
      content:
        `指标名「${name}」在本 Run 已存在。当前重名策略为 reject：` +
        '第二次记录会被拒绝，现有值不会被新值覆盖。请改用新的指标名。',
      positiveText: '知道了',
    })
    return
  }
  return withBusy(async () => {
    try {
      await recordMetric(run.value.id, {
        name,
        value: metric.value,
        step: metric.step,
        expected_version: run.value.version,
      })
      metric.step += 1
    } catch (e) {
      // 并发下本地投影可能滞后：刷新后把服务端拒绝原因交给 withBusy 弹窗
      await load()
      throw e
    }
  })
}

function doArtifact() {
  if (!artifact.content_sha256 || artifact.content_sha256.length !== 64) {
    message.warning('请填写 64 位 content_sha256')
    return
  }
  return withBusy(() =>
    attachArtifact(run.value.id, {
      ...artifact,
      expected_version: run.value.version,
    }),
  )
}

function doComplete() {
  if (!completeSummary.value.trim()) {
    message.warning('请填写完成摘要')
    return
  }
  return withBusy(() =>
    completeRun(run.value.id, {
      result_summary: completeSummary.value,
      expected_version: run.value.version,
    }),
  )
}

function doAbort() {
  if (!abortReason.value.trim()) {
    message.warning('请填写中止原因')
    return
  }
  return withBusy(() =>
    abortRun(run.value.id, {
      reason: abortReason.value,
      expected_version: run.value.version,
    }),
  )
}

onMounted(async () => {
  try {
    const policy = await getMetricPolicy()
    duplicatePolicy.value = policy.duplicate_name
  } catch (e) {
    // 策略接口不可用时沿用默认 reject，以后端 409 为最终依据
    duplicatePolicy.value = 'reject'
  }
  try {
    await load()
  } catch (e) {
    message.error(e.message || '加载失败')
  }
})
</script>
