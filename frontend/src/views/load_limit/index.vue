<template>
  <section class="page" data-module="load_limit">
    <header class="page-head">
      <div>
        <h2>限载管理</h2>
        <p class="page-desc">限载投影由桥梁定检签发回写产生，每个检测编号只保留一个生效版本，旧评分不再重复显示。</p>
      </div>
      <div class="page-actions">
        <button class="btn" type="button" @click="exportRows">导出限载清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label class="filter-item">
        <span>检测编号</span>
        <input v-model="filters['检测编号']" placeholder="按检测编号或桥梁名称检索" />
      </label>
      <label class="filter-item">
        <span>投影状态</span>
        <select v-model="filters['投影状态']">
          <option value="">生效中</option>
          <option value="已失效">已失效</option>
          <option value="全部">全部</option>
        </select>
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length" class="empty-state">暂无生效限载投影，请先在桥梁定检签发结论</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条限载投影</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>

const ENDPOINT = '/api/load_limit'
const columns = ["限载编号", "检测编号", "桥梁名称", "技术状况评分", "技术状况等级", "评分版本", "限载吨位", "投影状态", "签发时间"]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})

const stats = computed(() => [
  { label: '生效投影', value: rows.value.filter((row) => row['投影状态'] === '生效中').length },
  { label: '限载桥梁', value: rows.value.filter((row) => row['限载吨位'] && row['限载吨位'] !== '不限载').length },
  { label: '四五类桥梁', value: rows.value.filter((row) => ['四类', '五类'].includes(String(row['技术状况等级']))).length },
])

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

async function reload() {
  errorMessage.value = ''
  const params = new URLSearchParams()
  if (filters.value['检测编号']) {
    params.set('keyword', filters.value['检测编号'])
  }
  const status = filters.value['投影状态']
  if (status === '已失效') {
    params.set('status', '已失效')
  } else if (status === '全部') {
    params.set('status', '全部')
    params.set('size', '200')
  }
  try {
    const response = await request(`${ENDPOINT}?${params.toString()}`)
    if (!response.ok) {
      throw new Error('限载投影列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '限载投影列表读取失败'
  }
}

onMounted(reload)
</script>
