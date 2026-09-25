<script setup>
import { ElMessage } from "element-plus";
import { computed, onMounted, ref } from "vue";

import { logsApi, runtimeApi } from "../api";
import { auth } from "../stores/auth";

const loading = ref(false);
const busy = ref(false);
const status = ref(null);

async function load() {
  loading.value = true;
  try {
    const { data } = await logsApi.status();
    status.value = data;
  } catch (error) {
    ElMessage.error(error.message);
  } finally {
    loading.value = false;
  }
}

onMounted(load);

const runtimeLabel = computed(() => {
  const state = status.value?.runtime?.status;
  if (state === "running") {
    return status.value?.runtime?.paused ? "已暂停" : "运行中";
  }
  if (state === "stopped") return "未启动";
  return state || "-";
});

const heartbeatText = computed(() => {
  const age = status.value?.runtime?.heartbeat_age_seconds;
  if (age === null || age === undefined) return "-";
  return `${Math.round(age)} 秒前`;
});

const isPaused = computed(() => Boolean(status.value?.runtime?.paused));

async function act(action) {
  busy.value = true;
  try {
    const { data } = await runtimeApi[action]();
    if (action === "start" || action === "restart") {
      const info = data.start || {};
      if (info.started) {
        ElMessage.success(`运行时已启动（PID ${info.pid}）`);
      } else if (info.reason === "already_running") {
        ElMessage.info("运行时已经在运行了");
      } else {
        ElMessage.error(info.hint || "运行时启动失败，详见 data/runtime.stderr.log");
      }
    } else {
      ElMessage.success(
        { pause: "已暂停投递", resume: "已恢复投递", stop: "已请求停止运行时" }[action] || "已执行",
      );
    }
    await load();
  } catch (error) {
    ElMessage.error(error.message);
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <div v-loading="loading">
    <div class="toolbar">
      <h2 class="page-title">运行总览</h2>
      <div class="spacer" />
      <el-button size="small" @click="load">刷新</el-button>
    </div>

    <el-row :gutter="12">
      <el-col :xs="12" :sm="8" :md="6">
        <el-card shadow="never">
          <div class="card-hint">后台账号</div>
          <div class="stat-value">{{ status?.counts?.users ?? "-" }}</div>
          <div class="card-hint">启用超管 {{ status?.counts?.active_super_admins ?? "-" }}</div>
        </el-card>
      </el-col>
      <el-col :xs="12" :sm="8" :md="6">
        <el-card shadow="never">
          <div class="card-hint">搬运运行时</div>
          <div class="stat-value">{{ runtimeLabel }}</div>
          <div class="card-hint">队列 {{ status?.counts?.queue_size ?? 0 }} 条</div>
        </el-card>
      </el-col>
      <el-col :xs="12" :sm="8" :md="6">
        <el-card shadow="never">
          <div class="card-hint">数据库</div>
          <div class="stat-value">{{ status?.database?.status === "ok" ? "正常" : "异常" }}</div>
          <div class="card-hint">{{ status?.database?.url_scheme }}</div>
        </el-card>
      </el-col>
      <el-col :xs="12" :sm="8" :md="6">
        <el-card shadow="never">
          <div class="card-hint">投递成功</div>
          <div class="stat-value">{{ status?.counts?.jobs?.success ?? 0 }}</div>
          <div class="card-hint">
            失败 {{ status?.counts?.jobs?.failed ?? 0 }} · 重试中 {{ status?.counts?.jobs?.retrying ?? 0 }}
          </div>
        </el-card>
      </el-col>
    </el-row>

    <el-card shadow="never" class="panel">
      <template #header>
        <span>运行时控制</span>
      </template>
      <div class="runtime-actions">
        <el-button type="primary" :loading="busy" @click="act('start')">启动</el-button>
        <el-button :loading="busy" @click="act('restart')">重启</el-button>
        <el-button :loading="busy" @click="act(isPaused ? 'resume' : 'pause')">
          {{ isPaused ? "恢复投递" : "暂停投递" }}
        </el-button>
        <el-button type="danger" plain :loading="busy" @click="act('stop')">停止</el-button>
        <span class="card-hint">心跳 {{ heartbeatText }}</span>
      </div>
      <p class="card-hint runtime-note">
        运行时不热加载配置：改完线路、接收目标或广告策略后，点「重启」才生效。
      </p>
    </el-card>

    <el-card shadow="never" class="panel">
      <template #header>
        <span>当前登录</span>
      </template>
      <el-descriptions :column="2" border>
        <el-descriptions-item label="账号">{{ auth.username }}</el-descriptions-item>
        <el-descriptions-item label="角色">
          {{
            auth.role === "super_admin" ? "超级管理员" : auth.role === "sub_admin" ? "子管理员" : "只读"
          }}
        </el-descriptions-item>
        <el-descriptions-item label="内置账号">{{ auth.isBuiltin ? "是（密码在 .env 维护）" : "否" }}</el-descriptions-item>
        <el-descriptions-item label="服务器时间">{{ status?.server_time }}</el-descriptions-item>
      </el-descriptions>
    </el-card>

    <el-card shadow="never" class="panel">
      <template #header>
        <span>数据保留策略</span>
      </template>
      <el-descriptions :column="3" border>
        <el-descriptions-item label="原始消息">{{ status?.retention?.messages_days }} 天</el-descriptions-item>
        <el-descriptions-item label="线索/档案">{{ status?.retention?.leads_days }} 天</el-descriptions-item>
        <el-descriptions-item label="归档文件">{{ status?.retention?.archive_days }} 天</el-descriptions-item>
      </el-descriptions>
    </el-card>

    <el-alert
      class="panel"
      type="info"
      :closable="false"
      show-icon
      title="当前是一期基础工程（T1-01 / T1-02 / E2 登录与权限）。"
      description="搬运运行时用 python main.py run 启动；启动后这里会显示心跳、队列长度与投递统计。会员线索与关键词词库属于二期范围。"
    />
  </div>
</template>

<style scoped>
.stat-value {
  font-size: 22px;
  font-weight: 500;
  margin: 4px 0;
}

.panel {
  margin-top: 12px;
}

.runtime-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.runtime-note {
  margin: 10px 0 0;
}
</style>
