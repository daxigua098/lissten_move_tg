<script setup>
import { ElMessage, ElMessageBox } from "element-plus";
import { onMounted, reactive, ref } from "vue";

import { accountsApi } from "../api";

const loading = ref(false);
const rows = ref([]);
const total = ref(0);
const dialogVisible = ref(false);
const loginDialog = ref(false);
const loginAccount = ref(null);
const loginStage = ref("idle");
const loginBusy = ref(false);
const loginResult = ref(null);
const loginForm = reactive({ code: "", password: "", force_sms: false });
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

async function refreshCredentials(row) {
  try {
    await accountsApi.refreshCredentials(row.id);
    ElMessage.success("已用 .env 里的凭据覆盖该账号");
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

function openLogin(row) {
  loginAccount.value = row;
  loginStage.value = "idle";
  loginResult.value = null;
  Object.assign(loginForm, { code: "", password: "", force_sms: false });
  loginDialog.value = true;
}

async function sendLoginCode() {
  loginBusy.value = true;
  try {
    const { data } = await accountsApi.loginStart(loginAccount.value.id, loginForm.force_sms);
    loginStage.value = "code_sent";
    ElMessage.success(`验证码已发送到 ${data.phone_masked} 对应的 Telegram`);
  } catch (error) {
    ElMessage.error(error.message);
  } finally {
    loginBusy.value = false;
  }
}

async function submitLoginCode() {
  if (!loginForm.code.trim()) {
    ElMessage.warning("请输入验证码");
    return;
  }
  loginBusy.value = true;
  try {
    const { data } = await accountsApi.loginVerify(loginAccount.value.id, loginForm.code.trim());
    if (data.status === "password_required") {
      loginStage.value = "password_required";
      ElMessage.info("该账号开启了两步验证，请继续输入密码");
    } else {
      loginStage.value = "active";
      loginResult.value = data;
      ElMessage.success("登录成功");
      load();
    }
  } catch (error) {
    ElMessage.error(error.message);
  } finally {
    loginBusy.value = false;
  }
}

async function submitLoginPassword() {
  if (!loginForm.password) {
    ElMessage.warning("请输入两步验证密码");
    return;
  }
  loginBusy.value = true;
  try {
    const { data } = await accountsApi.loginPassword(loginAccount.value.id, loginForm.password);
    loginStage.value = "active";
    loginResult.value = data;
    ElMessage.success("登录成功");
    load();
  } catch (error) {
    ElMessage.error(error.message);
  } finally {
    loginBusy.value = false;
  }
}

async function closeLoginDialog() {
  if (loginStage.value === "code_sent" || loginStage.value === "password_required") {
    try {
      await accountsApi.loginCancel(loginAccount.value.id);
    } catch {
      // 取消失败不阻塞关闭
    }
  }
  loginDialog.value = false;
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
          <el-button size="small" link type="success" @click="openLogin(row)">
            {{ row.status === "active" ? "重新登录" : "登录" }}
          </el-button>
          <el-button size="small" link type="primary" :disabled="row.is_default" @click="setDefault(row)">
            设为默认
          </el-button>
          <el-button size="small" link @click="refreshCredentials(row)">用 .env 凭据</el-button>
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
      title="登记后还要完成一次 Telegram 登录，状态才会变成「正常」"
      description="点列表里的「登录」按钮即可：发送验证码 → 输入验证码 →（若开了两步验证）输入密码。也可以在服务器终端执行 main.py account-login --account-id 账号ID。"
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

    <el-dialog
      :model-value="loginDialog"
      title="登录执行账号"
      width="460px"
      :close-on-click-modal="false"
      @close="closeLoginDialog"
      @update:model-value="(value) => { if (!value) closeLoginDialog(); }"
    >
      <div v-if="loginAccount" class="login-body">
        <el-alert
          type="info"
          :closable="false"
          show-icon
          :title="`账号：${loginAccount.name}（${loginAccount.phone_masked}）`"
          description="验证码会发到该手机号对应的 Telegram App 里，不是短信。"
        />

        <div v-if="loginStage === 'idle'" class="login-step">
          <p class="card-hint">点击下方按钮让 Telegram 发送登录验证码。</p>
          <el-checkbox v-model="loginForm.force_sms">改用短信接收验证码（收不到 App 消息时勾选）</el-checkbox>
          <el-button type="primary" :loading="loginBusy" @click="sendLoginCode">
            发送验证码
          </el-button>
        </div>

        <div v-else-if="loginStage === 'code_sent'" class="login-step">
          <el-input
            v-model="loginForm.code"
            size="large"
            placeholder="输入 Telegram 收到的验证码"
            @keyup.enter="submitLoginCode"
          />
          <div class="login-actions">
            <el-button link type="primary" :loading="loginBusy" @click="sendLoginCode">
              没收到？重新发送
            </el-button>
            <div class="spacer" />
            <el-button @click="closeLoginDialog">取消</el-button>
            <el-button type="primary" :loading="loginBusy" @click="submitLoginCode">提交</el-button>
          </div>
        </div>

        <div v-else-if="loginStage === 'password_required'" class="login-step">
          <el-alert
            type="warning"
            :closable="false"
            show-icon
            title="该账号开启了两步验证"
            description="请输入你在 Telegram 设置的两步验证密码（不是登录验证码）。"
          />
          <el-input
            v-model="loginForm.password"
            type="password"
            show-password
            size="large"
            placeholder="两步验证密码"
            @keyup.enter="submitLoginPassword"
          />
          <div class="login-actions">
            <div class="spacer" />
            <el-button @click="closeLoginDialog">取消</el-button>
            <el-button type="primary" :loading="loginBusy" @click="submitLoginPassword">提交</el-button>
          </div>
        </div>

        <div v-else class="login-step">
          <el-result icon="success" title="登录成功">
            <template #sub-title>
              <span v-if="loginResult?.username">@{{ loginResult.username }} · ID {{ loginResult.tg_user_id }}</span>
              <span v-else>session 已生成</span>
            </template>
          </el-result>
          <el-button type="primary" @click="closeLoginDialog">完成</el-button>
        </div>
      </div>
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

.login-body {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.login-step {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.login-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}
</style>
