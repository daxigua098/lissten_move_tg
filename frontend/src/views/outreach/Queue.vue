<script setup>
import { ElMessage, ElMessageBox } from "element-plus";
import { nextTick, onMounted, reactive, ref } from "vue";

import { outreachApi, routesApi, sourcesApi } from "../../api";

const loading = ref(false);
const runtime = ref({ paused: false });
const capacity = ref(null);
const rows = ref([]);
const total = ref(0);
const planResult = ref(null);
const detailVisible = ref(false);
const detailLoading = ref(false);
const detail = ref(null);
const tableRef = ref(null);
const selectedTasks = ref([]);
const routeOptions = ref([]);
const sourceOptions = ref([]);
const previewResult = ref(null);
const planning = ref(false);
const filters = reactive({
  route_ids: [],
  source_chat_ids: [],
  keyword: "",
  only_hits: false,
  authorized_only: false,
  limit: 200,
});

async function load() {
  loading.value = true;
  try {
    const [run, cap, tasks] = await Promise.all([
      outreachApi.runtimeStatus(),
      outreachApi.capacity(),
      outreachApi.tasks({ limit: 100 }),
    ]);
    runtime.value = run.data;
    capacity.value = cap.data;
    rows.value = tasks.data.items;
    total.value = tasks.data.total;
    await nextTick();
    selectAll(); // 生成 / 刷新后默认全选
  } catch (error) {
    ElMessage.error(error.message);
  } finally {
    loading.value = false;
  }
}

async function plan() {
  planning.value = true;
  try {
    const { data } = await outreachApi.planQueue(queueParams());
    planResult.value = data;
    ElMessage.success(
      `扫描 ${data.scanned_contacts} 人，入队 ${data.created} 条，受阻 ${data.blocked_contacts} 人`,
    );
    await Promise.all([load(), loadPreview()]);
  } catch (error) {
    ElMessage.error(error.message);
  } finally {
    planning.value = false;
  }
}

function queueParams() {
  return {
    limit: filters.limit,
    route_ids: filters.route_ids.length ? filters.route_ids.join(",") : undefined,
    source_chat_ids: filters.source_chat_ids.length
      ? filters.source_chat_ids.join(",")
      : undefined,
    keyword: filters.keyword || undefined,
    only_hits: filters.only_hits || undefined,
    authorized_only: filters.authorized_only || undefined,
  };
}

async function loadPreview() {
  try {
    const { data } = await outreachApi.previewQueue(queueParams());
    previewResult.value = data;
  } catch (error) {
    ElMessage.error(error.message);
  }
}

async function loadOptions() {
  try {
    const [routes, sources] = await Promise.all([
      routesApi.list({ limit: 200 }),
      sourcesApi.list({ limit: 200 }),
    ]);
    routeOptions.value = routes.data.items;
    sourceOptions.value = sources.data.items;
  } catch {
    routeOptions.value = [];
    sourceOptions.value = [];
  }
}

async function refreshAll() {
  await Promise.all([load(), loadPreview()]);
}

async function confirmUnknown(row, delivered) {
  try {
    await outreachApi.confirmDelivery(row.id, delivered);
    ElMessage.success(delivered ? "已确认送达" : "已确认未送达并重新排队");
    await Promise.all([load(), loadPreview()]);
  } catch (error) {
    ElMessage.error(error.message);
  }
}

async function retryTask(row) {
  try {
    await outreachApi.retryTask(row.id);
    ElMessage.success("已重新排队");
    await Promise.all([load(), loadPreview()]);
  } catch (error) {
    ElMessage.error(error.message);
  }
}

function selectAll() {
  for (const row of rows.value) {
    tableRef.value?.toggleRowSelection(row, true);
  }
}

function clearSelection() {
  tableRef.value?.clearSelection();
}

function toggleSelectAll() {
  if (selectedTasks.value.length) {
    clearSelection();
  } else {
    selectAll();
  }
}

async function deleteTasks(taskIds) {
  if (!taskIds.length) return;
  try {
    await ElMessageBox.confirm(
      `确认删除 ${taskIds.length} 条队列任务？只排过队、还没联系过的线索会退回「待生成」，可再次生成。`,
      "删除队列任务",
      { type: "warning", confirmButtonText: "删除", cancelButtonText: "取消" },
    );
    const { data } = await outreachApi.deleteTasks(taskIds);
    ElMessage.success(`已删除 ${data.deleted} 条任务`);
    load();
  } catch (error) {
    if (error?.message && !error.message.includes("cancel")) {
      ElMessage.error(error.message);
    }
  }
}

function removeOne(row) {
  deleteTasks([row.id]);
}

async function openDetail(row) {
  detailVisible.value = true;
  detailLoading.value = true;
  detail.value = null;
  try {
    const { data } = await outreachApi.taskDetail(row.id);
    detail.value = data;
  } catch (error) {
    ElMessage.error(error.message);
  } finally {
    detailLoading.value = false;
  }
}

function routeLabels(routes) {
  const labels = {
    USERNAME: "用户名",
    PHONE: "手机号",
    PEER_REFERENCE: "监听账号见过",
    SHARED_GROUP: "同群关系",
  };
  if (!routes?.length) return "无";
  return routes.map((item) => labels[item] || item).join(" / ");
}

function consentLabel(value) {
  const labels = {
    NONE: "无（冷触达）",
    EXPLICIT_DM_INVITE: "对方邀请私聊",
    PRIOR_REPLY: "以前回复过",
    MEMBER_CONSENT: "会员授权",
  };
  return labels[value] || value || "-";
}

async function clearQueue() {
  if (!total.value) {
    ElMessage.info("队列已经是空的");
    return;
  }
  try {
    await ElMessageBox.confirm(
      `确认清空队列？将删除 ${total.value} 条待发送任务；` +
        "只排过队、还没联系过的线索会退回「待生成」，已联系/已回复的记录不受影响。",
      "清空队列",
      { type: "warning", confirmButtonText: "清空", cancelButtonText: "取消" },
    );
    const { data } = await outreachApi.clearQueue();
    ElMessage.success(
      `已清空：删除 ${data.deleted_tasks} 条任务，退回 ${data.reset_leads} 条线索`,
    );
    planResult.value = null;
    load();
  } catch (error) {
    if (error?.message && !error.message.includes("cancel")) {
      ElMessage.error(error.message);
    }
  }
}

async function dispatch() {
  try {
    const { data } = await outreachApi.dispatch(1);
    if (data.sent) {
      ElMessage.success("已发送 1 条");
    } else {
      ElMessage.warning("当前没有可发送的任务（缺账号 / 缺话术 / 都在冷却）");
    }
    load();
  } catch (error) {
    ElMessage.error(error.message);
  }
}

async function toggleRuntime() {
  try {
    if (runtime.value.paused) {
      await outreachApi.resumeRuntime();
      ElMessage.success("冷触达已恢复");
    } else {
      await outreachApi.pauseRuntime();
      ElMessage.success("冷触达已暂停");
    }
    load();
  } catch (error) {
    ElMessage.error(error.message);
  }
}

function fmt(value) {
  return value ? new Date(value).toLocaleString("zh-CN") : "-";
}

function capacitySum(field) {
  return (capacity.value?.account_details || []).reduce(
    (total, item) => total + Number(item[field] || 0),
    0,
  );
}

onMounted(async () => {
  await Promise.all([load(), loadOptions()]);
  await loadPreview();
});
</script>

<template>
  <div>
    <div class="toolbar">
      <h2 class="page-title">冷触达队列</h2>
      <span class="card-hint">共 {{ total }} 条任务</span>
      <div class="spacer" />
      <el-button size="small" type="primary" :loading="planning" @click="plan">生成队列</el-button>
      <el-button size="small" :loading="loading" @click="dispatch">立即发送一条</el-button>
      <el-button size="small" @click="toggleSelectAll">
        {{ selectedTasks.length ? `取消全选（${selectedTasks.length}）` : "全选" }}
      </el-button>
      <el-button
        size="small"
        type="danger"
        plain
        :disabled="!selectedTasks.length"
        @click="deleteTasks(selectedTasks.map((row) => row.id))"
      >
        删除选中
      </el-button>
      <el-button size="small" type="danger" plain @click="clearQueue">清空队列</el-button>
      <el-button size="small" @click="toggleRuntime">
        {{ runtime.paused ? "恢复冷触达" : "暂停冷触达" }}
      </el-button>
      <el-button size="small" @click="refreshAll">刷新</el-button>
    </div>

    <el-card shadow="never" class="panel">
      <div class="filters">
        <el-select
          v-model="filters.route_ids"
          size="small"
          multiple
          collapse-tags
          clearable
          placeholder="全部线路"
          style="width: 210px"
        >
          <el-option
            v-for="item in routeOptions"
            :key="item.id"
            :label="item.name || `线路 ${item.id}`"
            :value="item.id"
          />
        </el-select>
        <el-select
          v-model="filters.source_chat_ids"
          size="small"
          multiple
          collapse-tags
          clearable
          placeholder="全部来源群"
          style="width: 220px"
        >
          <el-option
            v-for="item in sourceOptions"
            :key="item.id"
            :label="item.name || item.title || item.username || `来源 ${item.id}`"
            :value="item.id"
          />
        </el-select>
        <el-input
          v-model="filters.keyword"
          size="small"
          placeholder="关键词"
          clearable
          style="width: 150px"
          @keyup.enter="loadPreview"
        />
        <el-checkbox v-model="filters.only_hits" size="small">只看命中</el-checkbox>
        <el-checkbox v-model="filters.authorized_only" size="small">只看有授权</el-checkbox>
        <el-input-number v-model="filters.limit" size="small" :min="1" :max="2000" />
        <el-button size="small" @click="loadPreview">预览</el-button>
        <span class="card-hint">
          预计可生成 {{ previewResult?.created ?? "-" }} 人，受阻
          {{ previewResult?.blocked_contacts ?? "-" }} 人
        </span>
      </div>
    </el-card>

    <el-alert
      v-if="runtime.paused"
      class="panel"
      type="warning"
      :closable="false"
      show-icon
      title="冷触达已暂停"
      description="暂停只影响发信息账号，搬运 / 监听照常运行。"
    />

    <el-row v-if="capacity" :gutter="12">
      <el-col :span="6"><el-statistic title="今日还可发送" :value="capacity.today_available" /></el-col>
      <el-col :span="6"><el-statistic title="排队中" :value="capacity.queued" /></el-col>
      <el-col :span="6"><el-statistic title="冷却中账号" :value="capacity.cooling_accounts" /></el-col>
      <el-col :span="6"><el-statistic title="被限制账号" :value="capacity.limited_accounts" /></el-col>
    </el-row>

    <el-alert
      v-if="capacity"
      class="panel"
      type="info"
      :closable="false"
      show-icon
      :title="`今日总额度 ${capacitySum('daily_cap')} / 已发 ${capacitySum('today_sent')} / 剩余 ${capacity.today_available}`"
      :description="[
        ...(capacity.account_details || []).map(
          (item) =>
            `账号 #${item.id} ${item.name}：${item.tier_label}，已发 ${item.today_sent}/${item.daily_cap}，剩余 ${item.remaining}${item.cooldown_until ? '，冷却中' : ''}`,
        ),
        ...(capacity.excluded_accounts || []).map(
          (item) => `未计入 #${item.id} ${item.name}：${item.reason}`,
        ),
      ].join('；')"
    />

    <el-alert
      v-if="capacity && capacity.estimated_days"
      class="panel"
      type="warning"
      :closable="false"
      show-icon
      :title="`按今日容量约需 ${capacity.estimated_days} 天消化完`"
      description="账号不够时队列会一直排队，不会突破每日上限。请增加真实活跃账号，或改为让用户主动联系。"
    />

    <el-alert
      v-if="planResult && planResult.blocked.length"
      class="panel"
      type="info"
      :closable="false"
      show-icon
      title="本次未入队的原因"
      :description="planResult.blocked.map((item) => `${item.label}：${item.count}`).join('；')"
    />

    <el-table
      ref="tableRef"
      v-loading="loading"
      :data="rows"
      size="small"
      border
      class="panel"
      row-key="id"
      @selection-change="(value) => (selectedTasks = value)"
    >
      <el-table-column type="selection" width="42" />
      <el-table-column prop="id" label="ID" width="60" />
      <el-table-column label="类型" width="90">
        <template #default="{ row }">{{ row.kind === "follow_up" ? "跟进" : "首条" }}</template>
      </el-table-column>
      <el-table-column label="状态" width="110">
        <template #default="{ row }">
          <el-tag size="small">{{ row.status_label }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="priority" label="优先级" width="80" />
      <el-table-column label="来源群" min-width="130">
        <template #default="{ row }">{{ row.source_title || "-" }}</template>
      </el-table-column>
      <el-table-column label="联系人" min-width="180">
        <template #default="{ row }">
          <el-button link type="primary" @click="openDetail(row)">
            {{ row.contact_name || row.contact_tg_user_id || "-" }}
          </el-button>
          <span class="card-hint">{{ row.contact_tg_user_id || "" }}</span>
        </template>
      </el-table-column>
      <el-table-column label="计划时间" width="170">
        <template #default="{ row }">{{ fmt(row.scheduled_at) }}</template>
      </el-table-column>
      <el-table-column label="下一步时间" width="170">
        <template #default="{ row }">{{ fmt(row.next_plan_at) }}</template>
      </el-table-column>
      <el-table-column prop="attempt_count" label="尝试" width="70" />
      <el-table-column label="最近错误" min-width="180">
        <template #default="{ row }">{{ row.last_error || "-" }}</template>
      </el-table-column>
      <el-table-column label="操作" width="170" fixed="right">
        <template #default="{ row }">
          <template v-if="row.status === 'UNKNOWN_DELIVERY'">
            <el-button size="small" link type="success" @click="confirmUnknown(row, true)">
              已送达
            </el-button>
            <el-button size="small" link type="warning" @click="confirmUnknown(row, false)">
              未送达
            </el-button>
          </template>
          <el-button
            v-if="row.status === 'FAILED'"
            size="small"
            link
            type="warning"
            @click="retryTask(row)"
          >
            重试
          </el-button>
          <el-button size="small" link type="danger" @click="removeOne(row)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-drawer v-model="detailVisible" title="队列详情（含监听抓取信息）" size="560px">
      <div v-loading="detailLoading">
        <template v-if="detail">
          <el-descriptions :column="1" border size="small" title="任务">
            <el-descriptions-item label="任务类型">
              {{ detail.task.kind === "follow_up" ? "跟进" : "首条招呼" }}
            </el-descriptions-item>
            <el-descriptions-item label="状态">{{ detail.task.status_label }}</el-descriptions-item>
            <el-descriptions-item label="优先级">
              {{ detail.task.priority }} · {{ detail.task.priority_reason || "-" }}
            </el-descriptions-item>
            <el-descriptions-item label="计划时间">{{ fmt(detail.task.scheduled_at) }}</el-descriptions-item>
            <el-descriptions-item label="下一步时间">{{ fmt(detail.task.next_plan_at) }}</el-descriptions-item>
            <el-descriptions-item label="尝试次数">{{ detail.task.attempt_count }}</el-descriptions-item>
            <el-descriptions-item label="最近错误">{{ detail.task.last_error || "-" }}</el-descriptions-item>
          </el-descriptions>

          <el-descriptions
            v-if="detail.contact"
            class="panel"
            :column="1"
            border
            size="small"
            title="联系人"
          >
            <el-descriptions-item label="昵称">{{ detail.contact.display_name || "-" }}</el-descriptions-item>
            <el-descriptions-item label="用户名">{{ detail.contact.username || "-" }}</el-descriptions-item>
            <el-descriptions-item label="TG 用户 ID">{{ detail.contact.tg_user_id }}</el-descriptions-item>
            <el-descriptions-item label="手机号">{{ detail.contact.phone || "-" }}</el-descriptions-item>
            <el-descriptions-item label="状态">
              {{ detail.contact.state_label }} / 回复：{{ detail.contact.reply_state || "-" }}
            </el-descriptions-item>
            <el-descriptions-item label="首次联系">{{ fmt(detail.contact.first_contact_at) }}</el-descriptions-item>
            <el-descriptions-item label="跨账号锁至">
              {{ fmt(detail.contact.global_lock_until) }}
            </el-descriptions-item>
            <el-descriptions-item label="免打扰">
              {{ detail.contact.do_not_contact ? "是" : "否" }}
            </el-descriptions-item>
          </el-descriptions>

          <el-descriptions
            v-if="detail.lead"
            class="panel"
            :column="1"
            border
            size="small"
            title="监听抓取到的信息"
          >
            <el-descriptions-item label="来源群">{{ detail.lead.source_title || "-" }}</el-descriptions-item>
            <el-descriptions-item label="发言时间">{{ fmt(detail.lead.message_at) }}</el-descriptions-item>
            <el-descriptions-item label="消息 ID">{{ detail.lead.message_id }}</el-descriptions-item>
            <el-descriptions-item label="发言人">
              {{ detail.lead.sender_name || "-" }}
              <span class="card-hint">
                {{ detail.lead.sender_username ? `@${detail.lead.sender_username}` : "" }}
                {{ detail.lead.sender_tg_id || "" }}
              </span>
            </el-descriptions-item>
            <el-descriptions-item label="命中关键词">
              {{ detail.lead.keyword || "（全量入库）" }}
              <span class="card-hint">
                {{ detail.lead.matched_mode }} {{ detail.lead.score ? `· 评分 ${detail.lead.score}` : "" }}
              </span>
            </el-descriptions-item>
            <el-descriptions-item label="可触达路径">
              {{ routeLabels(detail.lead.reachable_routes) }}
            </el-descriptions-item>
            <el-descriptions-item label="授权依据">
              {{ consentLabel(detail.lead.consent_type) }}
            </el-descriptions-item>
            <el-descriptions-item label="抓取判定">
              {{ detail.lead.capture_reason || "-" }}
            </el-descriptions-item>
            <el-descriptions-item label="联系方式">
              手机：{{ detail.lead.phone || "-" }} / 微信：{{ detail.lead.wechat || "-" }}
              <span class="card-hint">
                {{ (detail.lead.contacts.usernames || []).join(" ") }}
              </span>
            </el-descriptions-item>
            <el-descriptions-item label="原文">
              <div class="lead-text">{{ detail.lead.text || "-" }}</div>
            </el-descriptions-item>
          </el-descriptions>

          <el-alert
            v-else
            class="panel"
            type="info"
            :closable="false"
            show-icon
            title="没有关联的监听线索"
            description="这条任务可能是手工加入或来源线索已被清理。"
          />
        </template>
      </div>
    </el-drawer>

    <el-alert
      class="panel"
      type="warning"
      :closable="false"
      show-icon
      title="本阶段只入队，不发送"
      description="冷触达实验功能：不保证送达，也不保证账号稳定。首触全账号池每人只发一次，冷却与每日上限都不会被突破。"
    />
  </div>
</template>

<style scoped>
.panel {
  margin-top: 12px;
}

.lead-text {
  white-space: pre-wrap;
  word-break: break-word;
}
</style>
