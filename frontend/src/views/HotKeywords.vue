<script setup>
import { ElMessage, ElMessageBox } from "element-plus";
import { onMounted, reactive, ref } from "vue";

import { hotKeywordsApi, keywordsApi } from "../api";

const loading = ref(false);
const rows = ref([]);
const total = ref(0);
const stats = ref(null);
const keywordGroups = ref([]);
const filters = reactive({ days: null, min_count: 1, limit: 200, offset: 0 });

async function load() {
  loading.value = true;
  try {
    const [list, stat] = await Promise.all([
      hotKeywordsApi.list({
        days: filters.days || undefined,
        min_count: filters.min_count,
        limit: filters.limit,
        offset: filters.offset,
      }),
      hotKeywordsApi.stats(),
    ]);
    rows.value = list.data.items;
    total.value = list.data.total;
    stats.value = stat.data;
  } catch (error) {
    ElMessage.error(error.message);
  } finally {
    loading.value = false;
  }
}

async function loadGroups() {
  try {
    const { data } = await keywordsApi.list("keyword");
    keywordGroups.value = data.items;
  } catch {
    keywordGroups.value = [];
  }
}

function search() {
  filters.offset = 0;
  load();
}

async function promote(row) {
  if (!keywordGroups.value.length) {
    ElMessage.warning("还没有关键词组，先去「词库管理」建一个");
    return;
  }
  try {
    const { value } = await ElMessageBox.prompt(
      `把「${row.token}」加进哪个关键词组？（填组名，例如 资源求助）`,
      "加入词库",
      {
        inputPlaceholder: keywordGroups.value.map((item) => item.name).join(" / "),
        inputValidator: (input) =>
          keywordGroups.value.some((item) => item.name === String(input).trim()) ||
          "组名不存在，请从提示里选一个",
        confirmButtonText: "加入",
        cancelButtonText: "取消",
      },
    );
    const group = keywordGroups.value.find((item) => item.name === String(value).trim());
    const { data } = await hotKeywordsApi.promote({ token: row.token, group_id: group.id });
    ElMessage.success(
      data.added ? `已加入「${group.name}」` : `「${group.name}」里已经有这个词了`,
    );
  } catch (error) {
    if (error?.message && !error.message.includes("cancel")) {
      ElMessage.error(error.message);
    }
  }
}

onMounted(async () => {
  await loadGroups();
  await load();
});
</script>

<template>
  <div v-loading="loading">
    <div class="toolbar">
      <h2 class="page-title">热门关键词</h2>
      <span class="card-hint">
        从监听到的会员发言里自动采集，按出现次数排名——用来看"用户都在搜什么"
      </span>
      <div class="spacer" />
      <el-button size="small" @click="load">刷新</el-button>
    </div>

    <el-row :gutter="12">
      <el-col :xs="12" :sm="8">
        <el-card shadow="never">
          <div class="card-hint">累计采集词条（永不删除）</div>
          <div class="stat-value">{{ stats?.tokens ?? 0 }}</div>
        </el-card>
      </el-col>
      <el-col :xs="12" :sm="8">
        <el-card shadow="never">
          <div class="card-hint">累计出现次数</div>
          <div class="stat-value">{{ stats?.occurrences ?? 0 }}</div>
        </el-card>
      </el-col>
      <el-col :xs="12" :sm="8">
        <el-card shadow="never">
          <div class="card-hint">今日新增词</div>
          <div class="stat-value">{{ stats?.new_today ?? 0 }}</div>
        </el-card>
      </el-col>
    </el-row>

    <el-card shadow="never" class="panel-gap">
      <div class="filters">
        <el-select v-model="filters.days" size="small" clearable placeholder="全部时间" style="width: 130px" @change="search">
          <el-option label="近 1 天" :value="1" />
          <el-option label="近 3 天" :value="3" />
          <el-option label="近 7 天" :value="7" />
          <el-option label="近 30 天" :value="30" />
        </el-select>
        <el-input-number v-model="filters.min_count" size="small" :min="1" :max="1000" style="width: 140px" />
        <span class="card-hint">最少出现次数</span>
        <el-button size="small" type="primary" @click="search">筛选</el-button>
      </div>

      <el-table :data="rows" size="small" border class="table-gap">
        <el-table-column prop="rank" label="#" width="60" />
        <el-table-column label="关键词" min-width="160">
          <template #default="{ row }">
            <span class="chat-name">{{ row.token }}</span>
          </template>
        </el-table-column>
        <el-table-column prop="count" label="出现次数" width="100" sortable />
        <el-table-column label="来源群" min-width="200">
          <template #default="{ row }">
            <span class="card-hint">{{ row.source_text || "-" }}</span>
          </template>
        </el-table-column>
        <el-table-column label="首次" width="150">
          <template #default="{ row }">{{ (row.first_seen_at || "").slice(0, 16) }}</template>
        </el-table-column>
        <el-table-column label="最近" width="150">
          <template #default="{ row }">{{ (row.last_seen_at || "").slice(0, 16) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="110">
          <template #default="{ row }">
            <el-button size="small" link type="primary" @click="promote(row)">加入词库</el-button>
          </template>
        </el-table-column>
      </el-table>

      <p class="card-hint">
        下方排名为去噪后的 {{ total }} 条（同一短语切出的重叠碎片、被长词包含的短词已合并）；
        上方统计是累计采集的原始词条数。
      </p>

      <el-pagination
        class="table-gap"
        layout="prev, pager, next, total"
        :total="total"
        :page-size="filters.limit"
        :current-page="Math.floor(filters.offset / filters.limit) + 1"
        @current-change="(page) => { filters.offset = (page - 1) * filters.limit; load(); }"
      />
    </el-card>

    <el-alert
      class="panel-gap"
      type="info"
      :closable="false"
      show-icon
      title="这些词是自动采到的，永久保留"
      description="采集规则：只统计通过过滤（非机器人、非排除词、字数达标）的会员发言；先剥掉链接、@用户名、手机号，再把中文按 2~4 字滑窗、英文按整词统计；同频次下短的会被长的合并（抖音/抖音号 只留抖音号）。发现高频词直接点「加入词库」就能补进关键词组。"
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
</style>
