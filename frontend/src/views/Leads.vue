<script setup>
import { ElMessage, ElMessageBox } from "element-plus";
import { onMounted, reactive, ref } from "vue";

import { http, leadsApi, sourcesApi } from "../api";

const loading = ref(false);
const rows = ref([]);
const total = ref(0);
const stats = ref(null);
const sources = ref([]);
const filters = reactive({
  source_chat_id: null,
  keyword: "",
  days: 3,
  only_hits: false,
  limit: 100,
  offset: 0,
});

async function load() {
  loading.value = true;
  try {
    const params = {
      source_chat_id: filters.source_chat_id || undefined,
      keyword: filters.keyword || undefined,
      days: filters.days || undefined,
      only_hits: filters.only_hits || undefined,
      limit: filters.limit,
      offset: filters.offset,
    };
    const [list, stat] = await Promise.all([leadsApi.list(params), leadsApi.stats()]);
    rows.value = list.data.items;
    total.value = list.data.total;
    stats.value = stat.data;
  } catch (error) {
    ElMessage.error(error.message);
  } finally {
    loading.value = false;
  }
}

async function loadSources() {
  try {
    const { data } = await sourcesApi.list({ limit: 200 });
    sources.value = data.items;
  } catch {
    sources.value = [];
  }
}

function search() {
  filters.offset = 0;
  load();
}

function onPage(page) {
  filters.offset = (page - 1) * filters.limit;
  load();
}

async function purgeDelivered() {
  // 「已完成」= 已经推送到接收群的线索；未推送的一条都不动
  const done = Math.max(0, Number(stats.value?.total || 0) - Number(stats.value?.undelivered || 0));
  try {
    await ElMessageBox.confirm(
      `将删除 ${done} 条「已推送」的线索，未推送的会保留。删除前会自动归档到服务器，操作不可撤销。`,
      "清空已完成",
      { type: "warning", confirmButtonText: "确认清空", cancelButtonText: "取消" },
    );
  } catch {
    return;
  }
  try {
    const { data } = await leadsApi.purgeDelivered();
    ElMessage.success(data.message || `已清空 ${data.deleted} 条`);
    filters.offset = 0;
    await load();
  } catch (error) {
    ElMessage.error(error.message);
  }
}

async function purgeAll() {
  const leadCount = Math.max(0, Number(stats.value?.total || 0));
  const memberCount = Math.max(0, Number(stats.value?.members || 0));
  try {
    await ElMessageBox.confirm(
      `将删除全部 ${leadCount} 条线索和 ${memberCount} 个会员档案。删除前会自动归档线索，操作不可撤销。`,
      "清空全部",
      {
        type: "warning",
        confirmButtonText: "确认清空全部",
        cancelButtonText: "取消",
      },
    );
  } catch {
    return;
  }
  try {
    const { data } = await leadsApi.purgeAll();
    ElMessage.success(data.message || `已清空 ${data.deleted} 条线索`);
    filters.offset = 0;
    await load();
  } catch (error) {
    ElMessage.error(error.message);
  }
}

async function exportCsv() {
  try {
    const url = leadsApi.exportUrl({
      source_chat_id: filters.source_chat_id || undefined,
      keyword: filters.keyword || undefined,
      days: filters.days || undefined,
      only_hits: filters.only_hits || undefined,
    });
    const response = await http.get(url, { responseType: "blob" });
    const objectUrl = URL.createObjectURL(response.data);
    const link = document.createElement("a");
    link.href = objectUrl;
    link.download = `线索-${new Date().toISOString().slice(0, 10)}.csv`;
    link.click();
    URL.revokeObjectURL(objectUrl);
    ElMessage.success("已导出 CSV");
  } catch (error) {
    ElMessage.error(error.message);
  }
}

onMounted(async () => {
  await loadSources();
  await load();
});
</script>

<template>
  <div v-loading="loading">
    <div class="toolbar">
      <h2 class="page-title">线索池</h2>
      <span class="card-hint">共 {{ total }} 条</span>
      <div class="spacer" />
      <el-button size="small" @click="load">刷新</el-button>
      <el-button size="small" @click="purgeDelivered">清空已完成</el-button>
      <el-button size="small" type="danger" @click="purgeAll">清空全部</el-button>
      <el-button size="small" type="primary" @click="exportCsv">导出 CSV</el-button>
    </div>

    <el-row :gutter="12">
      <el-col :xs="12" :sm="6">
        <el-card shadow="never">
          <div class="card-hint">今日新增</div>
          <div class="stat-value">{{ stats?.today ?? 0 }}</div>
        </el-card>
      </el-col>
      <el-col :xs="12" :sm="6">
        <el-card shadow="never">
          <div class="card-hint">命中线索（永久保留）</div>
          <div class="stat-value">{{ stats?.hits ?? 0 }}</div>
          <div class="card-hint">永久保留用户 {{ stats?.pinned_members ?? 0 }} 人</div>
        </el-card>
      </el-col>
      <el-col :xs="12" :sm="6">
        <el-card shadow="never">
          <div class="card-hint">线索总数</div>
          <div class="stat-value">{{ stats?.total ?? 0 }}</div>
          <div class="card-hint">未推送 {{ stats?.undelivered ?? 0 }} 条</div>
        </el-card>
      </el-col>
      <el-col :xs="12" :sm="6">
        <el-card shadow="never">
          <div class="card-hint">会员档案</div>
          <div class="stat-value">{{ stats?.members ?? 0 }}</div>
        </el-card>
      </el-col>
    </el-row>

    <el-card shadow="never" class="panel-gap">
      <div class="filters">
        <el-select
          v-model="filters.source_chat_id"
          size="small"
          clearable
          placeholder="全部来源群"
          style="width: 220px"
          @change="search"
        >
          <el-option
            v-for="item in sources"
            :key="item.id"
            :label="item.name || item.title || item.username"
            :value="item.id"
          />
        </el-select>
        <el-input
          v-model="filters.keyword"
          size="small"
          placeholder="按关键词筛选"
          style="width: 180px"
          @keyup.enter="search"
        />
        <el-select v-model="filters.days" size="small" style="width: 130px" @change="search">
          <el-option label="近 1 天" :value="1" />
          <el-option label="近 3 天" :value="3" />
          <el-option label="近 7 天" :value="7" />
          <el-option label="近 30 天" :value="30" />
        </el-select>
        <el-checkbox v-model="filters.only_hits" size="small" @change="search">
          只看命中（永久保留）
        </el-checkbox>
        <el-button size="small" type="primary" @click="search">筛选</el-button>
      </div>

      <el-table :data="rows" size="small" border class="table-gap">
        <el-table-column prop="id" label="ID" width="64" />
        <el-table-column label="时间" width="150">
          <template #default="{ row }">{{ (row.message_at || row.created_at || "").slice(0, 16) }}</template>
        </el-table-column>
        <el-table-column prop="source_title" label="来源群" min-width="130" />
        <el-table-column label="会员" min-width="150">
          <template #default="{ row }">
            <span class="chat-name">{{ row.sender_name || "（无昵称）" }}</span>
            <div class="card-hint">
              {{ row.sender_username ? `@${row.sender_username}` : "" }}
              {{ row.sender_tg_id ? `· ID ${row.sender_tg_id}` : "" }}
            </div>
          </template>
        </el-table-column>
        <el-table-column label="联系方式" min-width="150">
          <template #default="{ row }">
            <div v-if="row.phone">📞 {{ row.phone }}</div>
            <div v-if="row.wechat">💬 {{ row.wechat }}</div>
            <span v-if="!row.phone && !row.wechat" class="card-hint">只给了用户名</span>
          </template>
        </el-table-column>
        <el-table-column label="命中" width="130">
          <template #default="{ row }">
            <el-tag v-if="row.keyword" size="small" type="warning">{{ row.keyword }}</el-tag>
            <span v-else class="card-hint">全量入库</span>
          </template>
        </el-table-column>
        <el-table-column label="原文" min-width="220">
          <template #default="{ row }">
            <span class="text-body">{{ row.text }}</span>
          </template>
        </el-table-column>
        <el-table-column label="已推送" width="90">
          <template #default="{ row }">
            <el-tag size="small" :type="row.delivered ? 'success' : 'info'">
              {{ row.delivered ? "已推" : "仅入库" }}
            </el-tag>
          </template>
        </el-table-column>
      </el-table>

      <el-pagination
        class="table-gap"
        layout="prev, pager, next, total"
        :total="total"
        :page-size="filters.limit"
        :current-page="Math.floor(filters.offset / filters.limit) + 1"
        @current-change="onPage"
      />
    </el-card>

    <el-alert
      class="panel-gap"
      type="info"
      :closable="false"
      show-icon
      title="全量监听只入库、不刷屏"
      description="B 线默认把所有发言存进线索池，只有命中关键词的才会推卡片到接收群。保留策略分两档：命中关键词的线索与对应用户永久保留；未命中的线索与档案按保留策略（默认 3 天）清理，线索删除前会先归档成 JSONL 放到 data/archive。"
    />
  </div>
</template>

<style scoped>
.stat-value {
  font-size: 22px;
  font-weight: 500;
  margin: 4px 0;
}

.filters {
  display: flex;
  gap: 8px;
  align-items: center;
  flex-wrap: wrap;
}

.table-gap {
  margin-top: 10px;
}

.panel-gap {
  margin-top: 12px;
}

.text-body {
  word-break: break-word;
}
</style>
