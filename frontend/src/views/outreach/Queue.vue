<script setup>
import { ElMessage } from "element-plus";
import { onMounted, ref } from "vue";

import { outreachApi } from "../../api";

const loading = ref(false);
const runtime = ref({ paused: false });
const capacity = ref(null);
const rows = ref([]);
const total = ref(0);
const planResult = ref(null);

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
      <el-table-column label="联系人" min-width="160">
        <template #default="{ row }">
          {{ row.contact_name || "-" }}
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
</style>
