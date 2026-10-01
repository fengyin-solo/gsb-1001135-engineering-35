<template>
  <section class="page" data-module="release">
    <header class="page-head">
      <div>
        <h2>道桥养护态势总览 · 发布管控</h2>
        <p class="page-desc">
          发布闸门按「依赖检查 → 样例回放 → 差异裁定 → 切换流量」单向推进；发布前先在隔离环境做影子验收。
          跳级发布与倒序回滚一律拒绝，任一步失败保留旧版本，绝不把总览写成零。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" type="button" @click="reload">刷新状态</button>
      </div>
    </header>

    <p v-if="errorMessage" class="error-text">{{ errorMessage }}</p>

    <!-- 当前现网版本 + 编排锁 -->
    <div class="stat-row">
      <article class="stat-card">
        <span class="stat-label">现网总览版本</span>
        <strong class="stat-value">{{ status.active_version?.version ?? '—' }}</strong>
        <span class="sub-line">{{ status.active_version?.revision }} · {{ status.active_version?.batch_id }}</span>
      </article>
      <article class="stat-card">
        <span class="stat-label">版本签名</span>
        <strong class="stat-value sig">{{ status.active_version?.signature ?? '—' }}</strong>
        <span class="sub-line">重复发布按签名幂等</span>
      </article>
      <article class="stat-card">
        <span class="stat-label">编排锁</span>
        <strong class="stat-value" :class="status.orchestration_lock?.locked ? 'lock-on' : 'lock-off'">
          {{ status.orchestration_lock?.locked ? '已锁定' : '空闲' }}
        </strong>
        <span class="sub-line">
          {{ status.orchestration_lock?.locked
            ? `持有者 ${status.orchestration_lock.holder} · ${status.orchestration_lock.operator}`
            : '同一时刻只允许一个发布者' }}
        </span>
      </article>
      <article class="stat-card">
        <span class="stat-label">台账 / 待办 / 告警</span>
        <strong class="stat-value">
          {{ status.counts?.ledger ?? 0 }} / {{ status.counts?.road_todos ?? 0 }} / {{ status.counts?.alerts ?? 0 }}
        </strong>
        <span class="sub-line">发布结论三处回写</span>
      </article>
    </div>

    <!-- 基础设施可达性 -->
    <section class="panel">
      <h3>依赖检查：队列、地图服务、缓存</h3>
      <p class="panel-desc">任一不可达时，依赖检查失败且禁止转换流量（可做故障演练）。</p>
      <div class="infra-row">
        <article v-for="item in status.infra" :key="item.name" class="infra-card"
                 :class="item.reachable ? 'ok' : 'bad'">
          <span class="infra-label">{{ item.label }}</span>
          <strong>{{ item.reachable ? '可达' : '不可达' }}</strong>
          <button class="link" type="button" @click="toggleInfra(item.name, !item.reachable)">
            {{ item.reachable ? '置为不可达' : '恢复可达' }}
          </button>
        </article>
      </div>
    </section>

    <!-- 发布前影子验收 -->
    <section class="panel">
      <h3>发布前「影子验收」（隔离环境 · 只读）</h3>
      <p class="panel-desc">
        按辖区运营约定（{{ preview?.convention.timezone }}，业务日切点
        {{ preview?.convention.business_day_cutoff }}，设备钟容忍
        {{ preview?.convention.device_clock_tolerance_minutes }} 分钟），在隔离环境重放巡查、病害、桥隧事件，
        再与现网采样逐项对照。
      </p>
      <div class="row-actions">
        <button class="btn primary" type="button" :disabled="previewLoading" @click="runPreview(false)">
          {{ previewLoading ? '隔离回放中…' : '执行影子验收' }}
        </button>
        <button class="btn" type="button" :disabled="previewLoading" @click="runPreview(true)">
          演练：注入约定未覆盖冲突
        </button>
      </div>
      <table v-if="preview" class="data-table diff-table">
        <thead>
          <tr>
            <th>事件</th><th>类型</th><th>路段</th><th>字段</th>
            <th>隔离环境</th><th>现网采样</th><th>裁决结果</th><th>辖区约定依据</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="diff in preview.report.diffs" :key="diff.event_id + diff.kind"
              :class="rowClass(diff.result)">
            <td>{{ diff.event_id }}</td>
            <td>{{ diff.kind }}</td>
            <td>{{ diff.section }}</td>
            <td>{{ diff.field }}</td>
            <td>{{ diff.shadow ?? '缺失' }}</td>
            <td>{{ diff.live ?? '缺失' }}</td>
            <td><span class="tag" :class="resultClass(diff.result)">{{ resultLabel(diff.result) }}</span></td>
            <td>{{ diff.basis }}</td>
          </tr>
        </tbody>
      </table>
      <p v-if="preview" class="verdict" :class="preview.report.verdict === '通过' ? 'pass' : 'fail'">
        影子验收结论：{{ preview.report.verdict }}
        ｜隔离 {{ preview.report.summary.shadow_count }} 项、现网 {{ preview.report.summary.live_count }} 项、
        差异 {{ preview.report.summary.diff_count }} 项（接受/和解 {{ preview.report.summary.accepted }}、
        待现网核实 {{ preview.report.summary.suspended }}、阻断 {{ preview.report.summary.blocked }}）
        ｜回放 {{ preview.replay.ingested }} 项{{ preview.replay.resumed_from ? `，断点续跑 ${preview.replay.resumed_from} 项` : '' }}
      </p>
    </section>

    <!-- 创建发布 -->
    <section class="panel">
      <h3>创建发布批次</h3>
      <form class="filter-bar create-bar" @submit.prevent="createRelease">
        <label class="filter-item">
          <span>发布者</span>
          <input v-model="operator" placeholder="发布者姓名" />
        </label>
        <label class="filter-item grow">
          <span>版本修订说明（影响版本签名）</span>
          <input v-model="revision" placeholder="例如：总览例行发布 2026-10-01" />
        </label>
        <button class="btn primary" type="submit">构建发布包并占锁</button>
      </form>
    </section>

    <!-- 活跃发布与闸门 -->
    <section v-if="active" class="panel">
      <h3>活跃发布 {{ active.batch_id }} · {{ active.version }}（{{ active.status }}）</h3>
      <ol class="gate-steps">
        <li v-for="(gate, idx) in active.gates" :key="gate.code" class="gate-step"
            :class="gateStateClass(idx, gate.state)">
          <div class="gate-head">
            <span class="gate-index">{{ idx + 1 }}</span>
            <strong>{{ gate.label }}</strong>
            <span class="tag" :class="gateTagClass(gate.state)">{{ gate.state }}</span>
          </div>
          <p class="gate-detail">{{ gate.detail || '待执行' }}</p>
          <div class="row-actions">
            <button v-if="idx === active.gate_index" class="btn primary" type="button"
                    :disabled="busy" @click="advance(gate.code, false)">
              执行{{ gate.label }}
            </button>
            <button v-if="idx === active.gate_index" class="btn" type="button"
                    :disabled="busy" @click="advance(gate.code, gate.code === 'adjudication')">
              {{ gate.code === 'adjudication' ? '演练：注入未知冲突' : '演练：注入该闸门故障' }}
            </button>
            <button v-if="gate.code === 'replay' && gate.state === '中断'" class="btn" type="button"
                    @click="resume">
              连接断开？从未完成项复位
            </button>
          </div>
        </li>
      </ol>
      <div v-if="active.replay" class="sub-panel">
        <strong>样例回放检查点：</strong>已灌入 {{ active.checkpoint.length }} 项，
        <template v-if="active.replay.resumed_from">
          其中 {{ active.replay.resumed_from }} 项来自断点续跑（不重复灌入）；
        </template>
        <template v-else>本次为完整回放；</template>
        故障可在执行前注入，断连后点「从未完成项复位」再重放。
      </div>
      <div v-if="active.shadow_report" class="sub-panel">
        <strong>影子验收结论：</strong>{{ active.shadow_report.verdict }}
        （差异 {{ active.shadow_report.summary.diff_count }}，阻断
        {{ active.shadow_report.summary.blocked }}）。结论已写入发布台账、路段待办与告警清单。
      </div>
      <p v-if="active.abandon_reason" class="error-text">本批次未上线：{{ active.abandon_reason }}，现网版本保持不变。</p>
    </section>
    <section v-else class="panel empty-panel">
      当前没有活跃发布批次；影子验收通过后可创建发布。
    </section>

    <!-- 三个回写清单 -->
    <section class="panel">
      <h3>发布台账（append-only，历史保留当时批次）</h3>
      <table class="data-table">
        <thead><tr><th>#</th><th>批次</th><th>版本</th><th>闸门</th><th>结论</th><th>时间</th><th>结果</th></tr></thead>
        <tbody>
          <tr v-for="row in ledger" :key="row.id" :class="row.ok ? '' : 'row-fail'">
            <td>{{ row.id }}</td><td>{{ row.batch_id }}</td><td>{{ row.version }}</td>
            <td>{{ row.gate }}</td><td>{{ row.conclusion }}</td><td>{{ row.recorded_at }}</td>
            <td>{{ row.ok ? '成功' : '失败/阻断' }}</td>
          </tr>
          <tr v-if="!ledger.length"><td colspan="7" class="empty-state">暂无台账记录</td></tr>
        </tbody>
      </table>
    </section>

    <section class="panel">
      <h3>路段待办</h3>
      <table class="data-table">
        <thead><tr><th>#</th><th>路段</th><th>事项</th><th>来源</th><th>批次</th><th>状态</th></tr></thead>
        <tbody>
          <tr v-for="row in roadTodos" :key="row.id">
            <td>{{ row.id }}</td><td>{{ row.section }}</td><td>{{ row.title }}</td>
            <td>{{ row.source }}</td><td>{{ row.batch_id }}</td><td>{{ row.status }}</td>
          </tr>
          <tr v-if="!roadTodos.length"><td colspan="6" class="empty-state">暂无路段待办</td></tr>
        </tbody>
      </table>
    </section>

    <section class="panel">
      <h3>告警清单</h3>
      <table class="data-table">
        <thead><tr><th>#</th><th>告警</th><th>级别</th><th>来源</th><th>批次</th><th>说明</th><th>状态</th></tr></thead>
        <tbody>
          <tr v-for="row in alerts" :key="row.id">
            <td>{{ row.id }}</td><td>{{ row.title }}</td>
            <td><span class="tag" :class="levelClass(row.level)">{{ row.level }}</span></td>
            <td>{{ row.source }}</td><td>{{ row.batch_id }}</td>
            <td>{{ row.detail }}</td><td>{{ row.status }}</td>
          </tr>
          <tr v-if="!alerts.length"><td colspan="7" class="empty-state">暂无告警</td></tr>
        </tbody>
      </table>
    </section>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'

type GateState = '待执行' | '通过' | '失败' | '中断' | '阻断' | '回退'
type Gate = { code: string; label: string; state: GateState; at: string | null; detail: string | null }
type Diff = {
  event_id: string; kind: string; section: string; field: string
  shadow: string | null; live: string | null
  result: 'accept' | 'reconcile' | 'suspend' | 'block'
  basis: string; resolved: boolean
}
type Release = {
  batch_id: string; version: string; revision: string; operator: string
  status: string; gate_index: number; next_gate: string | null
  gates: Gate[]; checkpoint: string[]
  replay: { ingested: number; resumed_from: number } | null
  shadow_report: { verdict: string; summary: Record<string, number> } | null
  abandon_reason?: string
}
type Status = {
  active_version: { version: string; revision: string; batch_id: string; signature: string }
  orchestration_lock: { locked: boolean; holder: string | null; operator: string | null }
  active_release: Release | null
  infra: { name: string; label: string; reachable: boolean }[]
  counts: { releases: number; ledger: number; road_todos: number; alerts: number }
}
type LedgerRow = {
  id: number; batch_id: string; version: string; gate: string
  conclusion: string; recorded_at: string; ok: boolean
}
type TodoRow = { id: number; section: string; title: string; source: string; batch_id: string; status: string }
type AlertRow = { id: number; title: string; level: string; source: string; batch_id: string; detail: string; status: string }
type Preview = {
  convention: { timezone: string; business_day_cutoff: string; device_clock_tolerance_minutes: number }
  replay: { ingested: number; resumed_from: number }
  report: { verdict: string; summary: Record<string, number>; diffs: Diff[] }
}

const status = ref<Status>({
  active_version: { version: '', revision: '', batch_id: '', signature: '' },
  orchestration_lock: { locked: false, holder: null, operator: null },
  active_release: null,
  infra: [],
  counts: { releases: 0, ledger: 0, road_todos: 0, alerts: 0 },
})
const active = ref<Release | null>(null)
const ledger = ref<LedgerRow[]>([])
const roadTodos = ref<TodoRow[]>([])
const alerts = ref<AlertRow[]>([])
const preview = ref<Preview | null>(null)
const errorMessage = ref('')
const busy = ref(false)
const previewLoading = ref(false)
const operator = ref('值班员')
const revision = ref('总览例行发布')

async function call<T>(path: string, init?: RequestInit, allowError = false): Promise<T | null> {
  const response = await request(path, init)
  if (!response.ok) {
    const payload = await response.json().catch(() => ({ detail: '请求失败' }))
    const detail = (payload as { detail?: string }).detail ?? '请求失败'
    if (allowError) {
      errorMessage.value = detail
      return null
    }
    throw new Error(detail)
  }
  return (await response.json()) as T
}

async function reload() {
  errorMessage.value = ''
  try {
    const [s, l, t, a] = await Promise.all([
      call<Status>('/api/release/status'),
      call<{ items: LedgerRow[] }>('/api/release/ledger'),
      call<{ items: TodoRow[] }>('/api/release/road-todos'),
      call<{ items: AlertRow[] }>('/api/release/alerts'),
    ])
    if (s) status.value = s
    active.value = s?.active_release ?? null
    ledger.value = l?.items.slice().reverse() ?? []
    roadTodos.value = t?.items.slice().reverse() ?? []
    alerts.value = a?.items.slice().reverse() ?? []
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '发布状态读取失败'
  }
}

async function runPreview(unknown: boolean) {
  previewLoading.value = true
  errorMessage.value = ''
  try {
    preview.value = await call<Preview>('/api/release/shadow-preview', {
      method: 'POST',
      body: JSON.stringify({ operator: operator.value || '值班员', unknown_conflict: unknown }),
    })
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '影子验收失败'
  } finally {
    previewLoading.value = false
  }
}

async function createRelease() {
  busy.value = true
  errorMessage.value = ''
  try {
    const result = await call<Release & { idempotent?: boolean; message?: string }>(
      '/api/release/releases',
      { method: 'POST', body: JSON.stringify({ operator: operator.value || '值班员', revision: revision.value }) },
    )
    if (result?.message) errorMessage.value = result.message
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '创建发布失败'
  } finally {
    busy.value = false
  }
}

async function maybeInject(batchId: string, gate: string, inject: boolean) {
  if (!inject) {
    // 差异裁定闸门的“演练”才注入；其他闸门通过注入接口在执行前布故障
    return
  }
  if (gate !== 'adjudication') {
    await call(`/api/release/releases/${batchId}/inject`, {
      method: 'POST',
      body: JSON.stringify({ stage: gate === 'replay' ? 'replay' : gate }),
    })
  }
}

async function advance(gate: string, unknown: boolean) {
  if (!active.value) return
  busy.value = true
  errorMessage.value = ''
  const batchId = active.value.batch_id
  try {
    await maybeInject(batchId, gate, unknown)
    await call(`/api/release/releases/${batchId}/gates/${gate}`, {
      method: 'POST',
      body: JSON.stringify({ operator: operator.value || '值班员', unknown_conflict: unknown }),
    }, true)
  } finally {
    busy.value = false
    await reload()
  }
}

async function resume() {
  if (!active.value) return
  errorMessage.value = ''
  const info = await call<{ completed: number; message: string }>(
    `/api/release/releases/${active.value.batch_id}/resume`,
    { method: 'POST' },
  )
  if (info) errorMessage.value = `${info.message}（已完成 ${info.completed} 项），请再次执行样例回放续跑`
  await reload()
}

async function toggleInfra(name: string, reachable: boolean) {
  errorMessage.value = ''
  await call(`/api/release/infra/${name}`, {
    method: 'POST',
    body: JSON.stringify({ reachable }),
  })
  await reload()
}

function gateStateClass(idx: number, state: GateState) {
  if (state === '通过') return 'step-done'
  if (state === '失败' || state === '阻断' || state === '回退') return 'step-fail'
  if (state === '中断') return 'step-pause'
  return idx === active.value?.gate_index ? 'step-current' : 'step-todo'
}
function gateTagClass(state: GateState) {
  if (state === '通过') return 'tag-green'
  if (state === '失败' || state === '阻断' || state === '回退') return 'tag-red'
  if (state === '中断') return 'tag-amber'
  return 'tag-gray'
}
function resultLabel(result: Diff['result']) {
  return { accept: '接受', reconcile: '以现网为准并告警', suspend: '待现网核实', block: '阻断' }[result]
}
function resultClass(result: Diff['result']) {
  if (result === 'block') return 'tag-red'
  if (result === 'suspend') return 'tag-amber'
  if (result === 'reconcile') return 'tag-blue'
  return 'tag-green'
}
function rowClass(result: Diff['result']) {
  return result === 'block' ? 'row-fail' : ''
}
function levelClass(level: string) {
  return level === '高' ? 'tag-red' : level === '中' ? 'tag-amber' : 'tag-green'
}

onMounted(reload)
</script>

<style scoped>
.panel { background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 12px 14px; margin-bottom: 14px; }
.panel h3 { margin: 0 0 6px; font-size: 15px; }
.panel-desc { color: var(--muted); font-size: 12px; margin: 0 0 10px; }
.sub-line { display: block; color: var(--muted); font-size: 12px; margin-top: 2px; }
.sig { font-size: 14px; font-family: ui-monospace, monospace; }
.lock-on { color: #b42318; }
.lock-off { color: #047857; }
.infra-row { display: flex; gap: 10px; }
.infra-card { flex: 1; border: 1px solid var(--border); border-radius: 8px; padding: 10px; display: flex; flex-direction: column; gap: 6px; }
.infra-card.ok { border-color: #97d3af; background: #f1fbf5; }
.infra-card.bad { border-color: #f0a7a0; background: #fef3f2; }
.infra-label { color: var(--muted); font-size: 12px; }
.create-bar { margin-bottom: 0; }
.grow { flex: 1; }
.gate-steps { list-style: none; padding: 0; margin: 0; display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; }
.gate-step { border: 1px solid var(--border); border-radius: 8px; padding: 10px; }
.step-current { border-color: var(--brand); box-shadow: 0 0 0 2px rgba(31, 111, 235, 0.12); }
.step-done { border-color: #97d3af; background: #f6fef9; }
.step-fail { border-color: #f0a7a0; background: #fef3f2; }
.step-pause { border-color: #e6c06a; background: #fffaeb; }
.gate-head { display: flex; align-items: center; gap: 6px; }
.gate-index { width: 20px; height: 20px; border-radius: 50%; background: #e8eef6; color: #334155; display: inline-flex; align-items: center; justify-content: center; font-size: 12px; }
.step-done .gate-index { background: #047857; color: #fff; }
.step-fail .gate-index { background: #b42318; color: #fff; }
.gate-detail { font-size: 12px; color: var(--muted); min-height: 34px; margin: 6px 0; }
.tag { display: inline-block; padding: 1px 8px; border-radius: 10px; font-size: 12px; }
.tag-green { background: #d1fadf; color: #047857; }
.tag-red { background: #fee4e2; color: #b42318; }
.tag-amber { background: #fef0c7; color: #b54708; }
.tag-blue { background: #dbeafe; color: #1d4ed8; }
.tag-gray { background: #eef2f6; color: #475569; }
.sub-panel { margin-top: 10px; background: #f8fafc; border: 1px dashed var(--border); border-radius: 6px; padding: 8px 10px; font-size: 13px; }
.diff-table td, .diff-table th { font-size: 12px; }
.verdict { margin: 8px 0 0; font-size: 13px; }
.verdict.pass { color: #047857; }
.verdict.fail { color: #b42318; }
.row-fail { background: #fef3f2; }
.empty-panel { color: var(--muted); font-size: 13px; }
</style>
