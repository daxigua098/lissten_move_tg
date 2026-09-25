<script setup>
import { ElMessage } from "element-plus";
import { onMounted, ref } from "vue";

import { logsApi } from "../api";
import { auth } from "../stores/auth";

const loading = ref(false);
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
          <div class="card-hint">监听源 / 线路</div>
          <div class="stat-value">{{ status?.counts?.sources ?? 0 }} / {{ status?.counts?.routes ?? 0 }}</div>
          <div class="card-hint">T1-03 后接入真实数据</div>
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
          <div class="card-hint">访问模式</div>
          <div class="stat-value">{{ status?.access_mode === "local" ? "本机" : "公网" }}</div>
          <div class="card-hint">{{ status?.environment }}</div>
        </el-card>
      </el-col>
    </el-row>

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
      description="监听源、接收组、线路管理、会员线索等页面会在后续任务中接入；运行时状态（T1-03）接入后这里会显示心跳、账号健康度与队列长度。"
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
</style>
