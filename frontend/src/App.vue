<script setup>
import { ElMessage, ElMessageBox } from "element-plus";
import { computed } from "vue";
import { useRoute, useRouter } from "vue-router";

import { authApi } from "./api";
import { auth, MODULE_LABELS } from "./stores/auth";

const route = useRoute();
const router = useRouter();

const showShell = computed(
  () => auth.isAuthenticated && !["login", "change-password"].includes(route.name),
);

const canOperate = computed(() => ["sub_admin", "super_admin"].includes(auth.role));

// 菜单按「账号类型 + 功能块」生成：
//   代理 → 只有代理工作台；会员 → 基础能力 + 已授权的功能块；平台 → 沿用角色分级
const menuItems = computed(() => {
  if (auth.isAgent) {
    return [{ index: "/agent", label: "代理工作台" }];
  }
  if (auth.isMember) {
    const items = [{ index: "/", label: "运行总览" }];
    if (auth.hasAnyModule(["carry", "monitor"])) {
      items.push({ index: "/config", label: "线路配置" });
    }
    if (auth.hasModule("monitor")) {
      items.push({ index: "/library", label: "词库" });
      items.push({ index: "/leads", label: "线索池" });
    }
    if (auth.hasModule("discovery")) {
      items.push({ index: "/resources", label: "资源发现" });
    }
    items.push({ index: "/ops", label: "账号与机器人" });
    return items;
  }
  const items = [{ index: "/", label: "运行总览" }];
  if (canOperate.value) {
    items.push({ index: "/config", label: "线路配置" });
    items.push({ index: "/library", label: "词库" });
    items.push({ index: "/resources", label: "资源发现" });
    items.push({ index: "/leads", label: "线索池" });
  }
  if (auth.isSuperAdmin) {
    items.push({ index: "/ops", label: "账号与机器人" });
  }
  if (canOperate.value) {
    items.push({ index: "/system", label: "系统管理" });
  }
  return items;
});

const moduleHint = computed(() => {
  if (!auth.isMember) return "";
  const names = auth.modules.map((code) => MODULE_LABELS[code] || code);
  return names.length ? names.join(" · ") : "仅基础功能";
});

const expiryHint = computed(() => {
  if (!auth.expiresAt) return "";
  return String(auth.expiresAt).slice(0, 10);
});

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
        <el-menu-item v-for="item in menuItems" :key="item.index" :index="item.index">
          {{ item.label }}
        </el-menu-item>
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
          <el-tag type="success" effect="light">后台服务正常</el-tag>
          <el-tag v-if="auth.isMember && moduleHint" type="info" effect="plain">
            {{ moduleHint }}
          </el-tag>
          <el-tag v-if="auth.isMember && expiryHint" type="warning" effect="plain">
            有效期至 {{ expiryHint }}
          </el-tag>
          <span class="card-hint">{{ auth.username }} · {{ auth.roleLabel }}</span>
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
