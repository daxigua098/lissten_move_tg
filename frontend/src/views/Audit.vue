<script setup>
import { ElMessage } from "element-plus";
import { onMounted, reactive, ref } from "vue";

import { logsApi } from "../api";

const loading = ref(false);
const rows = ref([]);
const total = ref(0);
const filters = reactive({ username: "", path: "", limit: 50, offset: 0 });

async function load() {
  loading.value = true;
  try {
    const params = { limit: filters.limit, offset: filters.offset };
    if (filters.username) params.username = filters.username;
    if (filters.path) params.path = filters.path;
    const { data } = await logsApi.audit(params);
    rows.value = data.items;
    total.value = data.total;
  } catch (error) {
    ElMessage.error(error.message);
  } finally {
    loading.value = false;
  }
}

function search() {
  filters.offset = 0;
  load();
}

function changePage(page) {
  filters.offset = (page - 1) * filters.limit;
  load();
}

function fmt(value) {
  return value ? new Date(value).toLocaleString("zh-CN") : "-";
}

onMounted(load);
</script>

<template>
  <div>
    <div class="toolbar">
      <h2 class="page-title">审计日志</h2>
      <div class="spacer" />
      <el-input v-model="filters.username" size="small" placeholder="按账号筛选" style="width: 160px" />
      <el-input v-model="filters.path" size="small" placeholder="按路径筛选" style="width: 200px" />
      <el-button size="small" type="primary" @click="search">查询</el-button>
      <el-button size="small" @click="load">刷新</el-button>
    </div>

    <el-table v-loading="loading" :data="rows" size="small" border>
      <el-table-column prop="id" label="ID" width="70" />
      <el-table-column prop="username" label="账号" width="140" />
      <el-table-column prop="method" label="方法" width="90" />
      <el-table-column prop="path" label="路径" min-width="220" />
      <el-table-column label="结果" width="90">
        <template #default="{ row }">
          <el-tag :type="row.status_code < 400 ? 'success' : 'danger'" size="small">
            {{ row.status_code }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="ip_address" label="IP" width="140" />
      <el-table-column label="时间" width="180">
        <template #default="{ row }">{{ fmt(row.created_at) }}</template>
      </el-table-column>
    </el-table>

    <el-pagination
      class="pager"
      layout="total, prev, pager, next"
      :total="total"
      :page-size="filters.limit"
      @current-change="changePage"
    />
  </div>
</template>

<style scoped>
.pager {
  margin-top: 12px;
  justify-content: flex-end;
}
</style>
