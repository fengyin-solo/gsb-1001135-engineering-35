<template>
  <section class="page">
    <header class="page-head">
      <div>
        <h2>运营概览</h2>
        <p class="page-desc">汇总各业务模块的关键指标，先看总量再看异常。</p>
      </div>
      <div v-if="release" class="release-badge">
        生效版本：{{ release.batch_no }} · {{ release.version_signature }}
      </div>
    </header>
    <div class="stat-row">
      <article v-for="card in cards" :key="card.label" class="stat-card">
        <span class="stat-label">{{ card.label }}</span>
        <strong class="stat-value">{{ card.value }}</strong>
      </article>
    </div>
    <table class="data-table">
      <thead>
        <tr><th>业务模块</th><th>今日新增</th><th>待处理</th><th>异常量</th></tr>
      </thead>
      <tbody>
        <tr v-for="row in moduleRows" :key="row.name">
          <td>{{ row.name }}</td>
          <td>{{ row.created }}</td>
          <td>{{ row.pending }}</td>
          <td>{{ row.abnormal }}</td>
        </tr>
      </tbody>
    </table>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { fetchJson } from '@/api/client'

type Overview = {
  cards: { label: string; value: number }[]
  modules: { name: string; created: number; pending: number; abnormal: number }[]
  release?: { batch_no: string; version_signature: string; package_signature: string; jurisdiction_code: string | null }
}

const cards = ref<Overview['cards']>([])
const moduleRows = ref<Overview['modules']>([])
const release = ref<Overview['release'] | null>(null)

onMounted(async () => {
  try {
    const payload = await fetchJson<Overview>('/api/overview')
    cards.value = payload.cards
    moduleRows.value = payload.modules
    release.value = payload.release ?? null
  } catch {
    cards.value = [{"label": "业务模块", "value": 0}, {"label": "今日新增", "value": 0}]
    moduleRows.value = [{"name": "路段管理", "created": 0, "pending": 0, "abnormal": 0}, {"name": "日常巡查", "created": 0, "pending": 0, "abnormal": 0}, {"name": "路面病害", "created": 0, "pending": 0, "abnormal": 0}, {"name": "桥梁定检", "created": 0, "pending": 0, "abnormal": 0}, {"name": "桥梁档案", "created": 0, "pending": 0, "abnormal": 0}, {"name": "隧道管养", "created": 0, "pending": 0, "abnormal": 0}, {"name": "交安设施", "created": 0, "pending": 0, "abnormal": 0}, {"name": "排水设施", "created": 0, "pending": 0, "abnormal": 0}, {"name": "绿化管养", "created": 0, "pending": 0, "abnormal": 0}, {"name": "路灯照明", "created": 0, "pending": 0, "abnormal": 0}, {"name": "除雪防滑", "created": 0, "pending": 0, "abnormal": 0}, {"name": "防汛应急", "created": 0, "pending": 0, "abnormal": 0}, {"name": "边坡防护", "created": 0, "pending": 0, "abnormal": 0}, {"name": "伸缩缝管理", "created": 0, "pending": 0, "abnormal": 0}, {"name": "支座维护", "created": 0, "pending": 0, "abnormal": 0}, {"name": "养护工程", "created": 0, "pending": 0, "abnormal": 0}, {"name": "养护车辆", "created": 0, "pending": 0, "abnormal": 0}, {"name": "养护材料", "created": 0, "pending": 0, "abnormal": 0}]
  }
})
</script>
