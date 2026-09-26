<script setup>
import { ElMessage, ElMessageBox } from "element-plus";
import { computed, onMounted, reactive, ref } from "vue";

import { resourcesApi, targetsApi } from "../api";
import FieldHelp from "../components/FieldHelp.vue";
import { RESOURCE_HELP } from "../resourceHelp";

const loading = ref(false);
const rows = ref([]);
const total = ref(0);
const selection = ref([]);
const overview = ref(null);
const facets = ref({ languages: [], categories: [], chat_types: [], sorts: [], freshness: [] });
const targets = ref([]);

const filters = reactive({
  keyword: "",
  chat_type: "",
  languages: [],
  categories: [],
  member_min: null,
  member_max: null,
  min_activity: null,
  is_index_group: null,
  freshness: "",
  active_within_days: null,
  adopted: null,
  blacklisted: false,
  favorite: null,
  sort: "activity",
  limit: 50,
  offset: 0,
});

const drawerVisible = ref(false);
const detail = ref(null);
const detailLoading = ref(false);

const importVisible = ref(false);
const importSaving = ref(false);
const importForm = reactive({ text: "", join: false, probe: true });
const importResult = ref(null);

const adoptVisible = ref(false);
const adoptSaving = ref(false);
const adoptRow = ref(null);
const adoptForm = reactive({
  create_route: false,
  business_type: "B",
  target_chat_ids: [],
  route_name: "",
});

const queryParams = computed(() => {
  const params = {};
  Object.entries(filters).forEach(([key, value]) => {
    if (value === null || value === undefined || value === "" || value === false) return;
    if (Array.isArray(value) && value.length === 0) return;
    params[key] = value;
  });
  return params;
});

function fmtCount(value) {
  if (value === null || value === undefined) return "-";
  if (value >= 10000) return `${(value / 10000).toFixed(1)} 万`;
  return String(value);
}

function fmtScore(value) {
  if (value === null || value === undefined) return "-";
  return Number(value).toFixed(1);
}

function fmtTime(value) {
  return value ? value.replace("T", " ").slice(0, 16) : "-";
}

async function load() {
  loading.value = true;
  try {
    const [list, facet, stat] = await Promise.all([
      resourcesApi.list(queryParams.value),
      resourcesApi.facets(),
      resourcesApi.overview(),
    ]);
    rows.value = list.data.items;
    total.value = list.data.total;
    facets.value = facet.data;
    overview.value = stat.data;
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

function resetFilters() {
  Object.assign(filters, {
    keyword: "",
    chat_type: "",
    languages: [],
    categories: [],
    member_min: null,
    member_max: null,
    min_activity: null,
    is_index_group: null,
    freshness: "",
    active_within_days: null,
    adopted: null,
    blacklisted: false,
    favorite: null,
    sort: "activity",
    offset: 0,
  });
  load();
}

async function collect() {
  loading.value = true;
  try {
    const { data } = await resourcesApi.collect({ keywords: [] });
    const item = (data.results || [])[0];
    if (!item) {
      ElMessage.info(data.hint || "没有到点的发现任务");
    } else if (item.skipped) {
      ElMessage.info(`「${item.keyword}」本轮跳过（${item.skipped}）`);
    } else {
      ElMessage.success(`「${item.keyword}」发现 ${item.hits} 个，新增 ${item.new_resources} 条候选`);
    }
    await load();
  } catch (error) {
    ElMessage.error(error.message);
  } finally {
    loading.value = false;
  }
}

async function openDetail(row) {
  drawerVisible.value = true;
  detailLoading.value = true;
  detail.value = null;
  try {
    const { data } = await resourcesApi.detail(row.id);
    detail.value = data;
  } catch (error) {
    ElMessage.error(error.message);
  } finally {
    detailLoading.value = false;
  }
}

async function refreshOne(row) {
  try {
    const { data } = await resourcesApi.refreshOne(row.id);
    if (data.result === "ok") {
      ElMessage.success(`已刷新「${row.title}」`);
    } else {
      ElMessage.warning(`刷新失败：${data.error}`);
    }
    await load();
    if (drawerVisible.value && detail.value?.id === row.id) await openDetail(row);
  } catch (error) {
    ElMessage.error(error.message);
  }
}

async function batchRefresh() {
  const ids = selection.value.map((item) => item.id);
  if (!ids.length) {
    ElMessage.warning("请先勾选要刷新的资源");
    return;
  }
  loading.value = true;
  try {
    const { data } = await resourcesApi.refresh({ ids });
    const failed = data.failed?.length || 0;
    ElMessage.success(`刷新完成：成功 ${data.succeeded?.length || 0} 条${failed ? `，失败 ${failed} 条` : ""}`);
    if (failed) {
      const first = data.failed[0];
      ElMessage.warning(`首个失败：${first.title} —— ${first.error}`);
    }
    await load();
  } catch (error) {
    ElMessage.error(error.message);
  } finally {
    loading.value = false;
  }
}

async function enqueueJoin(action) {
  const ids = selection.value.map((item) => item.id);
  if (!ids.length) {
    ElMessage.warning("请先勾选资源");
    return;
  }
  try {
    const { data } = await resourcesApi.join({ ids, action });
    const ok = data.queued?.length || 0;
    const bad = data.failures?.length || 0;
    ElMessage.success(
      `已排入${action === "join" ? "加群" : "退群"}队列 ${ok} 条${bad ? `，${bad} 条未入队` : ""}`,
    );
    if (bad) ElMessage.warning(data.failures[0].reason);
    await load();
  } catch (error) {
    ElMessage.error(error.message);
  }
}

async function toggleFavorite(row, value) {
  try {
    await resourcesApi.update(row.id, { is_favorite: value });
    row.is_favorite = value;
    if (detail.value?.id === row.id) detail.value.is_favorite = value;
  } catch (error) {
    ElMessage.error(error.message);
  }
}

function toggleBlacklist(row) {
  const next = !row.is_blacklisted;
  ElMessageBox.prompt(
    next ? "拉黑后自动发现会直接跳过这个群，且不能被采纳。" : "确认移出黑名单？",
    next ? "加入黑名单" : "移出黑名单",
    {
      inputPlaceholder: next ? "拉黑原因（可留空）" : undefined,
      inputValue: "",
      showCancelButton: true,
      confirmButtonText: "确认",
      cancelButtonText: "取消",
    },
  )
    .then(async ({ value }) => {
      await resourcesApi.update(row.id, {
        is_blacklisted: next,
        blacklist_reason: next ? value : null,
      });
      ElMessage.success(next ? "已加入黑名单" : "已移出黑名单");
      await load();
    })
    .catch(() => {});
}

function openImport() {
  importForm.text = "";
  importForm.join = false;
  importForm.probe = true;
  importResult.value = null;
  importVisible.value = true;
}

async function confirmImport() {
  const inputs = importForm.text
    .split("\n")
    .map((item) => item.trim())
    .filter(Boolean);
  if (!inputs.length) {
    ElMessage.warning("请粘贴至少一个链接或标识");
    return;
  }
  importSaving.value = true;
  try {
    const { data } = await resourcesApi.import({
      inputs,
      join: importForm.join,
      probe: importForm.probe,
    });
    importResult.value = data;
    ElMessage.success(`已处理 ${data.added.length} 条，失败 ${data.failures.length} 条`);
    await load();
  } catch (error) {
    ElMessage.error(error.message);
  } finally {
    importSaving.value = false;
  }
}

async function openAdopt(row) {
  adoptRow.value = row;
  adoptForm.create_route = false;
  adoptForm.business_type = "B";
  adoptForm.target_chat_ids = [];
  adoptForm.route_name = "";
  adoptVisible.value = true;
  if (!targets.value.length) {
    try {
      const { data } = await targetsApi.list({ limit: 200 });
      targets.value = data.items;
    } catch {
      targets.value = [];
    }
  }
}

async function confirmAdopt() {
  const row = adoptRow.value;
  if (!row) return;
  adoptSaving.value = true;
  try {
    const { data } = await resourcesApi.adopt(row.id, {
      create_route: adoptForm.create_route,
      business_type: adoptForm.business_type,
      target_chat_ids: adoptForm.target_chat_ids,
      route_name: adoptForm.route_name || null,
    });
    const routeText = data.routes?.length ? `，已建 ${data.routes.length} 条线路` : "";
    ElMessage.success(`已加入监听源${routeText}`);
    if (data.join_task?.status === "pending") {
      ElMessage.info("该资源还没确认在群里，已按限速排入加群队列");
    }
    adoptVisible.value = false;
    await load();
  } catch (error) {
    ElMessage.error(error.message);
  } finally {
    adoptSaving.value = false;
  }
}

function exportCsv() {
  const params = { ...queryParams.value };
  delete params.limit;
  delete params.offset;
  window.open(resourcesApi.exportUrl(params), "_blank");
}

const freshnessTag = { fresh: "success", warm: "warning", stale: "info", new: "" };
const freshnessLabel = { fresh: "24 小时内", warm: "7 天内", stale: "超过 7 天", new: "未探测" };
const statusLabel = {
  candidate: "候选",
  probed: "已探测",
  adopted: "已采纳",
  retired: "已淘汰",
};

/** 详情里的迷你趋势图：成员数与活跃度（数据来自探测日志）。 */
function sparkPath(values) {
  const points = values.filter((item) => item !== null && item !== undefined);
  if (points.length < 2) return "";
  const max = Math.max(...points);
  const min = Math.min(...points);
  const span = max - min || 1;
  const step = 100 / (points.length - 1);
  return points
    .map((value, index) => {
      const x = (index * step).toFixed(2);
      const y = (30 - ((value - min) / span) * 28).toFixed(2);
      return `${index === 0 ? "M" : "L"}${x},${y}`;
    })
    .join(" ");
}

onMounted(load);
</script>

<template>
  <div v-loading="loading">
    <div class="toolbar">
      <h2 class="page-title">资源发现</h2>
      <span class="card-hint">
        找群 → 评估 → 采纳。列表只读本地库，点「采集一次 / 刷新」才会访问 Telegram。
      </span>
      <div class="spacer" />
      <el-button size="small" @click="openImport">手动添加</el-button>
      <el-button size="small" @click="exportCsv">导出</el-button>
      <el-button size="small" type="primary" @click="collect">采集一次</el-button>
    </div>

    <el-row :gutter="12">
      <el-col :xs="12" :sm="6">
        <el-card shadow="never">
          <div class="card-hint">今日剩余搜索额度</div>
          <div class="stat-value">{{ overview?.searches_left ?? 0 }}</div>
          <div class="card-hint">上限 {{ overview?.search_daily_limit ?? 0 }} 次/天</div>
        </el-card>
      </el-col>
      <el-col :xs="12" :sm="6">
        <el-card shadow="never">
          <div class="card-hint">今日已加入 / 上限</div>
          <div class="stat-value">
            {{ overview?.joins_today ?? 0 }}/{{ overview?.join_daily_limit ?? 0 }}
          </div>
          <div class="card-hint">加群队列 {{ overview?.join_queue ?? 0 }} 条</div>
        </el-card>
      </el-col>
      <el-col :xs="12" :sm="6">
        <el-card shadow="never">
          <div class="card-hint">刷新队列 / 待审批</div>
          <div class="stat-value">
            {{ overview?.refresh_queue ?? 0 }}/{{ overview?.join_waiting_approval ?? 0 }}
          </div>
          <div class="card-hint">今日探测 {{ overview?.probes_today ?? 0 }} 次</div>
        </el-card>
      </el-col>
      <el-col :xs="12" :sm="6">
        <el-card shadow="never">
          <div class="card-hint">资源库</div>
          <div class="stat-value">{{ overview?.resources?.total ?? 0 }}</div>
          <div class="card-hint">
            候选 {{ overview?.resources?.candidates ?? 0 }} · 已采纳
            {{ overview?.resources?.adopted ?? 0 }}
          </div>
        </el-card>
      </el-col>
    </el-row>

    <el-card shadow="never" class="panel-gap">
      <div class="filters">
        <el-input
          v-model="filters.keyword"
          size="small"
          placeholder="名称 / 用户名 / 简介"
          style="width: 200px"
          @keyup.enter="search"
        />
        <el-select v-model="filters.chat_type" size="small" clearable placeholder="类型" style="width: 110px">
          <el-option
            v-for="item in facets.chat_types"
            :key="item.value"
            :label="item.label"
            :value="item.value"
          />
        </el-select>
        <el-select
          v-model="filters.languages"
          size="small"
          multiple
          collapse-tags
          clearable
          placeholder="语言"
          style="width: 130px"
        >
          <el-option v-for="item in facets.languages" :key="item" :label="item" :value="item" />
        </el-select>
        <el-select
          v-model="filters.categories"
          size="small"
          multiple
          collapse-tags
          clearable
          placeholder="行业"
          style="width: 150px"
        >
          <el-option v-for="item in facets.categories" :key="item" :label="item" :value="item" />
        </el-select>
        <el-input-number
          v-model="filters.member_min"
          size="small"
          :min="0"
          :controls="false"
          placeholder="成员数下限"
          style="width: 110px"
        />
        <el-input-number
          v-model="filters.member_max"
          size="small"
          :min="0"
          :controls="false"
          placeholder="上限"
          style="width: 100px"
        />
        <el-input-number
          v-model="filters.min_activity"
          size="small"
          :min="0"
          :max="100"
          :controls="false"
          placeholder="活跃度≥"
          style="width: 100px"
        />
        <span class="filter-label">
          活跃度口径
          <FieldHelp v-bind="RESOURCE_HELP.activity" />
        </span>
        <el-select
          v-model="filters.is_index_group"
          size="small"
          clearable
          placeholder="索引型"
          style="width: 110px"
        >
          <el-option label="是" :value="true" />
          <el-option label="否" :value="false" />
        </el-select>
        <el-select
          v-model="filters.freshness"
          size="small"
          clearable
          placeholder="新鲜度"
          style="width: 120px"
        >
          <el-option
            v-for="item in facets.freshness"
            :key="item.value"
            :label="item.label"
            :value="item.value"
          />
        </el-select>
        <el-select
          v-model="filters.sort"
          size="small"
          placeholder="排序"
          style="width: 140px"
          @change="search"
        >
          <el-option
            v-for="item in facets.sorts"
            :key="item.value"
            :label="item.label"
            :value="item.value"
          />
        </el-select>
        <el-checkbox v-model="filters.favorite" :true-value="true" :false-value="null">
          只看收藏
        </el-checkbox>
        <el-checkbox v-model="filters.blacklisted">只看黑名单</el-checkbox>
        <el-button size="small" type="primary" @click="search">筛选</el-button>
        <el-button size="small" @click="resetFilters">重置</el-button>
      </div>

      <div class="bulk">
        <el-button size="small" @click="batchRefresh">
          批量刷新
          <FieldHelp v-bind="RESOURCE_HELP.refresh" />
        </el-button>
        <el-button size="small" @click="enqueueJoin('join')">
          加入群组池
          <FieldHelp v-bind="RESOURCE_HELP.joinLimit" />
        </el-button>
        <el-button size="small" @click="enqueueJoin('leave')">退出群组</el-button>
      </div>

      <el-table
        :data="rows"
        size="small"
        border
        class="table-gap"
        @selection-change="(value) => (selection = value)"
      >
        <el-table-column type="selection" width="42" />
        <el-table-column label="名称" min-width="220">
          <template #default="{ row }">
            <el-button link type="primary" @click="openDetail(row)">{{ row.name }}</el-button>
            <el-tag
              v-if="row.freshness"
              size="small"
              :type="freshnessTag[row.freshness]"
              class="tag-gap"
            >
              {{ freshnessLabel[row.freshness] }}
            </el-tag>
            <el-tag v-if="row.is_index_group" size="small" type="success" class="tag-gap">
              索引
            </el-tag>
            <el-tag v-if="row.status === 'adopted'" size="small" type="warning" class="tag-gap">
              已加入来源
            </el-tag>
            <el-tag v-if="row.is_blacklisted" size="small" type="danger" class="tag-gap">
              黑名单
            </el-tag>
            <el-tag v-if="row.is_favorite" size="small" class="tag-gap">收藏</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="类型" width="80">
          <template #default="{ row }">
            {{ { channel: "频道", supergroup: "超级群", group: "群组" }[row.chat_type] || row.chat_type }}
          </template>
        </el-table-column>
        <el-table-column label="成员" width="110">
          <template #default="{ row }">
            {{ fmtCount(row.member_count) }}
            <span v-if="row.member_count_approx" class="card-hint">（近似）</span>
          </template>
        </el-table-column>
        <el-table-column label="活跃度" width="90">
          <template #default="{ row }">{{ fmtScore(row.activity_score) }}</template>
        </el-table-column>
        <el-table-column label="潜力" width="80">
          <template #default="{ row }">{{ fmtScore(row.lead_potential) }}</template>
        </el-table-column>
        <el-table-column label="语言" width="80">
          <template #default="{ row }">{{ row.language || "-" }}</template>
        </el-table-column>
        <el-table-column label="行业" min-width="140">
          <template #default="{ row }">
            <span class="card-hint">{{ (row.categories || []).join("、") || "-" }}</span>
          </template>
        </el-table-column>
        <el-table-column label="最后活跃" width="140">
          <template #default="{ row }">{{ fmtTime(row.last_active_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="210" fixed="right">
          <template #default="{ row }">
            <el-button size="small" link type="primary" @click="openDetail(row)">详情</el-button>
            <el-button size="small" link type="primary" @click="refreshOne(row)">刷新</el-button>
            <el-button size="small" link type="primary" @click="openAdopt(row)">采纳</el-button>
          </template>
        </el-table-column>
        <template #empty>
          <div class="empty">
            还没有资源。点右上角「采集一次」跑一批发现任务，或用「手动添加」粘一个链接进来。
          </div>
        </template>
      </el-table>

      <el-pagination
        class="table-gap"
        layout="prev, pager, next, total"
        :total="total"
        :page-size="filters.limit"
        :current-page="Math.floor(filters.offset / filters.limit) + 1"
        @current-change="
          (page) => {
            filters.offset = (page - 1) * filters.limit;
            load();
          }
        "
      />
    </el-card>

    <el-drawer v-model="drawerVisible" title="资源详情" size="620px">
      <div v-loading="detailLoading" class="detail">
        <template v-if="detail">
          <div class="detail-head">
            <h3>{{ detail.name }}</h3>
            <div class="card-hint">
              {{ detail.username ? `@${detail.username}` : "无公开用户名" }}
              · {{ detail.tg_id ?? "ID 待解析" }}
              · 状态 {{ statusLabel[detail.status] || detail.status }}
            </div>
            <div class="detail-actions">
              <el-button size="small" @click="refreshOne(detail)">刷新</el-button>
              <el-button size="small" @click="openAdopt(detail)">采纳</el-button>
              <el-button size="small" @click="toggleFavorite(detail, !detail.is_favorite)">
                {{ detail.is_favorite ? "取消收藏" : "收藏" }}
              </el-button>
              <el-button size="small" type="danger" plain @click="toggleBlacklist(detail)">
                {{ detail.is_blacklisted ? "移出黑名单" : "加入黑名单" }}
              </el-button>
            </div>
          </div>

          <el-descriptions :column="2" border size="small" class="detail-block">
            <el-descriptions-item label="成员数">
              {{ fmtCount(detail.member_count) }}
              <span v-if="detail.member_count_approx">（近似）</span>
            </el-descriptions-item>
            <el-descriptions-item label="人数采集时间">
              {{ fmtTime(detail.member_count_at) }}
            </el-descriptions-item>
            <el-descriptions-item label="真人活跃度">{{ fmtScore(detail.activity_score) }}</el-descriptions-item>
            <el-descriptions-item label="真人发言占比">
              {{ detail.human_ratio === null ? "-" : `${Math.round(detail.human_ratio * 100)}%` }}
            </el-descriptions-item>
            <el-descriptions-item label="独立发言人数">{{ detail.unique_senders ?? "-" }}</el-descriptions-item>
            <el-descriptions-item label="日均发帖">{{ detail.posts_per_day ?? "-" }}</el-descriptions-item>
            <el-descriptions-item label="链接密度">{{ detail.link_density ?? "-" }} / 百条</el-descriptions-item>
            <el-descriptions-item label="线索潜力">{{ fmtScore(detail.lead_potential) }}</el-descriptions-item>
            <el-descriptions-item label="语言">{{ detail.language || "-" }}</el-descriptions-item>
            <el-descriptions-item label="国家">{{ detail.country || "-" }}</el-descriptions-item>
            <el-descriptions-item label="行业" :span="2">
              {{ (detail.categories || []).join("、") || "-" }}
              <span v-if="(detail.manual_locked || []).includes('categories')" class="card-hint">
                （人工锁定）
              </span>
            </el-descriptions-item>
            <el-descriptions-item label="索引型" :span="2">
              {{ detail.is_index_group ? `是（命中 ${detail.index_score} 项特征）` : `否（命中 ${detail.index_score ?? 0} 项）` }}
            </el-descriptions-item>
            <el-descriptions-item label="来源路径" :span="2">
              {{ detail.discovered_from || "-" }}
              <span class="card-hint">（{{ detail.discovered_by || "未知" }}）</span>
            </el-descriptions-item>
            <el-descriptions-item label="最后活跃">{{ fmtTime(detail.last_active_at) }}</el-descriptions-item>
            <el-descriptions-item label="最后探测">{{ fmtTime(detail.last_probed_at) }}</el-descriptions-item>
            <el-descriptions-item label="下次刷新">{{ fmtTime(detail.next_refresh_at) }}</el-descriptions-item>
            <el-descriptions-item label="采纳">
              {{ detail.adopted_by ? `${detail.adopted_by} · ${fmtTime(detail.adopted_at)}` : "-" }}
            </el-descriptions-item>
          </el-descriptions>

          <div v-if="detail.about" class="detail-block">
            <h4>简介</h4>
            <p class="card-hint">{{ detail.about }}</p>
          </div>

          <div v-if="(detail.history || []).length >= 2" class="detail-block">
            <h4>趋势（探测历史）</h4>
            <svg viewBox="0 0 100 30" class="spark">
              <path :d="sparkPath(detail.history.map((item) => item.member_count))" class="line-members" />
              <path :d="sparkPath(detail.history.map((item) => item.activity_score))" class="line-activity" />
            </svg>
            <p class="card-hint">
              蓝线＝成员数，绿线＝真人活跃度，共 {{ detail.history.length }} 次探测。
            </p>
          </div>

          <div class="detail-block">
            <h4>样本消息（最近 {{ (detail.samples || []).length }} 条）</h4>
            <el-table v-if="(detail.samples || []).length" :data="detail.samples" size="small" border>
              <el-table-column label="时间" width="140">
                <template #default="{ row }">{{ fmtTime(row.date) }}</template>
              </el-table-column>
              <el-table-column label="发送者" width="110">
                <template #default="{ row }">
                  {{ row.sender_username ? `@${row.sender_username}` : row.sender_id }}
                  <el-tag v-if="row.is_bot" size="small" class="tag-gap">机器人</el-tag>
                </template>
              </el-table-column>
              <el-table-column label="内容" min-width="240">
                <template #default="{ row }">
                  <div>{{ row.text }}</div>
                  <div v-if="row.hits?.length" class="card-hint">命中：{{ row.hits.join("、") }}</div>
                </template>
              </el-table-column>
            </el-table>
            <p v-else class="card-hint">
              还没有样本——样本在探测时顺带落库，点一次「刷新」就有了。
            </p>
          </div>
        </template>
      </div>
    </el-drawer>

    <el-dialog v-model="importVisible" title="手动添加资源" width="560px">
      <el-input
        v-model="importForm.text"
        type="textarea"
        :rows="5"
        placeholder="一行一个：t.me/xxx、@username、t.me/+邀请码、数字 ID"
      />
      <div class="import-options">
        <el-checkbox v-model="importForm.join">允许执行账号加入私密群（邀请链接必须勾选）</el-checkbox>
        <el-checkbox v-model="importForm.probe">入库后立即探测一次</el-checkbox>
      </div>
      <div class="import-options">
        <span class="card-hint">
          用哪个账号
          <FieldHelp v-bind="RESOURCE_HELP.account" />
          ：默认账号。要换采集号，到「账号与机器人」里设默认。
        </span>
      </div>
      <el-alert
        v-if="importResult"
        class="panel-gap"
        type="info"
        :closable="false"
        show-icon
        :title="`成功 ${importResult.added.length} 条，失败 ${importResult.failures.length} 条`"
      >
        <div v-for="item in importResult.failures" :key="item.input" class="card-hint">
          {{ item.input }} —— {{ item.reason }}
        </div>
      </el-alert>
      <template #footer>
        <el-button @click="importVisible = false">关闭</el-button>
        <el-button type="primary" :loading="importSaving" @click="confirmImport">开始处理</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="adoptVisible" title="采纳资源" width="520px">
      <div v-if="adoptRow" class="adopt-body">
        <p>
          把 <b>{{ adoptRow.name }}</b> 写进「监听源」。
        </p>
        <el-checkbox v-model="adoptForm.create_route">顺手建一条线路</el-checkbox>
        <template v-if="adoptForm.create_route">
          <el-radio-group v-model="adoptForm.business_type" class="adopt-row">
            <el-radio value="A">A 线（搬运帖子）</el-radio>
            <el-radio value="B">B 线（监听会员）</el-radio>
          </el-radio-group>
          <el-select
            v-model="adoptForm.target_chat_ids"
            multiple
            placeholder="选接收组（可多选）"
            style="width: 100%"
            class="adopt-row"
          >
            <el-option
              v-for="item in targets"
              :key="item.id"
              :label="`${item.name}（${item.target_role_label}）`"
              :value="item.id"
            />
          </el-select>
          <el-input v-model="adoptForm.route_name" placeholder="线路名（留空自动生成）" class="adopt-row" />
        </template>
        <p class="card-hint">
          采纳后系统会记录采纳时间与操作人；如果这个群还没确认在账号的群里，会自动按限速排一次加群。
        </p>
      </div>
      <template #footer>
        <el-button @click="adoptVisible = false">取消</el-button>
        <el-button type="primary" :loading="adoptSaving" @click="confirmAdopt">确认采纳</el-button>
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

.filter-label {
  display: inline-flex;
  align-items: center;
  font-size: 13px;
  color: var(--tg-muted);
}

.bulk {
  display: flex;
  gap: 8px;
  align-items: center;
  margin-top: 10px;
}

.table-gap {
  margin-top: 10px;
}

.panel-gap {
  margin-top: 12px;
}

.tag-gap {
  margin-left: 4px;
}

.empty {
  padding: 24px 0;
  color: var(--tg-muted);
}

.detail h3 {
  margin: 0 0 4px;
  font-size: 16px;
}

.detail-head {
  padding-bottom: 10px;
  border-bottom: 1px solid var(--tg-border);
}

.detail-actions {
  display: flex;
  gap: 6px;
  margin-top: 8px;
  flex-wrap: wrap;
}

.detail-block {
  margin-top: 14px;
}

.detail-block h4 {
  margin: 0 0 6px;
  font-size: 14px;
}

.spark {
  width: 100%;
  height: 90px;
  background: #fafbfc;
  border: 1px solid var(--tg-border);
  border-radius: 6px;
}

.spark path {
  fill: none;
  stroke-width: 1.2;
}

.line-members {
  stroke: #409eff;
}

.line-activity {
  stroke: #67c23a;
}

.import-options {
  margin-top: 8px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.adopt-body p {
  margin: 0 0 8px;
  line-height: 1.7;
}

.adopt-row {
  margin-top: 8px;
}
</style>
