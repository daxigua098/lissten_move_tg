<script setup>
import { ElMessage } from "element-plus";
import { onMounted, reactive, ref } from "vue";

import { hotKeywordsApi, keywordsApi } from "../api";

const loading = ref(false);
const rows = ref([]);
const total = ref(0);
const stats = ref(null);
const keywordGroups = ref([]);
const filters = reactive({ days: null, min_count: 1, limit: 200, offset: 0 });
// 「加入词库」弹窗：可以选已有的组，也可以直接输入新组名
const promoteVisible = ref(false);
const promoteSaving = ref(false);
const promoteRow = ref(null);
const targetGroupId = ref(null);
const newGroupName = ref("");

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

function promote(row) {
  promoteRow.value = row;
  targetGroupId.value = null;
  newGroupName.value = "";
  promoteVisible.value = true;
}

async function confirmPromote() {
  const row = promoteRow.value;
  if (!row) return;
  const typedName = (newGroupName.value || "").trim();
  if (!typedName && !targetGroupId.value) {
    ElMessage.warning("请选择已有的组，或输入一个新组名");
    return;
  }
  promoteSaving.value = true;
  try {
    let group = typedName
      ? keywordGroups.value.find((item) => item.name === typedName)
      : keywordGroups.value.find((item) => item.id === targetGroupId.value);
    if (!group) {
      // 输入的是新组名：先建组，再把词加进去
      const created = await keywordsApi.create({ name: typedName, kind: "keyword" });
      group = { id: created.data.id, name: created.data.name };
      ElMessage.success(`已新建关键词组「${typedName}」`);
      await loadGroups();
    }
    const { data } = await hotKeywordsApi.promote({
      token: row.token,
      group_id: group.id,
      aliases: (row.variants || [])
        .map((item) => item.token)
        .filter((item) => item && item !== row.token),
    });
    ElMessage.success(
      data.added
        ? `已加入「${group.name}」${row.variants?.length > 1 ? "（变体一起写进别名了）" : ""}`
        : `「${group.name}」里已经有这个词了`,
    );
    promoteVisible.value = false;
  } catch (error) {
    ElMessage.error(error.message);
  } finally {
    promoteSaving.value = false;
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
            <el-tag v-if="row.merged" size="small" type="success" class="merged-tag">已归并</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="count" label="出现次数" width="110" sortable />
        <el-table-column label="同类说法" min-width="200">
          <template #default="{ row }">
            <span v-if="row.variant_text" class="card-hint">{{ row.variant_text }}</span>
            <span v-else class="card-hint">-</span>
          </template>
        </el-table-column>
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
      description="采集规则：只统计通过过滤（非机器人、非排除词、字数达标）的会员发言；先剥掉链接、@用户名、手机号，再把中文按 2~4 字滑窗、英文按整词统计。出现次数按消息计——同一条消息里重复刷同一个词只算一次。同频次下短词会被长词合并（抖音/抖音号 只留抖音号）。标了「已归并」的行，是按「词库管理 → 归并规则」把同类说法合并后的结果（例：微信 = 加我微信 + 微信同号）；点「加入词库」会把归类名作为主词、各个变体写成别名。"
    />

    <el-dialog v-model="promoteVisible" title="加入词库" width="480px">
      <div v-if="promoteRow" class="promote-body">
        <p>
          把 <b>{{ promoteRow.token }}</b>（出现 {{ promoteRow.count }} 次）加入哪个关键词组？
        </p>
        <el-select v-model="targetGroupId" clearable placeholder="选择已有的关键词组" style="width: 100%">
          <el-option
            v-for="item in keywordGroups"
            :key="item.id"
            :label="`${item.name}（${item.keyword_count} 个词）`"
            :value="item.id"
          />
        </el-select>
        <el-input
          v-model="newGroupName"
          placeholder="或者输入新组名（填了就新建这一组）"
          class="new-group-input"
        />
        <p class="card-hint">
          两个都填时以「新组名」为准。<span v-if="promoteRow.variants?.length > 1">
            这条是归并结果，会把 {{ promoteRow.variants.map((item) => item.token).join("、") }}
            一起写进别名。</span>
        </p>
      </div>
      <template #footer>
        <el-button @click="promoteVisible = false">取消</el-button>
        <el-button type="primary" :loading="promoteSaving" @click="confirmPromote">加入</el-button>
      </template>
    </el-dialog>
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

.merged-tag {
  margin-left: 6px;
}

.promote-body p {
  margin: 0 0 8px;
  line-height: 1.7;
}

.new-group-input {
  margin-top: 8px;
}
</style>
