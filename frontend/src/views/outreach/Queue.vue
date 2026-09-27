<script setup>
import { ElMessage, ElMessageBox } from "element-plus";
import { onMounted, ref } from "vue";

import { outreachApi } from "../../api";

const loading = ref(false);
const runtime = ref({ paused: false });
const capacity = ref(null);
const rows = ref([]);
const total = ref(0);
const planResult = ref(null);
const detailVisible = ref(false);
const detailLoading = ref(false);
const detail = ref(null);

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
  } catch (error) {
    ElMessage.error(error.message);
  } finally {
    loading.value = false;
  }
}

async function plan() {
  try {
    const { data } = await outreachApi.planQueue();
    planResult.value = data;
    ElMessage.success(`已扫描 ${data.scanned} 条，入队 ${data.created} 条`);
    load();
  } catch (error) {
    ElMessage.error(error.message);
  }
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

onMounted(load);
</script>

<template>
  <div>
    <div class="toolbar">
      <h2 class="page-title">冷触达队列</h2>
      <span class="card-hint">共 {{ total }} 条任务</span>
      <div class="spacer" />
      <el-button size="small" type="primary" :loading="loading" @click="plan">生成队列</el-button>
      <el-button size="small" :loading="loading" @click="dispatch">立即发送一条</el-button>
      <el-button size="small" type="danger" plain @click="clearQueue">清空队列</el-button>
      <el-button size="small" @click="toggleRuntime">
        {{ runtime.paused ? "恢复冷触达" : "暂停冷触达" }}
      </el-button>
      <el-button size="small" @click="load">刷新</el-button>
    </div>

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
      <el-col :span="6"><el-statistic title="今日可用冷聊总量" :value="capacity.today_available" /></el-col>
      <el-col :span="6"><el-statistic title="排队中" :value="capacity.queued" /></el-col>
      <el-col :span="6"><el-statistic title="冷却中账号" :value="capacity.cooling_accounts" /></el-col>
      <el-col :span="6"><el-statistic title="被限制账号" :value="capacity.limited_accounts" /></el-col>
    </el-row>

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

    <el-table v-loading="loading" :data="rows" size="small" border class="panel">
      <el-table-column prop="id" label="ID" width="60" />
      <el-table-column label="类型" width="90">
        <template #default="{ row }">{{ row.kind === "follow_up" ? "跟进" : "首条" }}</template>
      </el-table-column>
      <el-table-column label="状态" width="110">
        <template #default="{ row }">
          <el-tag size="small">{{ row.status_label }}</el-tag>
        </template>
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
      <el-table-column prop="attempt_count" label="尝试" width="70" />
      <el-table-column label="最近错误" min-width="180">
        <template #default="{ row }">{{ row.last_error || "-" }}</template>
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
            <el-descriptions-item label="计划时间">{{ fmt(detail.task.scheduled_at) }}</el-descriptions-item>
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
