<script setup>
import { ElMessage, ElMessageBox } from "element-plus";
import { onMounted, reactive, ref } from "vue";

import { usersApi } from "../api";
import { auth } from "../stores/auth";

const loading = ref(false);
const rows = ref([]);
const total = ref(0);
const dialogVisible = ref(false);
const form = reactive({ username: "", password: "", role: "sub_admin", display_name: "" });

const ROLE_LABEL = { super_admin: "超级管理员", sub_admin: "子管理员", viewer: "只读" };

async function load() {
  loading.value = true;
  try {
    const { data } = await usersApi.list({ limit: 100 });
    rows.value = data.items;
    total.value = data.total;
  } catch (error) {
    ElMessage.error(error.message);
  } finally {
    loading.value = false;
  }
}

function openCreate() {
  Object.assign(form, { username: "", password: "", role: "sub_admin", display_name: "" });
  dialogVisible.value = true;
}

async function create() {
  try {
    await usersApi.create({ ...form });
    dialogVisible.value = false;
    ElMessage.success("账号已创建，该账号首次登录需要修改密码");
    load();
  } catch (error) {
    ElMessage.error(error.message);
  }
}

async function changeRole(row, role) {
  try {
    await usersApi.update(row.id, { role });
    ElMessage.success("角色已更新");
    load();
  } catch (error) {
    ElMessage.error(error.message);
    load();
  }
}

async function toggleEnabled(row) {
  try {
    const { data } = await usersApi.update(row.id, { enabled: row.enabled });
    if (!row.enabled && data.revoked_sessions) {
      ElMessage.success(`账号已停用，${data.revoked_sessions} 个会话已下线`);
    } else {
      ElMessage.success(row.enabled ? "账号已启用" : "账号已停用");
    }
  } catch (error) {
    ElMessage.error(error.message);
    load();
  }
}

async function resetPassword(row) {
  try {
    const { value } = await ElMessageBox.prompt(`为 ${row.username} 设置新密码`, "重置密码", {
      inputPlaceholder: "至少 8 位，含字母与数字",
      inputValidator: (input) => (input && input.length >= 8 ? true : "密码至少 8 位"),
      confirmButtonText: "确认",
      cancelButtonText: "取消",
    });
    await usersApi.update(row.id, { password: value });
    ElMessage.success("密码已重置，该账号下次登录需重新设置密码");
  } catch (error) {
    if (error && error.message && !error.message.includes("cancel")) {
      ElMessage.error(error.message);
    }
  }
}

async function revokeSessions(row) {
  try {
    const { data } = await usersApi.revokeSessions(row.id);
    ElMessage.success(`已撤销 ${data.revoked} 个会话`);
  } catch (error) {
    ElMessage.error(error.message);
  }
}

async function remove(row) {
  try {
    await ElMessageBox.confirm(`确认删除账号 ${row.username}？该操作不可恢复。`, "删除账号", {
      type: "warning",
      confirmButtonText: "删除",
      cancelButtonText: "取消",
    });
    await usersApi.remove(row.id);
    ElMessage.success("账号已删除");
    load();
  } catch (error) {
    if (error && error.message && !error.message.includes("cancel")) {
      ElMessage.error(error.message);
    }
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
      <h2 class="page-title">账户管理</h2>
      <span class="card-hint">共 {{ total }} 个账号</span>
      <div class="spacer" />
      <el-button size="small" type="primary" @click="openCreate">新增子管理员</el-button>
      <el-button size="small" @click="load">刷新</el-button>
    </div>

    <el-table v-loading="loading" :data="rows" size="small" border>
      <el-table-column prop="id" label="ID" width="70" />
      <el-table-column label="用户名" min-width="150">
        <template #default="{ row }">
          {{ row.username }}
          <el-tag v-if="row.is_builtin" size="small" type="info" class="tag">内置</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="display_name" label="显示名" width="140" />
      <el-table-column label="角色" width="170">
        <template #default="{ row }">
          <el-select
            :model-value="row.role"
            size="small"
            :disabled="row.is_builtin || row.username === auth.username"
            @change="(value) => changeRole(row, value)"
          >
            <el-option label="超级管理员" value="super_admin" />
            <el-option label="子管理员" value="sub_admin" />
            <el-option label="只读" value="viewer" />
          </el-select>
        </template>
      </el-table-column>
      <el-table-column label="状态" width="110">
        <template #default="{ row }">
          <el-switch
            v-model="row.enabled"
            :disabled="row.username === auth.username"
            @change="() => toggleEnabled(row)"
          />
        </template>
      </el-table-column>
      <el-table-column label="最近登录" width="180">
        <template #default="{ row }">{{ fmt(row.last_login_at) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="280" fixed="right">
        <template #default="{ row }">
          <el-button size="small" link type="primary" :disabled="row.is_builtin" @click="resetPassword(row)">
            重置密码
          </el-button>
          <el-button size="small" link @click="revokeSessions(row)">踢下线</el-button>
          <el-button
            size="small"
            link
            type="danger"
            :disabled="row.is_builtin || row.username === auth.username"
            @click="remove(row)"
          >
            删除
          </el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-alert
      class="panel"
      type="info"
      :closable="false"
      show-icon
      title="角色说明"
      description="超级管理员：全部权限，可管理账号；子管理员：可管理源、线路与线索，不能管理账号；只读：仅可查看与导出。"
    />

    <el-dialog v-model="dialogVisible" title="新增子管理员" width="420px">
      <el-form label-position="top">
        <el-form-item label="用户名">
          <el-input v-model="form.username" placeholder="字母、数字、点、下划线、连字符，3–64 位" />
        </el-form-item>
        <el-form-item label="初始密码">
          <el-input v-model="form.password" type="password" show-password placeholder="至少 8 位，含字母与数字" />
        </el-form-item>
        <el-form-item label="角色">
          <el-select v-model="form.role" style="width: 100%">
            <el-option label="子管理员" value="sub_admin" />
            <el-option label="只读" value="viewer" />
            <el-option label="超级管理员" value="super_admin" />
          </el-select>
        </el-form-item>
        <el-form-item label="显示名（可选）">
          <el-input v-model="form.display_name" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" @click="create">创建</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.tag {
  margin-left: 6px;
}

.panel {
  margin-top: 12px;
}
</style>
