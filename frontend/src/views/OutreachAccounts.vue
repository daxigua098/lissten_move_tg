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
  api_id: "",
  api_hash: "",
  note: "",
  is_default: false,
  owner_confirmed: false,
});

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
    api_id: "",
    api_hash: "",
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
      api_id: form.api_id ? Number(form.api_id) : null,
      api_hash: form.api_hash || null,
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
