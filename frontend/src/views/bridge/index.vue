<template>
  <section class="page" data-module="bridge">
    <header class="page-head">
      <div>
        <h2>桥梁定检管理</h2>
        <p class="page-desc">定检结论沿「现场复核 → 签发 → 限载投影」单向推进，签发后同步检测清单、工程待办与限载页面。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="issueBatch">集中签发</button>
        <button class="btn" type="button" @click="exportRows">导出桥梁定检清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th>选择</th>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td>
            <input
              type="checkbox"
              :checked="selected.has(Number(row.id))"
              @change="toggleSelect(Number(row.id))"
            />
          </td>
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 2" class="empty-state">暂无桥梁定检数据，可先登记检测记录</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条桥梁定检记录</span>
      <span v-if="batchMessage" class="ok-text">{{ batchMessage }}</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>

const ENDPOINT = '/api/bridge'
const columns = ["检测编号", "桥梁名称", "检测类型", "检测日期", "技术状况评分", "技术状况等级", "签发状态", "评分版本", "检测状态"]
const actions = ["开始检测", "完成评定", "现场复核", "签发结论", "归档报告"]
const statuses = ["待检测", "检测中", "已评定", "已复核", "已签发", "已归档"]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const batchMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const selected = ref<Set<number>>(new Set())
// 批次号在重试间保持不变：断点续做靠它识别已完成的检测编号
const batchId = ref('')

const stats = computed(() => [
  { label: '待检测桥梁', value: rows.value.filter((row) => row['检测状态'] === '待检测').length },
  { label: '待签发桥梁', value: rows.value.filter((row) => row['签发状态'] === '未签发').length },
  { label: '已签发桥梁', value: rows.value.filter((row) => row['签发状态'] === '已签发').length },
])

function toggleSelect(id: number) {
  const next = new Set(selected.value)
  if (next.has(id)) {
    next.delete(id)
  } else {
    next.add(id)
  }
  selected.value = next
}

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  batchMessage.value = ''
  const values: Record<string, string | number> = { action }
  if (action === '现场复核') {
    const input = window.prompt(`录入 ${row['检测编号']} 的复核评分（0-100）`)
    if (input === null) return
    values['复核评分'] = input
  }
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ values }),
    })
    const payload = await response.json()
    if (!payload.ok) {
      throw new Error(payload.message || '桥梁定检动作未生效，请稍后重试')
    }
    batchMessage.value = payload.message
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '桥梁定检操作失败'
  }
}

async function issueBatch() {
  errorMessage.value = ''
  batchMessage.value = ''
  if (!selected.value.size) {
    errorMessage.value = '请先勾选要集中签发的检测记录'
    return
  }
  if (!batchId.value) {
    batchId.value = `BATCH-${Date.now()}`
  }
  try {
    const response = await request(`${ENDPOINT}/issue-batch`, {
      method: 'POST',
      body: JSON.stringify({ values: { batch_id: batchId.value, ids: [...selected.value] } }),
    })
    const payload = await response.json()
    if (!payload.ok) {
      // 保留批次号：修复断点后再次点击即断点续做，不会留下半批限载
      throw new Error(payload.message || '分组提交失败')
    }
    batchMessage.value = payload.message
    batchId.value = ''
    selected.value = new Set()
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '集中签发操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('检测记录列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '桥梁定检列表读取失败'
  }
}

onMounted(reload)
</script>
