<script setup>
import { ElMessage, ElMessageBox } from "element-plus";
import { computed } from "vue";
import { useRoute, useRouter } from "vue-router";

import { authApi } from "./api";
import { auth } from "./stores/auth";

const route = useRoute();
const router = useRouter();

const showShell = computed(
  () => auth.isAuthenticated && !["login", "change-password"].includes(route.name),
);

const canOperate = computed(() => ["sub_admin", "super_admin"].includes(auth.role));

// 当前加载的前端产物文件名，用来判断页面是不是最新构建
const pageVersion = import.meta.url.split("/").pop();

async function logout() {
  try {
    await authApi.logout();
  } catch {
    // 令牌可能已失效，忽略即可
  }
  auth.clear();
  ElMessage.success("已退出登录");
  router.push({ name: "login" });
}

function logoutAll() {
  ElMessageBox.confirm("将撤销你的全部登录会话，其他设备需要重新登录。", "确认退出所有设备", {
    confirmButtonText: "确认",
    cancelButtonText: "取消",
    type: "warning",
  })
    .then(async () => {
      await authApi.logoutAll();
      auth.clear();
      router.push({ name: "login" });
    })
    .catch(() => {});
}
</script>

<template>
  <router-view v-if="!showShell" />

  <el-container v-else class="shell">
    <el-aside width="212px" class="shell-aside">
      <div class="brand">
        <span class="brand-logo">TG</span>
        <div>
          <div class="brand-name">线索运营</div>
          <div class="brand-sub">后台管理</div>
        </div>
      </div>
      <el-menu :default-active="route.path" router class="shell-menu">
        <el-menu-item index="/">运行总览</el-menu-item>
        <el-menu-item v-if="canOperate" index="/sources">监听源</el-menu-item>
        <el-menu-item v-if="canOperate" index="/targets">接收组</el-menu-item>
        <el-menu-item v-if="canOperate" index="/routes">线路管理</el-menu-item>
        <el-menu-item v-if="canOperate" index="/keywords">词库管理</el-menu-item>
        <el-menu-item v-if="canOperate" index="/hot-keywords">热门关键词</el-menu-item>
        <el-menu-item v-if="canOperate" index="/leads">线索池</el-menu-item>
        <el-menu-item v-if="canOperate" index="/ad-assets">广告素材库</el-menu-item>
        <el-menu-item index="/jobs">投递任务</el-menu-item>
        <el-menu-item v-if="auth.isSuperAdmin" index="/accounts">执行账号池</el-menu-item>
        <el-menu-item v-if="auth.isSuperAdmin" index="/bots">控制 Bot</el-menu-item>
        <el-menu-item index="/audit">审计日志</el-menu-item>
        <el-menu-item v-if="auth.isSuperAdmin" index="/users">账户管理</el-menu-item>
        <el-menu-item v-if="auth.isSuperAdmin" index="/login-history">登录历史</el-menu-item>
      </el-menu>
      <div class="aside-footer">
        <span class="card-hint version">页面版本 {{ pageVersion }}</span>
        <el-button link type="primary" @click="logout">退出登录</el-button>
        <el-button link @click="logoutAll">退出所有设备</el-button>
      </div>
    </el-aside>

    <el-container>
      <el-header class="shell-header">
        <span class="page-title">{{ route.name === "dashboard" ? "运行总览" : "" }}</span>
        <div class="header-right">
          <el-tag type="success" effect="light">服务运行中</el-tag>
          <span class="card-hint">
            {{ auth.username }} ·
            {{ auth.role === "super_admin" ? "超级管理员" : auth.role === "sub_admin" ? "子管理员" : "只读" }}
          </span>
        </div>
      </el-header>
      <el-main class="shell-main">
        <router-view />
      </el-main>
    </el-container>
  </el-container>
</template>

<style scoped>
.shell {
  height: 100%;
}

.shell-aside {
  background: #111a2b;
  color: #c3cddd;
  display: flex;
  flex-direction: column;
}

.brand {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 16px 16px 14px;
  color: #fff;
}

.brand-logo {
  width: 30px;
  height: 30px;
  border-radius: 8px;
  background: var(--tg-accent);
  display: grid;
  place-items: center;
  font-size: 12px;
}

.brand-name {
  font-weight: 500;
}

.brand-sub {
  font-size: 12px;
  opacity: 0.6;
}

.shell-menu {
  background: transparent;
  border-right: 0;
  flex: 1;
}

.shell-menu :deep(.el-menu-item) {
  color: #c3cddd;
}

.shell-menu :deep(.el-menu-item.is-active) {
  color: #fff;
  background: rgba(255, 255, 255, 0.12);
}

.aside-footer {
  padding: 12px 16px 18px;
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 6px;
}

.aside-footer .version {
  font-size: 11px;
  opacity: 0.6;
  word-break: break-all;
}

.shell-header {
  background: #fff;
  border-bottom: 1px solid var(--tg-border);
  display: flex;
  align-items: center;
  gap: 12px;
}

.header-right {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: 10px;
}

.shell-main {
  background: var(--tg-bg);
}
</style>
