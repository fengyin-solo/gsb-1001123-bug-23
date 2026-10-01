<template>
  <section class="page" data-module="bridge">
    <header class="page-head">
      <div>
        <h2>桥梁定检管理</h2>
        <p class="page-desc">集中签发链：现场复核 → 签发 → 限载投影，签发结论同步检测清单、工程待办和限载页面。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="issueBatch">集中签发（已复核）</button>
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
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td class="row-actions">
            <button class="link" type="button" @click="reviewRow(row)">现场复核</button>
            <button class="link" type="button" @click="issueRow(row)">签发</button>
            <button class="link" type="button" @click="projectRow(row)">生成限载</button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无桥梁定检数据，可先登记检测记录</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条桥梁定检记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
      <span v-else-if="noticeMessage" class="notice-text">{{ noticeMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>

const ENDPOINT = '/api/bridge'
const columns = ["检测编号", "桥梁名称", "检测类型", "检测日期", "技术状况评分", "评定等级", "评分版本", "签发状态", "主要病害", "检测单位"]
const stats = [{"label": "待复核", "value": 0}, {"label": "已复核待签发", "value": 0}, {"label": "已限载", "value": 0}]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const noticeMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

async function post(path: string, values: Record<string, unknown>) {
  const response = await request(`${ENDPOINT}${path}`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  const payload = (await response.json()) as { ok: boolean; message: string }
  if (!payload.ok) {
    throw new Error(payload.message)
  }
  return payload
}

async function reviewRow(row: Row) {
  errorMessage.value = ''
  noticeMessage.value = ''
  const input = window.prompt(`现场复核 ${row.检测编号}：请输入技术状况评分（0-100）`)
  if (input === null) {
    return
  }
  try {
    const payload = await post(`/${row.id}/review`, { 评分: input })
    noticeMessage.value = payload.message
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '现场复核失败'
  }
}

async function issueRow(row: Row) {
  errorMessage.value = ''
  noticeMessage.value = ''
  try {
    const payload = await post(`/${row.id}/issue`, {})
    noticeMessage.value = payload.message
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '签发失败'
  }
}

async function projectRow(row: Row) {
  errorMessage.value = ''
  noticeMessage.value = ''
  try {
    const payload = await post(`/${row.id}/project-load`, {})
    noticeMessage.value = payload.message
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '生成限载失败'
  }
}

async function issueBatch() {
  errorMessage.value = ''
  noticeMessage.value = ''
  const codes = rows.value
    .filter((row) => row['签发状态'] === '已复核')
    .map((row) => String(row['检测编号']))
  if (!codes.length) {
    errorMessage.value = '当前列表没有已复核待签发的检测记录'
    return
  }
  try {
    const payload = await post('/issuance/batches', { 检测编号: codes })
    noticeMessage.value = payload.message
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '集中签发失败'
  }
}

async function reload() {
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('桥梁定检列表读取失败')
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
