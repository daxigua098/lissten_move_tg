<script setup>
import { ElMessage, ElMessageBox } from "element-plus";
import { computed, onMounted, reactive, ref } from "vue";

import { hotKeywordsApi, resourcesApi, targetsApi } from "../api";
import FieldHelp from "../components/FieldHelp.vue";
import { RESOURCE_HELP } from "../resourceHelp";

const loading = ref(false);
const rows = ref([]);
const total = ref(0);
const selection = ref([]);
const overview = ref(null);
const facets = ref({ languages: [], categories: [], chat_types: [], sorts: [], freshness: [] });
const counts = ref(null);
const hotWords = ref([]);
const targets = ref([]);

/** 视图：卡片墙（默认）/ 表格（批量操作用）。 */
const view = ref("cards");
/** 快捷榜：active / potential / new / due / adopted。 */
const board = ref("");
/** 高级筛选面板是否展开。 */
const advanced = ref(false);
const onlineEnabled = ref(true);
const onlineRunning = ref(false);
const onlineResult = ref(null);
const sensitiveVisible = ref(false);

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
  status: "",
  source_site: "",
  due_refresh: false,
  sort: "activity",
  limit: 24,
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

const BOARDS = [
  { value: "active", label: "最活跃", hint: "按真人活跃度排序" },
  { value: "potential", label: "潜力最高", hint: "按线索潜力排序" },
  { value: "new", label: "新发现", hint: "刚进候选池、还没探测" },
  { value: "due", label: "该刷新", hint: "到了该重新探测的时间" },
  { value: "adopted", label: "已采纳", hint: "已经变成监听源" },
];

const SOURCE_LABEL = {
  telegram: "Telegram 搜索",
  combot: "Combot 目录",
  tgme: "tg-me 列表",
  manual: "人工添加",
};
const RATING_LABEL = { normal: "常规", sensitive: "敏感", unknown: "未判定" };
const RATING_TAG = { normal: "success", sensitive: "danger", unknown: "info" };
const CHAT_TYPE_LABEL = { channel: "频道", supergroup: "超级群", group: "群组" };

const queryParams = computed(() => {
  const params = {};
  Object.entries(filters).forEach(([key, value]) => {
    if (value === null || value === undefined || value === "" || value === false) return;
    if (Array.isArray(value) && value.length === 0) return;
    if (key === "limit") return;
    params[key] = value;
  });
  // 卡片墙默认藏敏感内容；表格视图是批量操作视图，始终包含并标注
  params.include_sensitive = sensitiveVisible.value || view.value === "table";
  return params;
});

const quick = computed(() => counts.value?.quick || {});
const runtimeRunning = computed(() => overview.value?.runtime?.running ?? true);

const JOIN_LABEL = {
  pending: "待加入",
  running: "加入中",
  success: "已加入",
  waiting_approval: "待审批",
  failed: "加入失败",
};
const JOIN_TAG = {
  pending: "warning",
  running: "warning",
  success: "success",
  waiting_approval: "warning",
  failed: "danger",
};

/** 账号到底进群没有：探测确认过，或加群任务成功。 */
function isJoined(row) {
  return row.resource_state === "active" || row.join?.status === "success";
}

function joinState(row) {
  if (isJoined(row)) return { label: "已在群里", type: "success" };
  if (!row.join) return { label: "未加入", type: "info" };
  return {
    label: JOIN_LABEL[row.join.status] || row.join.status,
    type: JOIN_TAG[row.join.status] || "info",
  };
}

/** 已采纳却没进群：链路建好了也收不到数据，必须显式提醒。 */
function silentRisk(row) {
  return row.status === "adopted" && !isJoined(row);
}

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

function avatarText(row) {
  const name = (row.name || "").trim();
  return name ? name.slice(0, 1) : "#";
}

function freshnessTag(value) {
  return { fresh: "success", warm: "warning", stale: "info", new: "" }[value] || "";
}

function freshnessLabel(value) {
  return { fresh: "24 小时内", warm: "7 天内", stale: "超过 7 天", new: "未探测" }[value] || "";
}

/** 卡片上的来源一句话：谁带来的、什么时候。 */
function sourceText(row) {
  const site = SOURCE_LABEL[row.source_site] || "未标注来源";
  return row.discovered_from ? `${site} · ${row.discovered_from}` : site;
}

async function load() {
  loading.value = true;
  try {
    const [list, cnt, stat] = await Promise.all([
      resourcesApi.list({ ...queryParams.value, limit: filters.limit, offset: filters.offset }),
      resourcesApi.counts(),
      resourcesApi.overview(),
    ]);
    rows.value = list.data.items;
    total.value = list.data.total;
    counts.value = cnt.data;
    overview.value = stat.data;
  } catch (error) {
    ElMessage.error(error.message);
  } finally {
    loading.value = false;
  }
}

async function loadHotWords() {
  try {
    const { data } = await hotKeywordsApi.list({ limit: 12, min_count: 1 });
    hotWords.value = data.items || [];
  } catch {
    hotWords.value = [];
  }
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
    status: "",
    source_site: "",
    due_refresh: false,
    sort: "activity",
    offset: 0,
  });
  board.value = "";
  load();
}

/** 筛选条件变了就回到第一页。 */
function applyFilters() {
  filters.offset = 0;
  load();
}

function toggleLanguage(value) {
  filters.languages = filters.languages.includes(value)
    ? filters.languages.filter((item) => item !== value)
    : [...filters.languages, value];
  applyFilters();
}

function toggleCategory(value) {
  filters.categories = filters.categories.includes(value)
    ? filters.categories.filter((item) => item !== value)
    : [...filters.categories, value];
  applyFilters();
}

function selectBoard(value) {
  board.value = board.value === value ? "" : value;
  Object.assign(filters, {
    sort: "activity",
    status: "",
    due_refresh: false,
    adopted: null,
  });
  if (board.value === "active") filters.sort = "activity";
  if (board.value === "potential") filters.sort = "potential";
  if (board.value === "new") {
    filters.sort = "recent_found";
    filters.status = "candidate";
  }
  if (board.value === "due") filters.due_refresh = true;
  if (board.value === "adopted") filters.adopted = true;
  applyFilters();
}

/** 一次搜索＝本地先出结果 + 可选在线补搜追加（F-R19）。 */
async function search({ online = onlineEnabled.value } = {}) {
  filters.offset = 0;
  onlineResult.value = null;
  await load();
  const keyword = filters.keyword.trim();
  if (!online || !keyword) return;
  onlineRunning.value = true;
  try {
    const { data } = await resourcesApi.discoverOnline({ keywords: [keyword] });
    onlineResult.value = data;
    const added = data.new_total || 0;
    if (added) {
      ElMessage.success(`在线补搜新增 ${added} 条`);
    } else {
      ElMessage.info("在线补搜没有新发现");
    }
    await load();
  } catch (error) {
    ElMessage.warning(`在线补搜失败：${error.message}`);
  } finally {
    onlineRunning.value = false;
  }
}

function searchHotWord(token) {
  filters.keyword = token;
  search();
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
    ElMessage.success(
      `刷新完成：成功 ${data.succeeded?.length || 0} 条${failed ? `，失败 ${failed} 条` : ""}`,
    );
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

/** 卡片/详情上的「让账号加入」：排进限速队列。 */
async function enqueueJoinOne(row) {
  try {
    const { data } = await resourcesApi.join({ ids: [row.id] });
    if (data.failures?.length) {
      ElMessage.error(data.failures[0].reason);
    } else {
      ElMessage.success("已排入加群队列（按限速执行）");
      if (!runtimeRunning.value) {
        ElMessage.warning("运行时没在跑：队列要等它在「运行总览」启动后才会执行");
      }
    }
    await load();
    if (drawerVisible.value && detail.value?.id === row.id) await openDetail(row);
  } catch (error) {
    ElMessage.error(error.message);
  }
}

async function copyLink(row) {
  const link = row.link;
  if (!link) {
    ElMessage.warning("这条资源还没有公开链接");
    return;
  }
  try {
    await navigator.clipboard.writeText(link);
    ElMessage.success("链接已复制");
  } catch {
    ElMessage.info(link);
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

async function markRating(row, value) {
  try {
    await resourcesApi.update(row.id, { content_rating: value });
    ElMessage.success(`已标记为${RATING_LABEL[value]}`);
    await load();
    if (drawerVisible.value && detail.value?.id === row.id) await openDetail(row);
  } catch (error) {
    ElMessage.error(error.message);
  }
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
  window.open(resourcesApi.exportUrl({ ...queryParams.value, include_sensitive: true }), "_blank");
}

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

onMounted(async () => {
  onlineEnabled.value = true;
  await Promise.all([load(), loadHotWords()]);
});
</script>

<template>
  <div v-loading="loading">
    <!-- 顶部：目录站式 hero 搜索 -->
    <div class="hero">
      <div class="hero-title">资源发现</div>
      <div class="hero-sub">
        搜群、挑群、采纳成监听源。列表只读本地库，点「搜一下 / 刷新」才会访问外面。
      </div>
      <div class="hero-search">
        <el-input
          v-model="filters.keyword"
          size="large"
          placeholder="搜群 / 频道：输入关键词回车，先出本地结果，再补搜外部目录"
          clearable
          @keyup.enter="search()"
        >
          <template #prefix>
            <span class="hero-icon">🔍</span>
          </template>
        </el-input>
        <el-button size="large" type="primary" :loading="onlineRunning" @click="search()">
          搜一下
        </el-button>
      </div>
      <div class="hero-options">
        <el-checkbox v-model="onlineEnabled">
          同时在线补搜
          <FieldHelp v-bind="RESOURCE_HELP.onlineSearch" />
        </el-checkbox>
        <div class="spacer" />
        <el-button size="small" @click="openImport">手动添加</el-button>
        <el-button size="small" @click="exportCsv">导出</el-button>
      </div>

      <div v-if="hotWords.length" class="hero-hot">
        <span class="card-hint">热门词：</span>
        <el-button
          v-for="item in hotWords"
          :key="item.token"
          size="small"
          round
          @click="searchHotWord(item.token)"
        >
          {{ item.token }}
        </el-button>
      </div>

      <div v-if="onlineRunning || onlineResult" class="hero-online">
        <el-tag v-if="onlineRunning" size="small" type="warning">正在补搜…</el-tag>
        <template v-else>
          <el-tag
            v-for="item in onlineResult.results"
            :key="item.site"
            size="small"
            :type="item.status === 'error' ? 'danger' : item.status === 'local' ? 'info' : 'success'"
          >
            {{ SOURCE_LABEL[item.site] || item.site }}：
            <template v-if="item.status === 'error'">失败（{{ item.error }}）</template>
            <template v-else-if="item.status === 'local'">
              本地命中 {{ item.hits }} 条
            </template>
            <template v-else>发现 {{ item.hits }} 条，新增 {{ item.new_resources }} 条</template>
          </el-tag>
        </template>
      </div>

      <el-alert
        v-if="!runtimeRunning"
        class="hero-warn"
        type="warning"
        :closable="false"
        show-icon
        title="运行时没在跑：加群与探测都不会执行"
        description="排进队列的加群会一直等着。到「运行总览」启动运行时，队列才会按限速执行、才真的能监听到数据。"
      />
    </div>

    <!-- 分类 chips -->
    <div class="chips">
      <span class="chips-label">语言</span>
      <el-tag
        v-for="item in counts?.languages || []"
        :key="`lang-${item.value}`"
        class="chip"
        :effect="filters.languages.includes(item.value) ? 'dark' : 'plain'"
        @click="toggleLanguage(item.value)"
      >
        {{ item.label }} {{ item.count }}
      </el-tag>
      <span class="chips-label">行业</span>
      <el-tag
        v-for="item in counts?.categories || []"
        :key="`cat-${item.value}`"
        class="chip"
        :effect="filters.categories.includes(item.value) ? 'dark' : 'plain'"
        @click="toggleCategory(item.value)"
      >
        {{ item.label }} {{ item.count }}
      </el-tag>
      <span v-if="!(counts?.languages || []).length" class="card-hint">
        还没有数据——去「目录同步」拉一批，或用上面的搜索框搜一个词。
      </span>
    </div>

    <!-- 快捷榜 + 视图切换 -->
    <div class="board">
      <el-button
        v-for="item in BOARDS"
        :key="item.value"
        size="small"
        :type="board === item.value ? 'primary' : 'default'"
        @click="selectBoard(item.value)"
      >
        {{ item.label }}
        <span class="board-count">{{ quick[item.value] ?? 0 }}</span>
      </el-button>
      <div class="spacer" />
      <span class="card-hint">
        显示敏感内容
        <FieldHelp v-bind="RESOURCE_HELP.contentFilter" />
      </span>
      <el-switch v-model="sensitiveVisible" size="small" @change="applyFilters" />
      <el-radio-group v-model="view" size="small" class="view-switch">
        <el-radio-button value="cards">卡片</el-radio-button>
        <el-radio-button value="table">表格</el-radio-button>
      </el-radio-group>
    </div>

    <!-- 高级筛选（默认收起） -->
    <el-collapse v-model="advanced" class="advanced">
      <el-collapse-item name="filters" title="高级筛选（成员数 / 活跃度 / 索引型 / 新鲜度 / 排序）">
        <div class="filters">
          <el-select v-model="filters.chat_type" size="small" clearable placeholder="类型" style="width: 110px">
            <el-option
              v-for="item in facets.chat_types"
              :key="item.value"
              :label="item.label"
              :value="item.value"
            />
          </el-select>
          <el-select
            v-model="filters.source_site"
            size="small"
            clearable
            placeholder="来源站点"
            style="width: 140px"
          >
            <el-option label="Combot 目录" value="combot" />
            <el-option label="tg-me 列表" value="tgme" />
            <el-option label="Telegram 搜索" value="telegram" />
            <el-option label="人工添加" value="manual" />
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
          <el-select v-model="filters.sort" size="small" placeholder="排序" style="width: 140px">
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
          <el-button size="small" type="primary" @click="applyFilters">筛选</el-button>
          <el-button size="small" @click="resetFilters">重置</el-button>
        </div>
      </el-collapse-item>
    </el-collapse>

    <!-- 卡片墙 -->
    <div v-if="view === 'cards'" class="cards">
      <el-card v-for="row in rows" :key="row.id" shadow="hover" class="resource-card">
        <div class="card-top">
          <div class="avatar">{{ avatarText(row) }}</div>
          <div class="card-title">
            <el-button link type="primary" class="card-name" @click="openDetail(row)">
              {{ row.name }}
            </el-button>
            <div class="card-meta">
              {{ CHAT_TYPE_LABEL[row.chat_type] || row.chat_type }}
              · {{ fmtCount(row.member_count) }}
              <span v-if="row.member_count_approx && row.member_count">(近似)</span>
              <span v-if="row.directory_member_count" class="card-hint">
                · 目录 {{ fmtCount(row.directory_member_count) }}
              </span>
            </div>
          </div>
        </div>
        <div class="card-about">{{ row.about || "（还没有简介——探测一次就有了）" }}</div>
        <div class="card-metrics">
          <span>活跃 <b>{{ fmtScore(row.activity_score) }}</b></span>
          <span>潜力 <b>{{ fmtScore(row.lead_potential) }}</b></span>
          <span>{{ row.language || "语言未知" }}</span>
        </div>
        <div class="card-tags">
          <el-tag v-if="row.freshness" size="small" :type="freshnessTag(row.freshness)">
            {{ freshnessLabel(row.freshness) }}
          </el-tag>
          <el-tag v-if="row.is_index_group" size="small" type="success">索引</el-tag>
          <el-tag v-if="row.status === 'adopted'" size="small" type="warning">已采纳</el-tag>
          <el-tag v-if="row.content_rating === 'sensitive'" size="small" type="danger">敏感</el-tag>
          <el-tag v-if="row.is_favorite" size="small">收藏</el-tag>
          <el-tag v-for="item in (row.categories || []).slice(0, 3)" :key="item" size="small">
            {{ item }}
          </el-tag>
        </div>
        <!-- 公开链接 + 加群状态：点链接就是 Telegram 的"加入群组/频道"提示 -->
        <div class="card-link">
          <a
            v-if="row.link"
            :href="row.link"
            target="_blank"
            rel="noopener"
            class="link-a"
            :title="row.link"
          >
            {{ row.username ? `@${row.username}` : "邀请链接" }}
          </a>
          <el-button v-if="row.link" size="small" link @click="copyLink(row)">复制链接</el-button>
          <span v-else class="card-hint">还没有公开链接</span>
          <el-tag size="small" :type="joinState(row).type">{{ joinState(row).label }}</el-tag>
          <el-tag v-if="silentRisk(row)" size="small" type="danger">账号没进群，收不到数据</el-tag>
        </div>
        <div class="card-source">{{ sourceText(row) }}</div>
        <div class="card-actions">
          <el-button size="small" link type="primary" @click="openDetail(row)">详情</el-button>
          <el-button size="small" link type="primary" @click="refreshOne(row)">刷新</el-button>
          <el-button size="small" link type="primary" @click="openAdopt(row)">采纳</el-button>
          <el-button
            v-if="!isJoined(row)"
            size="small"
            link
            type="warning"
            @click="enqueueJoinOne(row)"
          >
            让账号加入
          </el-button>
          <el-button size="small" link @click="toggleFavorite(row, !row.is_favorite)">
            {{ row.is_favorite ? "取消收藏" : "收藏" }}
          </el-button>
        </div>
      </el-card>
      <div v-if="!rows.length" class="empty">
        还没有资源。用上面的搜索框搜一个词，或去「目录同步」拉一批目录进来。
      </div>
    </div>

    <!-- 表格视图：批量操作 -->
    <el-card v-else shadow="never" class="table-view">
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
        <span class="card-hint">表格视图始终包含敏感内容（带标签），导出同理。</span>
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
              v-if="row.content_rating === 'sensitive'"
              size="small"
              type="danger"
              class="tag-gap"
            >
              敏感
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
          </template>
        </el-table-column>
        <el-table-column label="来源" width="140">
          <template #default="{ row }">
            {{ SOURCE_LABEL[row.source_site] || "-" }}
            <span v-if="row.directory_rank" class="card-hint">#{{ row.directory_rank }}</span>
          </template>
        </el-table-column>
        <el-table-column label="公开链接" width="120">
          <template #default="{ row }">
            <a v-if="row.link" :href="row.link" target="_blank" rel="noopener" class="link-a">
              打开
            </a>
            <span v-else class="card-hint">-</span>
          </template>
        </el-table-column>
        <el-table-column label="加群" width="120">
          <template #default="{ row }">
            <el-tag size="small" :type="joinState(row).type">{{ joinState(row).label }}</el-tag>
            <el-tag v-if="silentRisk(row)" size="small" type="danger" class="tag-gap">收不到</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="类型" width="80">
          <template #default="{ row }">
            {{ CHAT_TYPE_LABEL[row.chat_type] || row.chat_type }}
          </template>
        </el-table-column>
        <el-table-column label="成员" width="110">
          <template #default="{ row }">
            {{ fmtCount(row.member_count) }}
            <span v-if="row.member_count_approx && row.member_count" class="card-hint">（近似）</span>
          </template>
        </el-table-column>
        <el-table-column label="目录成员" width="100">
          <template #default="{ row }">{{ fmtCount(row.directory_member_count) }}</template>
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
      </el-table>
    </el-card>

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

    <el-drawer v-model="drawerVisible" title="资源详情" size="620px">
      <div v-loading="detailLoading" class="detail">
        <template v-if="detail">
          <div class="detail-head">
            <h3>{{ detail.name }}</h3>
            <div class="card-hint">
              {{ detail.username ? `@${detail.username}` : "无公开用户名" }}
              · {{ detail.tg_id ?? "ID 待解析" }}
              · 状态 {{ statusLabel[detail.status] || detail.status }}
              · 来源 {{ SOURCE_LABEL[detail.source_site] || "未标注" }}
              <template v-if="detail.directory_rank"> · 目录第 {{ detail.directory_rank }} 名</template>
            </div>
            <div class="detail-actions">
              <el-button size="small" @click="refreshOne(detail)">刷新</el-button>
              <el-button size="small" @click="openAdopt(detail)">采纳</el-button>
              <el-button
                v-if="!isJoined(detail)"
                size="small"
                type="warning"
                plain
                @click="enqueueJoinOne(detail)"
              >
                让账号加入
              </el-button>
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
              <span v-if="detail.member_count_approx && detail.member_count">（近似）</span>
            </el-descriptions-item>
            <el-descriptions-item label="目录成员数">
              {{ fmtCount(detail.directory_member_count) }}
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
            <el-descriptions-item label="内容分级">
              <el-tag size="small" :type="RATING_TAG[detail.content_rating]">
                {{ RATING_LABEL[detail.content_rating] || detail.content_rating }}
              </el-tag>
              <el-button size="small" link type="primary" @click="markRating(detail, 'normal')">
                标为常规
              </el-button>
              <el-button size="small" link type="danger" @click="markRating(detail, 'sensitive')">
                标为敏感
              </el-button>
            </el-descriptions-item>
            <el-descriptions-item label="行业">
              {{ (detail.categories || []).join("、") || "-" }}
              <span v-if="(detail.manual_locked || []).includes('categories')" class="card-hint">
                （人工锁定）
              </span>
            </el-descriptions-item>
            <el-descriptions-item label="来源路径" :span="2">
              {{ detail.discovered_from || "-" }}
              <span class="card-hint">（{{ detail.discovered_by || "未知" }}）</span>
            </el-descriptions-item>
            <el-descriptions-item label="公开链接" :span="2">
              <template v-if="detail.link">
                <a :href="detail.link" target="_blank" rel="noopener" class="link-a">
                  {{ detail.link }}
                </a>
                <el-button size="small" link @click="copyLink(detail)">复制</el-button>
              </template>
              <span v-else class="card-hint">
                还没有公开链接——私密群需要先拿到邀请链接才能加入
              </span>
            </el-descriptions-item>
            <el-descriptions-item label="加群状态" :span="2">
              <el-tag size="small" :type="joinState(detail).type">
                {{ joinState(detail).label }}
              </el-tag>
              <span v-if="detail.join?.scheduled_at" class="card-hint">
                计划 {{ fmtTime(detail.join.scheduled_at) }}
              </span>
              <span v-if="detail.join?.last_error" class="card-hint">
                · {{ detail.join.last_error }}
              </span>
              <el-tag v-if="silentRisk(detail)" size="small" type="danger" class="tag-gap">
                账号没进群，监听不到数据
              </el-tag>
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
.hero {
  background: linear-gradient(135deg, #eef3ff 0%, #f7f9fc 60%, #f4f6fa 100%);
  border: 1px solid var(--tg-border);
  border-radius: 12px;
  padding: 18px 20px;
}

.hero-title {
  font-size: 20px;
  font-weight: 600;
}

.hero-sub {
  margin-top: 4px;
  color: var(--tg-muted);
  font-size: 13px;
}

.hero-search {
  display: flex;
  gap: 10px;
  margin-top: 14px;
}

.hero-icon {
  font-size: 15px;
}

.hero-options {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 10px;
  flex-wrap: wrap;
}

.hero-hot {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 10px;
  flex-wrap: wrap;
}

.hero-online {
  display: flex;
  gap: 6px;
  margin-top: 10px;
  flex-wrap: wrap;
}

.hero-warn {
  margin-top: 12px;
}

.chips {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 12px;
  flex-wrap: wrap;
}

.chips-label {
  color: var(--tg-muted);
  font-size: 13px;
  margin-right: 2px;
}

.chip {
  cursor: pointer;
}

.board {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 12px;
  flex-wrap: wrap;
}

.board-count {
  margin-left: 4px;
  opacity: 0.7;
  font-size: 12px;
}

.view-switch {
  margin-left: 8px;
}

.advanced {
  margin-top: 10px;
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

.cards {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
  gap: 12px;
  margin-top: 12px;
}

.resource-card {
  border-radius: 10px;
}

.card-top {
  display: flex;
  gap: 10px;
  align-items: center;
}

.avatar {
  width: 40px;
  height: 40px;
  border-radius: 50%;
  background: var(--tg-accent);
  color: #fff;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 18px;
  flex: 0 0 auto;
}

.card-title {
  min-width: 0;
}

.card-name {
  font-size: 15px;
  font-weight: 600;
  text-align: left;
  padding: 0;
}

.card-meta {
  color: var(--tg-muted);
  font-size: 12px;
  margin-top: 2px;
}

.card-about {
  margin-top: 10px;
  font-size: 13px;
  color: #475569;
  line-height: 1.6;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  min-height: 41px;
}

.card-metrics {
  display: flex;
  gap: 14px;
  margin-top: 10px;
  font-size: 13px;
  color: var(--tg-muted);
}

.card-metrics b {
  color: #0f172a;
}

.card-tags {
  display: flex;
  gap: 4px;
  margin-top: 8px;
  flex-wrap: wrap;
}

.card-source {
  margin-top: 8px;
  font-size: 12px;
  color: var(--tg-muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.card-link {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 8px;
  flex-wrap: wrap;
  font-size: 12px;
}

.link-a {
  color: var(--tg-accent);
  text-decoration: none;
  word-break: break-all;
}

.link-a:hover {
  text-decoration: underline;
}

.card-actions {
  display: flex;
  gap: 4px;
  margin-top: 6px;
  flex-wrap: wrap;
}

.table-view {
  margin-top: 12px;
}

.bulk {
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

.tag-gap {
  margin-left: 4px;
}

.empty {
  padding: 30px 0;
  color: var(--tg-muted);
  text-align: center;
  grid-column: 1 / -1;
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
