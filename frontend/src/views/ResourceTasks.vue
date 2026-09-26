<script setup>
import { ElMessage, ElMessageBox } from "element-plus";
import { onMounted, reactive, ref } from "vue";

import { resourcesApi } from "../api";
import FieldHelp from "../components/FieldHelp.vue";
import { RESOURCE_HELP } from "../resourceHelp";

const loading = ref(false);
const tasks = ref([]);
const joinTasks = ref([]);
const joinTotal = ref(0);
const quota = ref(null);
const joinFilter = reactive({ status: "", limit: 50, offset: 0 });

const createVisible = ref(false);
const createSaving = ref(false);
const createForm = reactive({ kind: "keyword", keyword: "", category: "" });

const sampleDepth = ref(100);

const KIND_LABEL = {
  keyword: "关键词",
  hotword: "热门词",
  phrase: "句式",
  link: "链接",
};

const STATUS_LABEL = {
  pending: "排队中",
  running: "执行中",
  success: "已完成",
  failed: "失败",
  waiting_approval: "待审批",
};

const STATUS_TAG = {
  pending: "info",
  running: "warning",
  success: "success",
  failed: "danger",
  waiting_approval: "warning",
};

function fmtTime(value) {
  return value ? value.replace("T", " ").slice(0, 16) : "-";
}

async function load() {
  loading.value = true;
  try {
    const [taskList, queue, board] = await Promise.all([
      resourcesApi.discoverTasks(),
      resourcesApi.joinTasks({
        status: joinFilter.status || undefined,
        limit: joinFilter.limit,
        offset: joinFilter.offset,
      }),
      resourcesApi.quota(),
    ]);
    tasks.value = taskList.data.items;
    joinTasks.value = queue.data.items;
    joinTotal.value = queue.data.total;
    quota.value = board.data;
  } catch (error) {
    ElMessage.error(error.message);
  } finally {
    loading.value = false;
  }
}

async function toggleTask(row, value) {
  try {
    await resourcesApi.updateDiscoverTask(row.id, { enabled: value });
    row.enabled = value;
  } catch (error) {
    ElMessage.error(error.message);
  }
}

async function removeTask(row) {
  try {
    await ElMessageBox.confirm(`删除发现任务「${row.keyword}」？`, "确认删除", {
      confirmButtonText: "删除",
      cancelButtonText: "取消",
      type: "warning",
    });
  } catch {
    return;
  }
  try {
    await resourcesApi.removeDiscoverTask(row.id);
    ElMessage.success("已删除");
    await load();
  } catch (error) {
    ElMessage.error(error.message);
  }
}

async function confirmCreate() {
  if (!createForm.keyword.trim()) {
    ElMessage.warning("请填写关键词或句式");
    return;
  }
  createSaving.value = true;
  try {
    await resourcesApi.createDiscoverTask({
      kind: createForm.kind,
      keyword: createForm.keyword.trim(),
      category: createForm.category.trim() || null,
    });
    ElMessage.success("已新增发现任务");
    createVisible.value = false;
    createForm.keyword = "";
    createForm.category = "";
    await load();
  } catch (error) {
    ElMessage.error(error.message);
  } finally {
    createSaving.value = false;
  }
}

async function importHotwords() {
  try {
    const { data } = await resourcesApi.importHotwords({ top_n: 20, min_count: 2 });
    if (data.added.length) {
      ElMessage.success(`已把 ${data.added.length} 个热门词加入发现关键词库`);
    } else {
      ElMessage.info("热门词都已在任务里了");
    }
    await load();
  } catch (error) {
    ElMessage.error(error.message);
  }
}

async function addPhrasePresets() {
  try {
    const { data } = await resourcesApi.phrasePresets();
    ElMessage.success(
      data.created ? `已添加 ${data.created} 条句式模板` : "句式模板都已经有了",
    );
    await load();
  } catch (error) {
    ElMessage.error(error.message);
  }
}

async function retryTask(row) {
  try {
    await resourcesApi.retryJoinTask(row.id);
    ElMessage.success("已重新排队");
    await load();
  } catch (error) {
    ElMessage.error(error.message);
  }
}

async function cancelTask(row) {
  try {
    await resourcesApi.cancelJoinTask(row.id);
    ElMessage.success("已取消");
    await load();
  } catch (error) {
    ElMessage.error(error.message);
  }
}

onMounted(load);
</script>

<template>
  <div v-loading="loading">
    <div class="toolbar">
      <h2 class="page-title">采集任务</h2>
      <span class="card-hint">
        发现可无人值守：运行时每 30 秒推进一步（加群 → 搜索 → 探测），全程限速。
      </span>
      <div class="spacer" />
      <span class="card-hint">
        采样深度
        <FieldHelp v-bind="RESOURCE_HELP.sampleDepth" />
      </span>
      <el-select v-model="sampleDepth" size="small" style="width: 110px" disabled>
        <el-option :label="'20 条'" :value="20" />
        <el-option :label="'100 条（默认）'" :value="100" />
        <el-option :label="'200 条'" :value="200" />
      </el-select>
      <el-button size="small" @click="load">刷新</el-button>
    </div>

    <el-alert
      class="panel-gap"
      type="info"
      :closable="false"
      show-icon
      title="采集号：默认用「执行账号池」里设为默认的那个账号"
      description="加群、搜索、探测都计到这个账号头上。专用采集号最稳，共用监听号最省事——具体取舍见右侧说明。"
    >
      <template #default>
        <div class="alert-row">
          <FieldHelp v-bind="RESOURCE_HELP.account" />
        </div>
      </template>
    </el-alert>

    <el-card shadow="never" class="panel-gap">
      <template #header>
        <div class="card-head">
          <span>发现任务（关键词 / 句式 / 热门词）</span>
          <div class="spacer" />
          <el-button size="small" @click="importHotwords">导入 Top 20 热门词</el-button>
          <el-button size="small" @click="addPhrasePresets">补句式模板</el-button>
          <el-button size="small" type="primary" @click="createVisible = true">新增任务</el-button>
        </div>
      </template>

      <el-table :data="tasks" size="small" border>
        <el-table-column label="类型" width="90">
          <template #default="{ row }">{{ KIND_LABEL[row.kind] || row.kind }}</template>
        </el-table-column>
        <el-table-column label="关键词 / 句式" min-width="180">
          <template #default="{ row }">
            <span class="chat-name">{{ row.keyword }}</span>
          </template>
        </el-table-column>
        <el-table-column label="分类" width="110">
          <template #default="{ row }">{{ row.category || "-" }}</template>
        </el-table-column>
        <el-table-column label="启用" width="80">
          <template #default="{ row }">
            <el-switch
              :model-value="row.enabled"
              size="small"
              @update:model-value="(value) => toggleTask(row, value)"
            />
          </template>
        </el-table-column>
        <el-table-column label="累计发现" width="100">
          <template #default="{ row }">{{ row.hits }}</template>
        </el-table-column>
        <el-table-column label="累计新增" width="100">
          <template #default="{ row }">{{ row.new_found }}</template>
        </el-table-column>
        <el-table-column label="限流次数" width="90">
          <template #default="{ row }">{{ row.flood_waits }}</template>
        </el-table-column>
        <el-table-column label="上次执行" width="140">
          <template #default="{ row }">{{ fmtTime(row.last_run_at) }}</template>
        </el-table-column>
        <el-table-column label="下次执行" width="140">
          <template #default="{ row }">
            {{ fmtTime(row.next_run_at) }}
            <el-tag v-if="row.due" size="small" type="warning">到点</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="上次错误" min-width="160">
          <template #default="{ row }">
            <span class="card-hint">{{ row.last_error || "-" }}</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="80" fixed="right">
          <template #default="{ row }">
            <el-button size="small" link type="danger" @click="removeTask(row)">删除</el-button>
          </template>
        </el-table-column>
        <template #empty>
          <div class="empty">
            还没有发现任务。可以「新增任务」写关键词，也可以从「热门关键词」页一键导入。
          </div>
        </template>
      </el-table>
    </el-card>

    <el-card shadow="never" class="panel-gap">
      <template #header>
        <div class="card-head">
          <span>加群队列</span>
          <span class="card-hint">
            限速执行
            <FieldHelp v-bind="RESOURCE_HELP.joinLimit" />
          </span>
          <div class="spacer" />
          <el-select
            v-model="joinFilter.status"
            size="small"
            clearable
            placeholder="全部状态"
            style="width: 130px"
            @change="
              () => {
                joinFilter.offset = 0;
                load();
              }
            "
          >
            <el-option v-for="(label, value) in STATUS_LABEL" :key="value" :label="label" :value="value" />
          </el-select>
        </div>
      </template>

      <el-table :data="joinTasks" size="small" border>
        <el-table-column label="资源" min-width="180">
          <template #default="{ row }">{{ row.resource_name || row.resource_id }}</template>
        </el-table-column>
        <el-table-column label="动作" width="80">
          <template #default="{ row }">{{ row.action === "join" ? "加入" : "退出" }}</template>
        </el-table-column>
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag size="small" :type="STATUS_TAG[row.status]">
              {{ STATUS_LABEL[row.status] || row.status }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="尝试" width="70">
          <template #default="{ row }">{{ row.attempts }}</template>
        </el-table-column>
        <el-table-column label="计划时间" width="140">
          <template #default="{ row }">{{ fmtTime(row.scheduled_at) }}</template>
        </el-table-column>
        <el-table-column label="完成时间" width="140">
          <template #default="{ row }">{{ fmtTime(row.finished_at) }}</template>
        </el-table-column>
        <el-table-column label="结果 / 原因" min-width="180">
          <template #default="{ row }">
            <span class="card-hint">{{ row.last_error || "-" }}</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="130" fixed="right">
          <template #default="{ row }">
            <el-button
              v-if="['failed', 'waiting_approval'].includes(row.status)"
              size="small"
              link
              type="primary"
              @click="retryTask(row)"
            >
              重试
            </el-button>
            <el-button
              v-if="['pending', 'waiting_approval'].includes(row.status)"
              size="small"
              link
              type="danger"
              @click="cancelTask(row)"
            >
              取消
            </el-button>
          </template>
        </el-table-column>
        <template #empty>
          <div class="empty">队列是空的。在「资源库」页勾选资源后点「加入群组池」才会排进来。</div>
        </template>
      </el-table>

      <el-pagination
        class="table-gap"
        layout="prev, pager, next, total"
        :total="joinTotal"
        :page-size="joinFilter.limit"
        :current-page="Math.floor(joinFilter.offset / joinFilter.limit) + 1"
        @current-change="
          (page) => {
            joinFilter.offset = (page - 1) * joinFilter.limit;
            load();
          }
        "
      />
    </el-card>

    <el-card shadow="never" class="panel-gap">
      <template #header>
        <div class="card-head">
          <span>配额看板（按账号 · 今天）</span>
          <span class="card-hint">
            超限会自动排队，不会报错
            <FieldHelp v-bind="RESOURCE_HELP.refresh" />
          </span>
        </div>
      </template>

      <el-table :data="quota?.items || []" size="small" border>
        <el-table-column label="账号" min-width="140">
          <template #default="{ row }">{{ row.account_name }}</template>
        </el-table-column>
        <el-table-column label="搜索" width="110">
          <template #default="{ row }">
            {{ row.searches }} / {{ quota?.limits?.search_daily_limit }}
          </template>
        </el-table-column>
        <el-table-column label="探测" width="110">
          <template #default="{ row }">
            {{ row.probes }} / {{ quota?.limits?.probe_daily_limit }}
          </template>
        </el-table-column>
        <el-table-column label="加群" width="110">
          <template #default="{ row }">
            {{ row.joins }} / {{ quota?.limits?.join_daily_limit }}
          </template>
        </el-table-column>
        <el-table-column label="退群" width="80">
          <template #default="{ row }">{{ row.leaves }}</template>
        </el-table-column>
        <el-table-column label="限流次数" width="100">
          <template #default="{ row }">{{ row.flood_waits }}</template>
        </el-table-column>
        <template #empty>
          <div class="empty">还没有执行账号。资源发现需要至少一个已登录的执行账号。</div>
        </template>
      </el-table>
    </el-card>

    <el-alert
      class="panel-gap"
      type="warning"
      :closable="false"
      show-icon
      title="发现号是否「探测完退群」"
    >
      <div class="card-hint">{{ RESOURCE_HELP.leaveAfterProbe.recommend }}</div>
    </el-alert>

    <el-dialog v-model="createVisible" title="新增发现任务" width="480px">
      <el-radio-group v-model="createForm.kind">
        <el-radio value="keyword">关键词</el-radio>
        <el-radio value="phrase">句式</el-radio>
      </el-radio-group>
      <el-input v-model="createForm.keyword" placeholder="例如：求职 / 求群" class="dialog-gap" />
      <el-input v-model="createForm.category" placeholder="分类（可选）：索引型 / 行业 / 语言" class="dialog-gap" />
      <p class="card-hint dialog-gap">
        关键词走公开群搜索，句式走消息全局搜索并从命中消息里提链接；同一个词
        24 小时内不会重复搜。
      </p>
      <template #footer>
        <el-button @click="createVisible = false">取消</el-button>
        <el-button type="primary" :loading="createSaving" @click="confirmCreate">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.panel-gap {
  margin-top: 12px;
}

.card-head {
  display: flex;
  align-items: center;
  gap: 8px;
}

.table-gap {
  margin-top: 10px;
}

.empty {
  padding: 20px 0;
  color: var(--tg-muted);
}

.alert-row {
  margin-top: 4px;
}

.dialog-gap {
  margin-top: 10px;
}
</style>
