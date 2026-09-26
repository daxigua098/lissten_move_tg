<script setup>
import { ElMessage } from "element-plus";
import { computed, onMounted, reactive, ref } from "vue";

import { resourcesApi } from "../api";
import FieldHelp from "../components/FieldHelp.vue";
import { RESOURCE_HELP } from "../resourceHelp";

const loading = ref(false);
const overview = ref(null);
const runs = ref([]);
const syncing = ref(false);

/** 每个站点各自的范围勾选与页数上限。 */
const form = reactive({
  combot: { scopes: ["zh"], maxPages: 24, resume: true },
  tgme: { scopes: ["车友"], maxPages: 1, resume: false },
});

const COMBOT_SCOPES = [
  { value: "zh", label: "中文群榜（2320 个 / 24 页）" },
  { value: "global", label: "全球群榜（38079 个 / 381 页）" },
  { value: "channels", label: "频道榜（19747 个 / 198 页）" },
];

const RESULT_LABEL = { ok: "完成", partial: "部分完成", failed: "失败" };
const RESULT_TAG = { ok: "success", partial: "warning", failed: "danger" };

const daily = computed(() => overview.value?.daily_requests || {});

function fmtTime(value) {
  return value ? value.replace("T", " ").slice(0, 16) : "-";
}

function sitePayload(source) {
  return (overview.value?.sites || []).find((item) => item.source === source);
}

async function load() {
  loading.value = true;
  try {
    const [sources, history] = await Promise.all([
      resourcesApi.directorySources(),
      resourcesApi.directoryRuns({ limit: 30 }),
    ]);
    overview.value = sources.data;
    runs.value = history.data.items || [];
  } catch (error) {
    ElMessage.error(error.message);
  } finally {
    loading.value = false;
  }
}

async function sync(source) {
  const config = form[source];
  const scopes = (config.scopes || []).filter(Boolean);
  if (!scopes.length) {
    ElMessage.warning("先选一个范围");
    return;
  }
  syncing.value = true;
  try {
    let added = 0;
    let seen = 0;
    for (const scope of scopes) {
      const { data } = await resourcesApi.directorySync({
        source,
        scope,
        max_pages: config.maxPages || null,
        resume: config.resume,
      });
      if (data.result === "skipped") {
        ElMessage.info(data.reason === "quota" ? "今日目录请求额度已用完" : "目录渠道已停用");
        break;
      }
      const run = data.run || {};
      added += run.items_added || 0;
      seen += run.items_seen || 0;
      if (run.error) ElMessage.warning(`${scope}：${run.error}`);
    }
    ElMessage.success(`同步完成：看到 ${seen} 条，新增 ${added} 条`);
    await load();
  } catch (error) {
    ElMessage.error(error.message);
  } finally {
    syncing.value = false;
  }
}

async function createTask(source) {
  const config = form[source];
  const scope = (config.scopes || [])[0];
  if (!scope) {
    ElMessage.warning("先选一个范围");
    return;
  }
  try {
    await resourcesApi.createDirectoryTask({
      source,
      scope,
    });
    ElMessage.success(`已建立每 24 小时自动同步的任务（${source} / ${scope}）`);
  } catch (error) {
    ElMessage.error(error.message);
  }
}

onMounted(load);
</script>

<template>
  <div v-loading="loading">
    <div class="toolbar">
      <h2 class="page-title">目录同步</h2>
      <span class="card-hint">
        三方目录只负责「发现」：拉回来的是候选线索，采纳前仍要走我们自己的探测。
      </span>
      <div class="spacer" />
      <span class="card-hint">
        今日目录请求 {{ overview?.daily_requests_used ?? 0 }} /
        {{ overview?.daily_requests_limit ?? 0 }}
      </span>
      <el-button size="small" @click="load">刷新</el-button>
    </div>

    <el-alert
      class="panel-gap"
      type="info"
      :closable="false"
      show-icon
      title="两个站点都只用公开页面/接口，遇到人机校验就停下并记录，不做绕过"
      description="combot 走它公开的目录接口（带 Telegram 数字 ID）；tg-me 走列表页，主站失败会自动换 tgoop 镜像。"
    />

    <el-row :gutter="12" class="panel-gap">
      <el-col :xs="24" :md="12">
        <el-card shadow="never" class="site-card">
          <template #header>
            <div class="card-head">
              <span>Combot 目录</span>
              <el-tag size="small" :type="sitePayload('combot')?.enabled ? 'success' : 'info'">
                {{ sitePayload('combot')?.enabled ? "可用" : "未启用" }}
              </el-tag>
            </div>
          </template>
          <el-checkbox-group v-model="form.combot.scopes">
            <el-checkbox v-for="item in COMBOT_SCOPES" :key="item.value" :value="item.value">
              {{ item.label }}
            </el-checkbox>
          </el-checkbox-group>
          <div class="row">
            <span class="card-hint">最多抓</span>
            <el-input-number v-model="form.combot.maxPages" size="small" :min="1" :max="500" />
            <span class="card-hint">页</span>
            <el-checkbox v-model="form.combot.resume">从上次断点继续</el-checkbox>
          </div>
          <div class="row">
            <el-button
              size="small"
              type="primary"
              :loading="syncing"
              @click="sync('combot')"
            >
              同步一次
            </el-button>
            <el-button size="small" @click="createTask('combot')">设为每 24 小时自动同步</el-button>
          </div>
          <div class="card-hint">
            上次同步：
            <template v-for="item in sitePayload('combot')?.scopes || []" :key="item.scope">
              {{ item.scope }}
              {{ item.last_run ? `${fmtTime(item.last_run.started_at)}（新增 ${item.last_run.items_added}）` : "还没同步过" }}；
            </template>
          </div>
        </el-card>
      </el-col>

      <el-col :xs="24" :md="12">
        <el-card shadow="never" class="site-card">
          <template #header>
            <div class="card-head">
              <span>tg-me 列表页</span>
              <el-tag size="small" :type="sitePayload('tgme')?.enabled ? 'success' : 'info'">
                {{ sitePayload('tgme')?.enabled ? "可用" : "未启用" }}
              </el-tag>
            </div>
          </template>
          <div class="row">
            <span class="card-hint">关键词（可多选，如「车友」「招聘」）</span>
          </div>
          <el-select
            v-model="form.tgme.scopes"
            multiple
            filterable
            allow-create
            default-first-option
            placeholder="输入关键词后回车"
            style="width: 100%"
          >
            <el-option v-for="item in ['车友', '招聘', '兼职', '加密货币']" :key="item" :label="item" :value="item" />
          </el-select>
          <div class="row">
            <span class="card-hint">最多抓</span>
            <el-input-number v-model="form.tgme.maxPages" size="small" :min="1" :max="30" />
            <span class="card-hint">页（每页 100 条）</span>
          </div>
          <div class="row">
            <el-button size="small" type="primary" :loading="syncing" @click="sync('tgme')">
              同步一次
            </el-button>
          </div>
          <div class="card-hint">
            tg-me 不给数字 ID，这批资源入库后要先探测一次才能采纳。
          </div>
        </el-card>
      </el-col>
    </el-row>

    <el-card shadow="never" class="panel-gap">
      <template #header>
        <div class="card-head">
          <span>同步历史</span>
          <span class="card-hint">中断过的记录会在卡片上标出「可续抓」</span>
          <div class="spacer" />
          <span class="card-hint">
            请求额度
            <FieldHelp v-bind="RESOURCE_HELP.onlineSearch" />
          </span>
        </div>
      </template>
      <el-table :data="runs" size="small" border>
        <el-table-column label="时间" width="150">
          <template #default="{ row }">{{ fmtTime(row.started_at) }}</template>
        </el-table-column>
        <el-table-column label="站点" width="100">
          <template #default="{ row }">{{ row.source }}</template>
        </el-table-column>
        <el-table-column label="范围" width="110">
          <template #default="{ row }">{{ row.scope }}</template>
        </el-table-column>
        <el-table-column label="页数" width="120">
          <template #default="{ row }">
            {{ row.pages_done }}{{ row.pages_total ? ` / ${row.pages_total}` : "" }}
          </template>
        </el-table-column>
        <el-table-column label="看到" width="90">
          <template #default="{ row }">{{ row.items_seen }}</template>
        </el-table-column>
        <el-table-column label="新增" width="90">
          <template #default="{ row }">{{ row.items_added }}</template>
        </el-table-column>
        <el-table-column label="请求" width="80">
          <template #default="{ row }">{{ row.requests_used }}</template>
        </el-table-column>
        <el-table-column label="结果" width="110">
          <template #default="{ row }">
            <el-tag size="small" :type="RESULT_TAG[row.result]">
              {{ RESULT_LABEL[row.result] || row.result }}
            </el-tag>
            <el-tag v-if="row.resumable" size="small" class="tag-gap">可续抓</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="说明" min-width="200">
          <template #default="{ row }">
            <span class="card-hint">{{ row.error || "-" }}</span>
          </template>
        </el-table-column>
        <template #empty>
          <div class="empty">
            还没有同步记录。上面的「同步一次」会把目录拉进资源库的候选池。
          </div>
        </template>
      </el-table>
    </el-card>

    <el-alert
      class="panel-gap"
      type="warning"
      :closable="false"
      show-icon
      title="目录数据只是候选"
      description="三方站的成员数是它们自己的口径，我们只存进「目录成员数」字段作参考，界面标近似；采纳前一定会用自己的账号探测一次。"
    />
  </div>
</template>

<style scoped>
.site-card {
  height: 100%;
}

.card-head {
  display: flex;
  align-items: center;
  gap: 8px;
}

.row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 10px;
  flex-wrap: wrap;
}

.panel-gap {
  margin-top: 12px;
}

.tag-gap {
  margin-left: 4px;
}

.empty {
  padding: 20px 0;
  color: var(--tg-muted);
}
</style>
