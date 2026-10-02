<template>
  <section class="page" data-module="release">
    <header class="page-head">
      <div>
        <h2>道桥养护态势总览 · 发布管控</h2>
        <p class="page-desc">
          发布闸门按「依赖检查 → 影子验收 → 差异裁定 → 切换流量」单向推进，
          跳级与倒序回滚一律拒绝；任一步失败保留旧版本，总览不会写成零。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="createBatch">创建发布批次</button>
        <button class="btn" type="button" @click="reloadAll">刷新</button>
      </div>
    </header>

    <div class="stat-row">
      <article class="stat-card">
        <span class="stat-label">当前生效版本</span>
        <strong class="stat-value">{{ state.active?.batch_no ?? '—' }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">版本签名</span>
        <strong class="stat-value" style="font-size: 13px">{{ state.active?.version_signature ?? '—' }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">辖区约定</span>
        <strong class="stat-value" style="font-size: 14px">{{ state.active?.jurisdiction_code ?? '默认辖区' }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">发布批次总数</span>
        <strong class="stat-value">{{ state.sessions ?? 0 }}</strong>
      </article>
    </div>

    <div class="gate-panel">
      <h3>外部依赖连通性（队列、地图服务或缓存不可达时禁止转换流量）</h3>
      <div class="dep-row">
        <span v-for="dep in state.dependencies" :key="dep.name" class="dep-pill" :class="dep.reachable ? 'ok' : 'down'">
          {{ dep.label }}：{{ dep.reachable ? '可达' : '不可达' }}
          <button class="link" type="button" @click="toggleFault(dep)">
            {{ dep.reachable ? '模拟故障' : '恢复' }}
          </button>
        </span>
        <span class="dep-pill" :class="state.lock ? 'down' : 'ok'">
          编排锁：{{ state.lock ? (`批次 ${state.lock.batch_no} 持有`) : '空闲（单发布者）' }}
        </span>
      </div>
    </div>

    <div v-if="current" class="gate-panel">
      <h3>批次 {{ current.batch_no }} · {{ statusText }}</h3>
      <p class="page-desc">
        版本签名 {{ current.version_signature }} ｜ 辖区 {{ current.jurisdiction_code }} ｜
        影子样例 {{ current.events?.length ?? 0 }} 条 ｜ 已灌入隔离环境 {{ current.replays?.length ?? 0 }} 条（复位不重灌）
      </p>
      <ol class="gate-steps">
        <li
          v-for="stage in stages"
          :key="stage.key"
          class="gate-step"
          :class="stepClass(stage.key)"
        >
          <strong>{{ stage.label }}</strong>
          <span>{{ stepText(stage.key) }}</span>
        </li>
      </ol>
      <div class="gate-actions">
        <button
          class="btn primary"
          type="button"
          :disabled="!canAdvance"
          @click="advance"
        >
          推进当前闸门：{{ currentStageLabel }}
        </button>
        <button class="btn" type="button" :disabled="!canResume" @click="resume">
          检查连接断开 · 从未完成项复位
        </button>
        <button class="btn ghost" type="button" :disabled="!canRollback" @click="rollback">
          整批回退（仅当前生效版本）
        </button>
      </div>
      <p v-if="message" class="error-text">{{ message }}</p>
    </div>

    <div class="gate-panel">
      <nav class="tab-row">
        <button
          v-for="tab in tabs"
          :key="tab.key"
          class="btn"
          :class="activeTab === tab.key ? 'primary' : ''"
          type="button"
          @click="activeTab = tab.key"
        >
          {{ tab.label }}
        </button>
      </nav>

      <table v-if="activeTab === 'ledger'" class="data-table">
        <thead>
          <tr><th>#</th><th>批次</th><th>闸门</th><th>结论</th><th>说明</th><th>约定版本</th><th>时间</th></tr>
        </thead>
        <tbody>
          <tr v-for="row in ledger" :key="row.seq">
            <td>{{ row.seq }}</td>
            <td>{{ row.batch_no }}</td>
            <td>{{ stageLabelOf(row.stage) }}</td>
            <td>{{ row.result }}</td>
            <td>{{ row.message }}</td>
            <td>{{ row.jurisdiction_code }}/{{ row.convention_version }}</td>
            <td>{{ formatTime(row.recorded_at) }}</td>
          </tr>
          <tr v-if="!ledger.length"><td colspan="7" class="empty-state">暂无发布台账</td></tr>
        </tbody>
      </table>

      <table v-if="activeTab === 'todos'" class="data-table">
        <thead>
          <tr><th>待办</th><th>路段/桥隧</th><th>事件编号</th><th>类型</th><th>裁定结论</th><th>规则</th><th>批次</th><th>状态</th></tr>
        </thead>
        <tbody>
          <tr v-for="row in todos" :key="row.id">
            <td>{{ row.title }}</td>
            <td>{{ row.road_ref }}</td>
            <td>{{ row.event_code }}</td>
            <td>{{ kindLabel(row.kind) }}</td>
            <td>{{ row.decision }}</td>
            <td>{{ row.rule_id ?? '—' }}</td>
            <td>{{ row.batch_no }}</td>
            <td>{{ row.done ? '已随回退关闭' : '待处置' }}</td>
          </tr>
          <tr v-if="!todos.length"><td colspan="8" class="empty-state">暂无路段待办，差异裁定后自动回写</td></tr>
        </tbody>
      </table>

      <table v-if="activeTab === 'alerts'" class="data-table">
        <thead>
          <tr><th>级别</th><th>来源闸门</th><th>标题</th><th>内容</th><th>批次</th><th>时间</th></tr>
        </thead>
        <tbody>
          <tr v-for="row in alerts" :key="row.id">
            <td>{{ row.level }}</td>
            <td>{{ stageLabelOf(row.source_stage) }}</td>
            <td>{{ row.title }}</td>
            <td>{{ row.content }}</td>
            <td>{{ row.batch_no }}</td>
            <td>{{ formatTime(row.created_at) }}</td>
          </tr>
          <tr v-if="!alerts.length"><td colspan="6" class="empty-state">暂无告警</td></tr>
        </tbody>
      </table>

      <table v-if="activeTab === 'sessions'" class="data-table">
        <thead>
          <tr><th>批次</th><th>状态</th><th>当前闸门</th><th>签名</th><th>锁持有</th></tr>
        </thead>
        <tbody>
          <tr
            v-for="row in sessions"
            :key="row.session_id"
            :class="current?.session_id === row.session_id ? 'selected-row' : ''"
            style="cursor: pointer"
            @click="selectSession(row.session_id)"
          >
            <td>{{ row.batch_no }}</td>
            <td>{{ row.status }}</td>
            <td>{{ row.current_stage ? stageLabelOf(row.current_stage) : '已完成' }}</td>
            <td>{{ row.version_signature }}</td>
            <td>{{ row.lock_holder ?? '—' }}</td>
          </tr>
          <tr v-if="!sessions.length"><td colspan="5" class="empty-state">暂无发布批次</td></tr>
        </tbody>
      </table>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Session = Record<string, any>
type Row = Record<string, any>

const stages = [
  { key: 'dependency_check', label: '依赖检查' },
  { key: 'shadow_acceptance', label: '影子验收' },
  { key: 'diff_adjudication', label: '差异裁定' },
  { key: 'traffic_switch', label: '切换流量' },
]
const tabs = [
  { key: 'sessions', label: '发布批次' },
  { key: 'ledger', label: '发布台账' },
  { key: 'todos', label: '路段待办' },
  { key: 'alerts', label: '告警清单' },
]

const state = ref<Row>({})
const sessions = ref<Session[]>([])
const current = ref<Session | null>(null)
const ledger = ref<Row[]>([])
const todos = ref<Row[]>([])
const alerts = ref<Row[]>([])
const activeTab = ref('sessions')
const message = ref('')

const statusText = computed(() => (current.value ? String(current.value.status) : ''))
const currentStageLabel = computed(() => {
  const stage = stages.find((item) => item.key === current.value?.current_stage)
  return stage ? stage.label : '—'
})
const canAdvance = computed(() => {
  const s = current.value
  return Boolean(s && s.current_stage && !['switched', 'rolled_back'].includes(String(s.status)))
})
const canResume = computed(() => ['failed', 'running'].includes(String(current.value?.status)))
const canRollback = computed(() => current.value?.status === 'switched')

function stageLabelOf(key: string): string {
  return stages.find((item) => item.key === key)?.label ?? key
}

function kindLabel(kind: string): string {
  return { patrol: '巡查', disease: '病害', bridge_tunnel: '桥隧事件' }[kind] ?? kind
}

function stepClass(stageKey: string): string {
  const s = current.value?.stages?.[stageKey]?.state
  if (s === 'passed') return 'passed'
  if (s === 'failed') return 'failed'
  if (s === 'running') return 'running'
  if (current.value?.current_stage === stageKey) return 'current'
  return 'pending'
}

function stepText(stageKey: string): string {
  const s = current.value?.stages?.[stageKey]
  if (s?.state === 'passed') return '通过'
  if (s?.state === 'failed') return `未通过：${(s.detail?.blocked_reasons ?? []).join('；') || s.detail?.unreachable?.join('、') || '见台账'}`
  if (s?.state === 'running') return '进行中'
  if (current.value?.current_stage === stageKey) return '待推进'
  return '未开始'
}

function formatTime(raw: string): string {
  return raw ? new Date(raw).toLocaleString('zh-CN', { hour12: false }) : '—'
}

async function callJson(path: string, init?: RequestInit): Promise<any> {
  const response = await request(path, init)
  const payload = await response.json().catch(() => ({}))
  if (!response.ok) {
    const detail = payload?.detail
    throw new Error(typeof detail === 'object' ? detail?.message ?? JSON.stringify(detail) : String(detail || response.status))
  }
  return payload
}

async function loadState() {
  state.value = await callJson('/api/release/state')
}

async function loadLists() {
  const [sessionList, ledgerList, todoList, alertList] = await Promise.all([
    callJson('/api/release/sessions'),
    callJson('/api/release/ledger'),
    callJson('/api/release/road_todos'),
    callJson('/api/release/alerts'),
  ])
  sessions.value = sessionList.items ?? []
  ledger.value = ledgerList.items ?? []
  todos.value = todoList.items ?? []
  alerts.value = alertList.items ?? []
  if (!current.value && sessions.value.length) {
    await selectSession(sessions.value[sessions.value.length - 1].session_id)
  } else if (current.value) {
    const fresh = sessions.value.find((item: Session) => item.session_id === current.value?.session_id)
    if (fresh) current.value = fresh
  }
}

async function reloadAll() {
  message.value = ''
  try {
    await Promise.all([loadState(), loadLists()])
  } catch (error) {
    message.value = error instanceof Error ? error.message : '发布管控台加载失败'
  }
}

async function selectSession(sessionId: string) {
  message.value = ''
  try {
    current.value = await callJson(`/api/release/sessions/${sessionId}`)
  } catch (error) {
    message.value = error instanceof Error ? error.message : '批次读取失败'
  }
}

async function createBatch() {
  message.value = ''
  try {
    const payload = await callJson('/api/release/sessions', {
      method: 'POST',
      body: JSON.stringify({ operator: '值班长', jurisdiction_code: 'J-EAST' }),
    })
    current.value = payload.session
    activeTab.value = 'sessions'
    await reloadAll()
    message.value = payload.created
      ? `批次 ${payload.session.batch_no} 已创建，从依赖检查开始`
      : `版本签名重复，幂等返回批次 ${payload.session.batch_no}，未重复灌入样例`
  } catch (error) {
    message.value = error instanceof Error ? error.message : '创建批次失败'
  }
}

async function advance() {
  if (!current.value) return
  try {
    const payload = await callJson(`/api/release/sessions/${current.value.session_id}/advance`, {
      method: 'POST',
      body: JSON.stringify({ operator: '值班长' }),
    })
    current.value = payload.session
    await loadLists()
    await loadState()
  } catch (error) {
    message.value = error instanceof Error ? error.message : '闸门推进被拒绝'
    await reloadAll()
  }
}

async function resume() {
  if (!current.value) return
  try {
    const payload = await callJson(`/api/release/sessions/${current.value.session_id}/resume`, {
      method: 'POST',
      body: JSON.stringify({ operator: '值班长' }),
    })
    current.value = payload.session
    message.value = '已从未完成项复位，隔离环境样例不重复灌入'
    await loadLists()
  } catch (error) {
    message.value = error instanceof Error ? error.message : '复位失败'
  }
}

async function rollback() {
  if (!current.value) return
  try {
    const payload = await callJson(`/api/release/sessions/${current.value.session_id}/rollback`, {
      method: 'POST',
      body: JSON.stringify({ operator: '值班长' }),
    })
    current.value = payload.session
    message.value = '整批回退完成，已恢复上一版本'
    await reloadAll()
  } catch (error) {
    message.value = error instanceof Error ? error.message : '整批回退被拒绝'
    await reloadAll()
  }
}

async function toggleFault(dep: Row) {
  try {
    await callJson('/api/release/dependencies/fault', {
      method: 'POST',
      body: JSON.stringify({ name: dep.name, down: dep.reachable }),
    })
    await loadState()
  } catch (error) {
    message.value = error instanceof Error ? error.message : '依赖演练失败'
  }
}

onMounted(reloadAll)
</script>

<style scoped>
.gate-panel {
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px 14px;
  margin-bottom: 14px;
}
.gate-panel h3 { margin: 0 0 10px; font-size: 15px; }
.dep-row { display: flex; flex-wrap: wrap; gap: 10px; }
.dep-pill { font-size: 13px; border-radius: 999px; padding: 4px 12px; border: 1px solid var(--border); }
.dep-pill.ok { background: #ecfdf3; color: #027a48; border-color: #abefc6; }
.dep-pill.down { background: #fef3f2; color: #b42318; border-color: #fecdca; }
.gate-steps { display: flex; gap: 8px; list-style: none; padding: 0; margin: 10px 0; }
.gate-step {
  flex: 1; border: 1px solid var(--border); border-radius: 8px; padding: 10px;
  display: flex; flex-direction: column; gap: 4px; font-size: 13px; min-height: 78px;
}
.gate-step strong { font-size: 13px; }
.gate-step span { color: var(--muted); font-size: 12px; }
.gate-step.passed { background: #ecfdf3; border-color: #abefc6; }
.gate-step.failed { background: #fef3f2; border-color: #fecdca; }
.gate-step.current { border-color: var(--brand); box-shadow: 0 0 0 2px rgba(31, 111, 235, 0.15); }
.gate-step.running { background: #eff8ff; border-color: var(--brand); }
.gate-actions { display: flex; gap: 10px; margin: 8px 0; }
.tab-row { display: flex; gap: 8px; margin-bottom: 10px; }
.selected-row { background: #eff8ff; }
</style>
