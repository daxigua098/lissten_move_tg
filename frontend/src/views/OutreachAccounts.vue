<script setup>
import { ElMessage, ElMessageBox } from "element-plus";
import { onMounted, reactive, ref } from "vue";

import { accountsApi } from "../api";
import AccountLoginDialog from "../components/AccountLoginDialog.vue";

const loading = ref(false);
const rows = ref([]);
const total = ref(0);
const dialogVisible = ref(false);
const loginDialog = ref(false);
const loginAccount = ref(null);
const selected = ref([]);
const form = reactive({
  name: "",
  phone: "",
  note: "",
  is_default: false,
  owner_confirmed: false,
});

// 批量登录：每个账号各自持有验证码状态，验证码由人工从各自 Telegram App 读取
const batchDialog = ref(false);
const batchBusy = ref(false);
const batchRows = ref([]);
const sharedPassword = ref("");

const BATCH_LABELS = {
  idle: "未发送",
  code_sent: "验证码已发送",
  password_required: "需要二级密码",
  active: "已登录",
  error: "失败",
  resolving: "解析接码地址",
  sending: "发送验证码",
  waiting_code: "等待接码平台",
  verifying: "提交验证码",
  success: "登录成功",
  failed: "失败",
  stopped: "已停止",
};

// 导入：每行「+手机号 接码地址」
const importDialog = ref(false);
const importText = ref("");
const importConfirmed = ref(false);
const importBusy = ref(false);
const importResult = ref(null);
let batchTimer = null;

const STATUS_TYPE = {
  pending_login: "warning",
  active: "success",
  restricted: "danger",
  disabled: "info",
};

const STATE_TYPE = {
  NEW: "info",
  READY: "success",
  COOLING: "warning",
  CAPPED: "info",
  LIMITED: "danger",
  PAUSED: "warning",
  DISABLED: "info",
};

const TIERS = [
  { value: "NEW", label: "新号（<14 天）" },
  { value: "WARMING", label: "养号（2~8 周）" },
  { value: "STANDARD", label: "普通（2~6 月）" },
  { value: "MATURE", label: "成熟健康号" },
];

async function load() {
  loading.value = true;
  try {
    const { data } = await accountsApi.list({ purpose: "outreach", limit: 100 });
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
    note: "",
    is_default: false,
    owner_confirmed: false,
  });
  dialogVisible.value = true;
}

async function create() {
  if (!form.owner_confirmed) {
    ElMessage.warning("请先确认该账号归你所有并已获授权用于发送消息");
    return;
  }
  try {
    await accountsApi.create({
      name: form.name,
      phone: form.phone,
      note: form.note || null,
      is_default: form.is_default,
      purpose: "outreach",
      owner_confirmed: true,
    });
    dialogVisible.value = false;
    ElMessage.success("账号已登记，请完成 Telegram 登录");
    load();
  } catch (error) {
    ElMessage.error(error.message);
  }
}

function openLogin(row) {
  loginAccount.value = row;
  loginDialog.value = true;
}

async function setDefault(row) {
  try {
    await accountsApi.update(row.id, { is_default: true });
    ElMessage.success(`已把 ${row.name} 设为默认发信息账号`);
    load();
  } catch (error) {
    ElMessage.error(error.message);
  }
}

async function setTier(row, tier) {
  try {
    await accountsApi.update(row.id, { outreach_tier: tier });
    ElMessage.success(`已把 ${row.name} 调为 ${TIERS.find((item) => item.value === tier)?.label}`);
    load();
  } catch (error) {
    ElMessage.error(error.message);
  }
}

async function toggleState(row) {
  const paused = row.outreach?.state === "PAUSED";
  try {
    await accountsApi.update(row.id, { outreach_state: paused ? "READY" : "PAUSED" });
    ElMessage.success(paused ? "账号已恢复" : "账号已暂停");
    load();
  } catch (error) {
    ElMessage.error(error.message);
  }
}

function openImport() {
  importText.value = "";
  importConfirmed.value = false;
  importResult.value = null;
  importDialog.value = true;
}

async function submitImport() {
  if (!importConfirmed.value) {
    ElMessage.warning("请先确认这些账号归你所有并已获授权用于发送消息");
    return;
  }
  importBusy.value = true;
  try {
    const { data } = await accountsApi.importAccounts({
      text: importText.value,
      owner_confirmed: true,
    });
    importResult.value = data;
    ElMessage.success(`已导入 ${data.total} 个账号`);
    load();
  } catch (error) {
    ElMessage.error(error.message);
  } finally {
    importBusy.value = false;
  }
}

function importErrorText(result) {
  if (!result?.errors?.length) return "";
  return result.errors.map((item) => item.message).join("；");
}

function stopBatchPolling() {
  if (batchTimer) {
    clearInterval(batchTimer);
    batchTimer = null;
  }
}

async function pollBatchStatus() {
  try {
    const { data } = await accountsApi.autoLoginStatus();
    const byId = new Map(data.items.map((item) => [item.account_id, item]));
    let running = 0;
    for (const row of batchRows.value) {
      const item = byId.get(row.id);
      if (!item) continue;
      row.stage = item.stage;
      row.message = item.message;
      row.error = item.status === "failed" ? item.message : "";
      if (item.status === "pending") running += 1;
    }
    if (!running) {
      stopBatchPolling();
      load();
    }
  } catch (error) {
    stopBatchPolling();
    ElMessage.error(error.message);
  }
}

async function autoLoginBatch() {
  const targets = batchRows.value.filter((row) => row.stage !== "active");
  if (!targets.length) {
    ElMessage.warning("没有需要登录的账号");
    return;
  }
  const withoutCode = targets.filter((row) => !row.code_host);
  try {
    await accountsApi.autoLogin(targets.map((row) => row.id));
    ElMessage.success("已开始自动登录，正在等接码平台返回验证码");
    if (withoutCode.length) {
      ElMessage.warning(`其中 ${withoutCode.length} 个账号没有接码地址，会直接失败`);
    }
    stopBatchPolling();
    batchTimer = setInterval(pollBatchStatus, 3000);
    pollBatchStatus();
  } catch (error) {
    ElMessage.error(error.message);
  }
}

async function stopAutoLogin() {
  try {
    await accountsApi.autoLoginStop(batchRows.value.map((row) => row.id));
    stopBatchPolling();
    ElMessage.success("已请求停止自动登录");
    pollBatchStatus();
  } catch (error) {
    ElMessage.error(error.message);
  }
}

function openBatch() {
  const targets = selected.value.length
    ? selected.value
    : rows.value.filter((row) => row.status !== "active");
  if (!targets.length) {
    ElMessage.warning("没有需要登录的账号，请先勾选或先登记账号");
    return;
  }
  batchRows.value = targets.map((row) => ({
    id: row.id,
    name: row.name,
    phone_masked: row.phone_masked,
    code_host: row.code_host || "",
    stage: "idle",
    code: "",
    password: "",
    message: "",
    error: "",
  }));
  stopBatchPolling();
  sharedPassword.value = "";
  batchDialog.value = true;
}

async function sendAllCodes() {
  batchBusy.value = true;
  let sent = 0;
  for (const item of batchRows.value) {
    if (item.stage === "active") continue;
    try {
      await accountsApi.loginStart(item.id);
      item.stage = "code_sent";
      item.error = "";
      sent += 1;
    } catch (error) {
      item.stage = "error";
      item.error = error.message;
    }
  }
  batchBusy.value = false;
  ElMessage.success(`已为 ${sent} 个账号请求验证码，请逐个填入收到验证码`);
}

async function submitAllCodes() {
  batchBusy.value = true;
  let ok = 0;
  for (const item of batchRows.value) {
    if (item.stage !== "code_sent" || !item.code.trim()) continue;
    try {
      const { data } = await accountsApi.loginVerify(item.id, item.code.trim());
      if (data.status === "password_required") {
        item.stage = "password_required";
        item.error = "";
      } else {
        item.stage = "active";
        item.error = "";
        ok += 1;
      }
    } catch (error) {
      item.error = error.message;
    }
  }
  batchBusy.value = false;
  load();
  ElMessage.success(`验证码已提交，登录成功 ${ok} 个`);
}

async function submitAllPasswords() {
  batchBusy.value = true;
  let ok = 0;
  for (const item of batchRows.value) {
    if (item.stage !== "password_required") continue;
    const password = item.password || sharedPassword.value;
    if (!password) {
      item.error = "请填写二级密码";
      continue;
    }
    try {
      await accountsApi.loginPassword(item.id, password);
      item.stage = "active";
      item.error = "";
      ok += 1;
    } catch (error) {
      item.error = error.message;
    }
  }
  batchBusy.value = false;
  load();
  ElMessage.success(`二级密码已提交，完成 ${ok} 个`);
}

async function closeBatch() {
  stopBatchPolling();
  for (const item of batchRows.value) {
    if (item.stage === "code_sent" || item.stage === "password_required") {
      try {
        await accountsApi.loginCancel(item.id);
      } catch {
        // 取消失败不阻塞关闭
      }
    }
  }
  batchDialog.value = false;
}

async function retireBatch(hard) {
  if (!selected.value.length) {
    ElMessage.warning("请先勾选要处理的账号");
    return;
  }
  const ids = selected.value.map((item) => item.id);
  try {
    if (hard) {
      await ElMessageBox.prompt(
        `将彻底删除 ${ids.length} 个账号：名下会话全部冻结（不会转给其他账号）。请输入 DELETE 确认。`,
        "彻底删除账号",
        {
          confirmButtonText: "删除",
          cancelButtonText: "取消",
          inputPattern: /^DELETE$/,
          inputErrorMessage: "请输入 DELETE",
        },
      );
    } else {
      await ElMessageBox.confirm(
        `将停用 ${ids.length} 个账号：名下会话全部冻结（不转号），在途任务重新排队。`,
        "批量停用",
        { type: "warning", confirmButtonText: "停用", cancelButtonText: "取消" },
      );
    }
    const { data } = await outreachApi.batchRetire({
      account_ids: ids,
      reason: hard ? "batch_delete" : "batch_disable",
      hard,
    });
    ElMessage.success(`已处理 ${data.count} 个账号，冻结会话 ${data.frozen_contacts} 个`);
    load();
  } catch (error) {
    if (error?.message && !error.message.includes("cancel")) {
      ElMessage.error(error.message);
    }
  }
}

function percent(value) {
  return value === null || value === undefined ? "-" : `${Math.round(value * 100)}%`;
}

async function remove(row) {
  try {
    await ElMessageBox.confirm(
      `确认删除发信息账号 ${row.name}？session 文件需要手工清理。`,
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
      <h2 class="page-title">发信息账号</h2>
      <span class="card-hint">共 {{ total }} 个账号</span>
      <div class="spacer" />
      <el-button size="small" type="primary" @click="openCreate">登记发信息账号</el-button>
      <el-button size="small" @click="openImport">导入账号</el-button>
      <el-button size="small" @click="openBatch">批量登录</el-button>
      <el-button size="small" :disabled="!selected.length" @click="retireBatch(false)">
        批量停用
      </el-button>
      <el-button
        size="small"
        type="danger"
        plain
        :disabled="!selected.length"
        @click="retireBatch(true)"
      >
        彻底删除
      </el-button>
      <el-button size="small" @click="load">刷新</el-button>
    </div>

    <el-table
      v-loading="loading"
      :data="rows"
      size="small"
      border
      @selection-change="(value) => (selected = value)"
    >
      <el-table-column type="selection" width="42" />
      <el-table-column prop="id" label="ID" width="60" />
      <el-table-column label="别名" min-width="140">
        <template #default="{ row }">
          {{ row.name }}
          <el-tag v-if="row.is_default" size="small" class="tag">默认</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="phone_masked" label="手机号" width="130" />
      <el-table-column label="登录状态" width="100">
        <template #default="{ row }">
          <el-tag :type="STATUS_TYPE[row.status] || 'info'" size="small">
            {{ row.status_label }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="运营态" width="120">
        <template #default="{ row }">
          <el-tag :type="STATE_TYPE[row.outreach?.state] || 'info'" size="small">
            {{ row.outreach?.state_label || "-" }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="档位" width="170">
        <template #default="{ row }">
          <el-select
            :model-value="row.outreach?.tier"
            size="small"
            @change="(value) => setTier(row, value)"
          >
            <el-option v-for="item in TIERS" :key="item.value" :label="item.label" :value="item.value" />
          </el-select>
        </template>
      </el-table-column>
      <el-table-column label="今日额度" width="120">
        <template #default="{ row }">
          {{ row.outreach?.today_sent ?? 0 }} / {{ row.outreach?.daily_cap ?? "-" }}
        </template>
      </el-table-column>
      <el-table-column label="7日成功率" width="110">
        <template #default="{ row }">{{ percent(row.outreach?.success_rate_7d) }}</template>
      </el-table-column>
      <el-table-column label="7日回复率" width="110">
        <template #default="{ row }">{{ percent(row.outreach?.reply_rate_7d) }}</template>
      </el-table-column>
      <el-table-column label="在跟会话" width="100">
        <template #default="{ row }">{{ row.outreach?.active_conversation_count ?? 0 }}</template>
      </el-table-column>
      <el-table-column label="冷却至" width="170">
        <template #default="{ row }">{{ fmt(row.outreach?.cooldown_until) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="260" fixed="right">
        <template #default="{ row }">
          <el-button size="small" link type="success" @click="openLogin(row)">
            {{ row.status === "active" ? "重新登录" : "登录" }}
          </el-button>
          <el-button size="small" link type="primary" :disabled="row.is_default" @click="setDefault(row)">
            设为默认
          </el-button>
          <el-button size="small" link @click="toggleState(row)">
            {{ row.outreach?.state === "PAUSED" ? "恢复" : "暂停" }}
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
      title="发信息账号专门用于冷触达与对话，与监听账号分开使用"
      description="每日首次私聊按档位限额（新号 3 / 养号 5 / 普通 10 / 成熟最多 20），每次首触冷却默认 2 小时。这是实验功能：不保证送达，也不保证账号稳定，请只用真实、成熟的账号。"
    />

    <el-alert
      class="panel"
      type="info"
      :closable="false"
      show-icon
      title="账号归属与授权"
      description="登记前请确认该账号归你本人或你的组织所有，并已获授权用于发送消息。手机号 / API 凭据均加密存储，界面只显示掩码。"
    />

    <el-dialog v-model="dialogVisible" title="登记发信息账号" width="480px">
      <el-form label-position="top">
        <el-form-item label="别名">
          <el-input v-model="form.name" placeholder="例如：冷聊1号" />
        </el-form-item>
        <el-form-item label="手机号（含区号）">
          <el-input v-model="form.phone" placeholder="+8613800001111" />
        </el-form-item>
        <el-form-item label="备注（可选）">
          <el-input v-model="form.note" />
        </el-form-item>
        <el-form-item>
          <el-checkbox v-model="form.is_default">设为默认发信息账号</el-checkbox>
        </el-form-item>
        <el-form-item>
          <el-checkbox v-model="form.owner_confirmed">
            我确认该账号归我本人或我的组织所有，并已获授权用于发送消息
          </el-checkbox>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" @click="create">登记</el-button>
      </template>
    </el-dialog>

    <AccountLoginDialog
      v-model="loginDialog"
      :account="loginAccount"
      title="登录发信息账号"
      @logged-in="load"
    />

    <el-dialog v-model="importDialog" title="导入发信息账号" width="640px">
      <el-alert
        type="info"
        :closable="false"
        show-icon
        title="每行一个账号：+手机号 接码地址"
        description="例如：+14135030718 https://logincode.add4533.com/?token=... （同一个接码地址同时提供登录验证码和二级密码）"
      />
      <el-input
        v-model="importText"
        type="textarea"
        :rows="8"
        class="panel"
        placeholder="+14135030718 https://logincode.add4533.com/?token=..."
      />
      <el-checkbox v-model="importConfirmed" class="panel">
        我确认这些账号归我本人或我的组织所有，并已获授权用于发送消息
      </el-checkbox>
      <el-alert
        v-if="importResult"
        class="panel"
        :type="importResult.errors.length ? 'warning' : 'success'"
        :closable="false"
        show-icon
        :title="`已导入 ${importResult.total} 个账号`"
        :description="importErrorText(importResult) || '全部成功'"
      />
      <template #footer>
        <el-button @click="importDialog = false">关闭</el-button>
        <el-button type="primary" :loading="importBusy" @click="submitImport">导入</el-button>
      </template>
    </el-dialog>

    <el-dialog
      v-model="batchDialog"
      title="批量登录发信息账号"
      width="720px"
      :close-on-click-modal="false"
      @close="closeBatch"
    >
      <el-alert
        type="info"
        :closable="false"
        show-icon
        title="验证码会发到各账号自己的 Telegram App"
        description="先点「批量发送验证码」，再把每个账号收到的验证码填进对应一行；开了两步验证的账号，再填二级密码（可只填一次统一密码）。"
      />

      <div class="batch-actions">
        <el-button type="primary" :loading="batchBusy" @click="sendAllCodes">
          批量发送验证码
        </el-button>
        <el-button :loading="batchBusy" @click="submitAllCodes">提交所有验证码</el-button>
        <el-button :loading="batchBusy" @click="submitAllPasswords">提交二级密码</el-button>
        <el-button type="success" @click="autoLoginBatch">从接码平台自动登录</el-button>
        <el-button @click="stopAutoLogin">停止</el-button>
        <span class="card-hint">统一二级密码</span>
        <el-input
          v-model="sharedPassword"
          show-password
          type="password"
          size="small"
          style="width: 180px"
          placeholder="多个账号共用时可只填这里"
        />
      </div>

      <el-table :data="batchRows" size="small" border class="panel">
        <el-table-column prop="name" label="别名" min-width="120" />
        <el-table-column prop="phone_masked" label="手机号" width="120" />
        <el-table-column label="接码地址" min-width="150">
          <template #default="{ row }">
            <span v-if="row.code_host">{{ row.code_host }}</span>
            <span v-else class="card-hint">未填</span>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="120">
          <template #default="{ row }">{{ BATCH_LABELS[row.stage] || row.stage }}</template>
        </el-table-column>
        <el-table-column label="验证码" width="130">
          <template #default="{ row }">
            <el-input
              v-model="row.code"
              size="small"
              placeholder="收到的验证码"
              :disabled="row.stage !== 'code_sent'"
            />
          </template>
        </el-table-column>
        <el-table-column label="二级密码" width="130">
          <template #default="{ row }">
            <el-input
              v-model="row.password"
              size="small"
              show-password
              type="password"
              placeholder="按需填写"
              :disabled="row.stage !== 'password_required'"
            />
          </template>
        </el-table-column>
        <el-table-column label="说明" min-width="180">
          <template #default="{ row }">{{ row.error || row.message || "-" }}</template>
        </el-table-column>
      </el-table>

      <template #footer>
        <el-button @click="closeBatch">关闭</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.tag {
  margin-left: 6px;
}

.batch-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 12px 0 4px;
  flex-wrap: wrap;
}

.panel {
  margin-top: 12px;
}
</style>
