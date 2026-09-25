<script setup>
import { ElMessage, ElMessageBox } from "element-plus";
import { onMounted, reactive, ref } from "vue";

import { accountsApi } from "../api";

const loading = ref(false);
const rows = ref([]);
const total = ref(0);
const dialogVisible = ref(false);
const form = reactive({
  name: "",
  phone: "",
  api_id: "",
  api_hash: "",
  is_default: false,
  note: "",
});

const STATUS_TYPE = {
  pending_login: "warning",
  active: "success",
  restricted: "danger",
  disabled: "info",
};

async function load() {
  loading.value = true;
  try {
    const { data } = await accountsApi.list({ limit: 100 });
    rows.value = data.items;
    total.value = data.total;
  } catch (error) {
    ElMessage.error(error.message);
  } finally {
    loading.value = false;
  }
}

function openCreate() {
  Object.assign(form, {
    name: "",
    phone: "",
    api_id: "",
    api_hash: "",
    is_default: false,
    note: "",
  });
  dialogVisible.value = true;
}

async function create() {
  try {
    await accountsApi.create({
      name: form.name,
      phone: form.phone,
      api_id: form.api_id ? Number(form.api_id) : null,
      api_hash: form.api_hash || null,
      is_default: form.is_default,
      note: form.note || null,
    });
    dialogVisible.value = false;
    ElMessage.success("账号已登记，请用 CLI 完成 Telegram 登录");
    load();
  } catch (error) {
    ElMessage.error(error.message);
  }
}

async function setDefault(row) {
  try {
    await accountsApi.update(row.id, { is_default: true });
    ElMessage.success(`已把 ${row.name} 设为默认账号`);
    load();
  } catch (error) {
    ElMessage.error(error.message);
  }
}

async function toggleStatus(row) {
  const next = row.status === "disabled" ? "pending_login" : "disabled";
  try {
    await accountsApi.update(row.id, { status: next });
    ElMessage.success(next === "disabled" ? "账号已停用" : "账号已启用，请重新登录");
    load();
  } catch (error) {
    ElMessage.error(error.message);
  }
}

async function remove(row) {
  try {
    await ElMessageBox.confirm(
      `确认删除执行账号 ${row.name}？session 文件需要手工清理。`,
      "删除账号",
      { type: "warning", confirmButtonText: "删除", cancelButtonText: "取消" },
    );
    await accountsApi.remove(row.id);
    ElMessage.success("账号已删除");
    load();
  } catch (error) {
    if (error?.message && !error.message.includes("cancel")) {
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
      <h2 class="page-title">执行账号池</h2>
      <span class="card-hint">共 {{ total }} 个账号</span>
      <div class="spacer" />
      <el-button size="small" type="primary" @click="openCreate">登记执行账号</el-button>
      <el-button size="small" @click="load">刷新</el-button>
    </div>

    <el-table v-loading="loading" :data="rows" size="small" border>
      <el-table-column prop="id" label="ID" width="60" />
      <el-table-column label="别名" min-width="140">
        <template #default="{ row }">
          {{ row.name }}
          <el-tag v-if="row.is_default" size="small" class="tag">默认</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="phone_masked" label="手机号" width="130" />
      <el-table-column label="状态" width="100">
        <template #default="{ row }">
          <el-tag :type="STATUS_TYPE[row.status] || 'info'" size="small">
            {{ row.status_label }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="健康度" width="140">
        <template #default="{ row }">
          <el-progress
            :percentage="row.health_score"
            :stroke-width="6"
            :status="row.health_score >= 80 ? 'success' : row.health_score >= 50 ? 'warning' : 'exception'"
          />
        </template>
      </el-table-column>
      <el-table-column label="Telegram" min-width="150">
        <template #default="{ row }">
          <span v-if="row.username || row.tg_user_id">
            {{ row.username ? `@${row.username}` : "" }}
            <span class="card-hint">{{ row.tg_user_id || "" }}</span>
          </span>
          <span v-else class="card-hint">未登录</span>
        </template>
      </el-table-column>
      <el-table-column label="最近使用" width="170">
        <template #default="{ row }">{{ fmt(row.last_used_at) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="270" fixed="right">
        <template #default="{ row }">
          <el-button size="small" link type="primary" :disabled="row.is_default" @click="setDefault(row)">
            设为默认
          </el-button>
          <el-button size="small" link @click="toggleStatus(row)">
            {{ row.status === "disabled" ? "启用" : "停用" }}
          </el-button>
          <el-button size="small" link type="danger" @click="remove(row)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-alert
      class="panel"
      type="warning"
      :closable="false"
      show-icon
      title="登记后还需要在服务器上完成一次 Telegram 登录，才会生成 session 文件并进入「正常」状态"
      description="命令：.\.venv\Scripts\python.exe main.py account-login --name 别名"
    />

    <el-alert
      class="panel"
      type="info"
      :closable="false"
      show-icon
      title="账号是业务的生命线"
      description="同一账号内部严格串行投递；API ID / API Hash / 手机号均加密存储，界面只显示掩码。账号文件与 session 不进版本库，请单独备份。"
    />

    <el-dialog v-model="dialogVisible" title="登记执行账号" width="480px">
      <el-form label-position="top">
        <el-form-item label="别名">
          <el-input v-model="form.name" placeholder="例如：主号、备用1" />
        </el-form-item>
        <el-form-item label="手机号（含区号）">
          <el-input v-model="form.phone" placeholder="+8613800001111" />
        </el-form-item>
        <el-form-item label="API ID（留空则用 .env 里的 TG_API_ID）">
          <el-input v-model="form.api_id" placeholder="留空使用 .env 里的默认凭据" />
        </el-form-item>
        <el-form-item label="API Hash（留空则用 .env 里的 TG_API_HASH）">
          <el-input v-model="form.api_hash" show-password placeholder="留空使用 .env 里的默认凭据" />
        </el-form-item>
        <el-form-item label="备注（可选）">
          <el-input v-model="form.note" />
        </el-form-item>
        <el-form-item>
          <el-checkbox v-model="form.is_default">设为默认执行账号</el-checkbox>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" @click="create">登记</el-button>
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
