<script setup>
import { ElMessage, ElMessageBox } from "element-plus";
import { computed, onMounted, reactive, ref } from "vue";

import { jobsApi, routesApi, runtimeApi } from "../api";

const loading = ref(false);
const rows = ref([]);
const total = ref(0);
const routes = ref([]);
const runtime = ref(null);
const filters = reactive({ status: "", route_id: null, limit: 50, offset: 0 });

const STATUS_TYPE = {
  success: "success",
  failed: "danger",
  retrying: "warning",
  processing: "warning",
  pending: "info",
  skipped: "info",
};

const stats = computed(() => runtime.value?.jobs || {});
const runtimeLabel = computed(() => {
  const state = runtime.value?.status;
  if (state === "running") return runtime.value?.paused ? "已暂停" : "运行中";
  if (state === "stopped") return "未启动";
  return state || "-";
});

async function load() {
  loading.value = true;
  try {
    const params = { limit: filters.limit, offset: filters.offset };
    if (filters.status) params.status = filters.status;
    if (filters.route_id) params.route_id = filters.route_id;
    const [jobList, runtimeStatus, routeList] = await Promise.all([
      jobsApi.list(params),
      runtimeApi.status(),
      routesApi.list({ limit: 200 }),
    ]);
    rows.value = jobList.data.items;
    total.value = jobList.data.total;
    runtime.value = runtimeStatus.data;
    routes.value = routeList.data.items;
  } catch (error) {
    ElMessage.error(error.message);
  } finally {
    loading.value = false;
  }
}

function changePage(page) {
  filters.offset = (page - 1) * filters.limit;
  load();
}

async function control(action) {
  const labels = { pause: "暂停投递", resume: "恢复投递", stop: "停止运行时" };
  try {
    await ElMessageBox.confirm(
      action === "stop"
        ? "停止后运行时进程会在当前任务结束后退出，需要重新执行 python main.py run 才能继续。"
        : `确认${labels[action]}？`,
      labels[action],
      { type: "warning", confirmButtonText: "确认", cancelButtonText: "取消" },
    );
    const { data } = await runtimeApi[action]();
    runtime.value = data;
    ElMessage.success(`已${labels[action].slice(0, 2)}`);
  } catch (error) {
    if (error?.message && !error.message.includes("cancel")) {
      ElMessage.error(error.message);
    }
  }
}

async function retryFailed() {
  try {
    const { data } = await jobsApi.retryFailed();
    ElMessage.success(`已重新排队 ${data.retried} 条失败任务`);
    load();
  } catch (error) {
    ElMessage.error(error.message);
  }
}

async function retryOne(row) {
  try {
    await jobsApi.retryOne(row.id);
    ElMessage.success("已重新排队");
    load();
  } catch (error) {
    ElMessage.error(error.message);
  }
}

async function skipOne(row) {
  try {
    await jobsApi.skipOne(row.id);
    ElMessage.success("已跳过该任务");
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
  <div v-loading="loading">
    <div class="toolbar">
      <h2 class="page-title">投递任务</h2>
      <el-tag :type="runtime?.status === 'running' ? 'success' : 'info'" size="small">
        {{ runtimeLabel }}
      </el-tag>
      <span class="card-hint" v-if="runtime?.heartbeat_age_seconds != null">
        心跳 {{ Math.round(runtime.heartbeat_age_seconds) }} 秒前
      </span>
      <div class="spacer" />
      <el-button size="small" @click="control('pause')" :disabled="runtime?.paused">暂停投递</el-button>
      <el-button size="small" @click="control('resume')" :disabled="!runtime?.paused">恢复投递</el-button>
      <el-button size="small" type="danger" plain @click="control('stop')">停止运行时</el-button>
      <el-button size="small" @click="load">刷新</el-button>
    </div>

    <el-row :gutter="10" class="stats">
      <el-col :xs="12" :sm="8" :md="4" v-for="item in [
        { key: 'pending', label: '待投递' },
        { key: 'retrying', label: '重试中' },
        { key: 'success', label: '成功' },
        { key: 'failed', label: '失败' },
        { key: 'skipped', label: '已跳过' },
      ]" :key="item.key">
        <el-card shadow="never">
          <div class="card-hint">{{ item.label }}</div>
          <div class="stat-value">{{ stats[item.key] ?? 0 }}</div>
        </el-card>
      </el-col>
    </el-row>

    <el-card shadow="never" class="panel-gap">
      <template #header>
        <div class="toolbar" style="margin: 0">
          <span>任务列表（共 {{ total }} 条）</span>
          <div class="spacer" />
          <el-select v-model="filters.status" size="small" placeholder="状态" style="width: 130px" @change="load">
            <el-option label="全部状态" value="" />
            <el-option label="待投递" value="pending" />
            <el-option label="重试中" value="retrying" />
            <el-option label="成功" value="success" />
            <el-option label="失败" value="failed" />
            <el-option label="已跳过" value="skipped" />
          </el-select>
          <el-select v-model="filters.route_id" size="small" placeholder="线路" clearable style="width: 180px" @change="load">
            <el-option v-for="item in routes" :key="item.id" :label="item.name" :value="item.id" />
          </el-select>
          <el-button size="small" @click="retryFailed">重试全部失败</el-button>
        </div>
      </template>

      <el-table :data="rows" size="small" border>
        <el-table-column prop="id" label="ID" width="70" />
        <el-table-column label="线路" min-width="170">
          <template #default="{ row }">{{ row.route_name || "-" }}</template>
        </el-table-column>
        <el-table-column label="源消息" width="110">
          <template #default="{ row }">{{ row.source_title }} · {{ row.source_message_id }}</template>
        </el-table-column>
        <el-table-column label="目标" min-width="140">
          <template #default="{ row }">
            {{ row.target_title }}
            <span v-if="row.target_message_id" class="card-hint">→ {{ row.target_message_id }}</span>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag :type="STATUS_TYPE[row.status] || 'info'" size="small">{{ row.status_label }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="尝试" width="80">
          <template #default="{ row }">{{ row.attempt_count }} / {{ row.max_attempts }}</template>
        </el-table-column>
        <el-table-column label="广告" width="70">
          <template #default="{ row }">{{ row.ad_applied ? "是" : "否" }}</template>
        </el-table-column>
        <el-table-column label="错误" min-width="180">
          <template #default="{ row }">
            <span v-if="row.last_error" class="error-text">{{ row.last_error }}</span>
            <span v-else class="card-hint">-</span>
          </template>
        </el-table-column>
        <el-table-column label="时间" width="170">
          <template #default="{ row }">{{ fmt(row.sent_at || row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="130" fixed="right">
          <template #default="{ row }">
            <el-button
              size="small"
              link
              type="primary"
              v-if="['failed', 'skipped'].includes(row.status)"
              @click="retryOne(row)"
            >
              重试
            </el-button>
            <el-button
              size="small"
              link
              v-if="['pending', 'retrying', 'failed'].includes(row.status)"
              @click="skipOne(row)"
            >
              跳过
            </el-button>
          </template>
        </el-table-column>
      </el-table>

      <el-pagination
        class="pager"
        layout="total, prev, pager, next"
        :total="total"
        :page-size="filters.limit"
        @current-change="changePage"
      />
    </el-card>

    <el-alert
      class="panel-gap"
      type="info"
      :closable="false"
      show-icon
      title="搬运运行时需要在服务器上启动"
      description="命令：python main.py run（实时监听 + 串行投递）。总览页与这里的暂停/恢复/停止按钮写入的是运行控制文件，运行时进程会在下一轮循环读取；停止后需要重新执行启动命令。"
    />
  </div>
</template>

<style scoped>
.panel-gap {
  margin-top: 12px;
}

.stats {
  margin-top: 4px;
}

.stat-value {
  font-size: 20px;
  font-weight: 500;
  margin-top: 2px;
}

.error-text {
  color: #b42318;
  font-size: 12px;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.pager {
  margin-top: 12px;
  justify-content: flex-end;
}
</style>
