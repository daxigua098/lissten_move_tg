<script setup>
import { ElMessage, ElMessageBox } from "element-plus";
import { computed, onMounted, reactive, ref } from "vue";

import { agentApi, http, platformApi } from "../api";
import { auth } from "../stores/auth";

const MODULE_OPTIONS = [
  { value: "carry", label: "搬运帖子" },
  { value: "monitor", label: "监听会员" },
  { value: "discovery", label: "资源发现" },
];

const QUOTA_OPTIONS = [
  { value: "member", label: "会员额度" },
  { value: "agent", label: "代理额度" },
  { value: "trial", label: "试用额度" },
];

const loading = ref(false);
const activeTab = ref("overview");

const overview = ref({ counts: {}, today: {}, expiring: [], quota_alerts: [] });
const agents = ref([]);
const agentKeyword = ref("");
const members = ref({ items: [], total: 0 });
const expiry = ref({ counts: {}, buckets: {} });
const ledger = ref({ items: [], total: 0 });
const templates = ref([]);

const memberFilter = reactive({
  agent_user_id: null,
  status: "",
  module: "",
  quota_type: "",
  keyword: "",
  limit: 50,
  offset: 0,
});
const ledgerFilter = reactive({
  agent_user_id: null,
  quota_type: "",
  action: "",
  start: "",
  end: "",
  limit: 50,
  offset: 0,
});

// 开一级代理（与划拨额度同表单提交，落库是两笔）
const agentForm = reactive({
  username: "",
  display_name: "",
  member_alloc: 0,
  agent_alloc: 0,
  trial_alloc: 0,
  note: "",
});
// 开会员
const memberForm = reactive({
  username: "",
  days: 30,
  template_code: "",
  modules: [],
  display_name: "",
  owner_agent_id: null,
  note: "",
});
const submitting = ref(false);
const issued = ref(null);
const issuedOpen = ref(false);

const allocVisible = ref(false);
const allocMode = ref("allocate");
const allocRow = ref(null);
const allocForm = reactive({ quota_type: "member", count: 1, note: "" });

const renewVisible = ref(false);
const renewRow = ref(null);
const renewForm = reactive({ days: 30, modules: [], template_code: "", note: "" });

const planVisible = ref(false);
const planRow = ref(null);
const planForm = reactive({ modules: [], template_code: "" });

const adjustVisible = ref(false);
const adjustRow = ref(null);
const adjustForm = reactive({ quota_type: "member", delta: 10, note: "" });

const drawerVisible = ref(false);
const drawerAgent = ref(null);
const drawerTree = ref([]);

const counterCards = computed(() => [
  { key: "agents", label: "代理账号", value: overview.value.counts.agents ?? 0 },
  { key: "members", label: "会员账号", value: overview.value.counts.members ?? 0 },
  { key: "expiring", label: "7 天内到期", value: overview.value.counts.expiring ?? 0 },
  { key: "expired", label: "已过期", value: overview.value.counts.expired ?? 0 },
]);

const expiryGroups = computed(() =>
  ["today", "days3", "days7", "expired"].map((key) => ({
    key,
    label: expiry.value.buckets?.[key]?.label || key,
    count: expiry.value.counts?.[key] ?? 0,
    items: expiry.value.buckets?.[key]?.items || [],
  })),
);

const agentOptions = computed(() =>
  agents.value.map((item) => ({
    value: item.user_id,
    label: `${item.username}${item.depth > 1 ? `（${item.depth} 层）` : ""}`,
  })),
);

function fmt(value) {
  return value ? new Date(value).toLocaleString("zh-CN") : "-";
}

function fmtDay(value) {
  return value ? String(value).slice(0, 10) : "-";
}

function statusTag(row) {
  if (!row.enabled) return { type: "info", text: "已停用" };
  if (row.status === "suspended") return { type: "info", text: "已停用" };
  if (row.status === "expired") return { type: "danger", text: "已过期" };
  return { type: "success", text: "生效中" };
}

function quotaText(row) {
  const quota = row.quota || {};
  const held = row.held || {};
  return `会员 ${quota.member ?? 0}（占用 ${held.member ?? 0}） / 代理 ${quota.agent ?? 0} / 试用 ${quota.trial ?? 0}（占用 ${held.trial ?? 0}）`;
}

function memberNames(row) {
  return (row.module_labels || []).join(" · ") || "仅基础功能";
}

async function loadOverview() {
  const { data } = await platformApi.overview({ days: 7 });
  overview.value = data;
}

async function loadAgents() {
  const { data } = await platformApi.agents({ keyword: agentKeyword.value || undefined });
  agents.value = data.items;
}

async function loadMembers() {
  const { data } = await platformApi.members({
    agent_user_id: memberFilter.agent_user_id || undefined,
    status: memberFilter.status || undefined,
    module: memberFilter.module || undefined,
    quota_type: memberFilter.quota_type || undefined,
    keyword: memberFilter.keyword || undefined,
    limit: memberFilter.limit,
    offset: memberFilter.offset,
  });
  members.value = data;
}

async function loadExpiry() {
  const { data } = await platformApi.expiry({ limit: 200 });
  expiry.value = data;
}

async function loadLedger() {
  const { data } = await platformApi.ledger({
    agent_user_id: ledgerFilter.agent_user_id || undefined,
    quota_type: ledgerFilter.quota_type || undefined,
    action: ledgerFilter.action || undefined,
    start: ledgerFilter.start || undefined,
    end: ledgerFilter.end || undefined,
    limit: ledgerFilter.limit,
    offset: ledgerFilter.offset,
  });
  ledger.value = data;
}

async function loadTemplates() {
  const { data } = await agentApi.templates();
  templates.value = data.items;
}

async function loadAll() {
  loading.value = true;
  try {
    await Promise.all([
      loadOverview(),
      loadAgents(),
      loadMembers(),
      loadExpiry(),
      loadLedger(),
      loadTemplates(),
    ]);
  } catch (error) {
    ElMessage.error(error.message);
  } finally {
    loading.value = false;
  }
}
function onMemberTemplate(code) {
  const template = templates.value.find((item) => item.code === code);
  if (template) memberForm.modules = [...template.modules];
}

function onRenewTemplate(code) {
  const template = templates.value.find((item) => item.code === code);
  if (template) renewForm.modules = [...template.modules];
}

function onPlanTemplate(code) {
  const template = templates.value.find((item) => item.code === code);
  if (template) planForm.modules = [...template.modules];
}

async function submitAgent() {
  if (!agentForm.username.trim()) {
    ElMessage.warning("请填写代理登录账号");
    return;
  }
  const allocate = {};
  for (const item of QUOTA_OPTIONS) {
    const value = Number(agentForm[`${item.value}_alloc`] || 0);
    if (value > 0) allocate[item.value] = value;
  }
  submitting.value = true;
  try {
    const { data } = await agentApi.openAgent({
      username: agentForm.username.trim(),
      display_name: agentForm.display_name || undefined,
      allocate: Object.keys(allocate).length ? allocate : undefined,
      note: agentForm.note || undefined,
    });
    issued.value = { ...data, message: "一级代理已开通" };
    issuedOpen.value = true;
    Object.assign(agentForm, {
      username: "",
      display_name: "",
      member_alloc: 0,
      agent_alloc: 0,
      trial_alloc: 0,
      note: "",
    });
    await loadAll();
  } catch (error) {
    ElMessage.error(error.message);
  } finally {
    submitting.value = false;
  }
}

async function submitMember() {
  if (!memberForm.username.trim()) {
    ElMessage.warning("请填写会员登录账号");
    return;
  }
  submitting.value = true;
  try {
    const { data } = await platformApi.openMember({
      username: memberForm.username.trim(),
      days: memberForm.days,
      template_code: memberForm.template_code || undefined,
      modules: memberForm.modules.length ? memberForm.modules : undefined,
      display_name: memberForm.display_name || undefined,
      owner_agent_id: memberForm.owner_agent_id || undefined,
      note: memberForm.note || undefined,
    });
    issued.value = { ...data, message: "会员已开通" };
    issuedOpen.value = true;
    Object.assign(memberForm, {
      username: "",
      days: 30,
      template_code: "",
      modules: [],
      display_name: "",
      owner_agent_id: null,
      note: "",
    });
    await loadAll();
  } catch (error) {
    ElMessage.error(error.message);
  } finally {
    submitting.value = false;
  }
}

function copyIssued() {
  if (!issued.value) return;
  const text = `账号：${issued.value.account.username}\n初始密码：${issued.value.initial_password}`;
  navigator.clipboard?.writeText(text);
  ElMessage.success("账号与初始密码已复制");
}

function openAlloc(row, mode) {
  allocRow.value = row;
  allocMode.value = mode;
  Object.assign(allocForm, { quota_type: "member", count: 1, note: "" });
  allocVisible.value = true;
}

async function submitAlloc() {
  try {
    const payload = { target_user_id: allocRow.value.user_id, ...allocForm };
    if (allocMode.value === "allocate") {
      await agentApi.allocate(payload);
      ElMessage.success("已划拨");
    } else {
      await agentApi.reclaim(payload);
      ElMessage.success("已回收");
    }
    allocVisible.value = false;
    await loadAll();
  } catch (error) {
    ElMessage.error(error.message);
  }
}

function openAdjust(row) {
  adjustRow.value = row;
  Object.assign(adjustForm, { quota_type: "member", delta: 10, note: "" });
  adjustVisible.value = true;
}

async function submitAdjust() {
  try {
    await platformApi.adjust(adjustRow.value.user_id, { ...adjustForm });
    ElMessage.success("调账完成");
    adjustVisible.value = false;
    await loadAll();
  } catch (error) {
    ElMessage.error(error.message);
  }
}

function openRenew(row) {
  renewRow.value = row;
  Object.assign(renewForm, { days: 30, modules: [], template_code: "", note: "" });
  renewVisible.value = true;
}

async function submitRenew() {
  try {
    await platformApi.renewMember(renewRow.value.user_id, {
      days: renewForm.days,
      template_code: renewForm.template_code || undefined,
      modules: renewForm.modules.length ? renewForm.modules : undefined,
      note: renewForm.note || undefined,
    });
    ElMessage.success("已续期，该客户需登录后手动启动线路");
    renewVisible.value = false;
    await loadAll();
  } catch (error) {
    ElMessage.error(error.message);
  }
}

function openPlan(row) {
  planRow.value = row;
  Object.assign(planForm, { modules: [...(row.modules || [])], template_code: "" });
  planVisible.value = true;
}

async function submitPlan() {
  try {
    await platformApi.setPlan(planRow.value.user_id, {
      template_code: planForm.template_code || undefined,
      modules: planForm.modules,
    });
    ElMessage.success("功能包已更新，前端菜单立即生效");
    planVisible.value = false;
    await loadAll();
  } catch (error) {
    ElMessage.error(error.message);
  }
}

async function toggleEnabled(row) {
  const next = !row.enabled;
  try {
    await ElMessageBox.confirm(
      next
        ? `确认解停 ${row.username}？解停后该账号需登录手动启动线路才恢复功能。`
        : `确认停用 ${row.username}？停用后所有功能立即停止，设置与数据保留，额度不释放。`,
      next ? "解停账号" : "停用账号",
      { type: "warning", confirmButtonText: "确认", cancelButtonText: "取消" },
    );
    await platformApi.setEnabled(row.user_id, {
      enabled: next,
      reason: next ? undefined : "平台停用",
    });
    ElMessage.success(next ? "已解停" : "已停用");
    await loadAll();
  } catch (error) {
    if (error && error.message && !error.message.includes("cancel")) {
      ElMessage.error(error.message);
    }
  }
}

async function showTree(row) {
  try {
    const { data } = await platformApi.agentTree(row.user_id);
    drawerAgent.value = data.account;
    drawerTree.value = data.items;
    drawerVisible.value = true;
  } catch (error) {
    ElMessage.error(error.message);
  }
}

async function exportLedger() {
  try {
    const url = platformApi.ledgerExportUrl({
      agent_user_id: ledgerFilter.agent_user_id || undefined,
      quota_type: ledgerFilter.quota_type || undefined,
      action: ledgerFilter.action || undefined,
      start: ledgerFilter.start || undefined,
      end: ledgerFilter.end || undefined,
    });
    const response = await http.get(url, { responseType: "blob" });
    const objectUrl = URL.createObjectURL(response.data);
    const link = document.createElement("a");
    link.href = objectUrl;
    link.download = `额度台账-${new Date().toISOString().slice(0, 10)}.csv`;
    link.click();
    URL.revokeObjectURL(objectUrl);
    ElMessage.success("已导出台账");
  } catch (error) {
    ElMessage.error(error.message);
  }
}

function onMemberPage(page) {
  memberFilter.offset = (page - 1) * memberFilter.limit;
  loadMembers();
}

function onLedgerPage(page) {
  ledgerFilter.offset = (page - 1) * ledgerFilter.limit;
  loadLedger();
}

onMounted(loadAll);
</script>
<template>
  <div v-loading="loading" class="platform-console">
    <el-card shadow="never">
      <template #header>
        <div class="header">
          <span>平台后台</span>
          <span class="hint">
            {{ auth.username }} · 超管视角：管渠道与客户，不碰客户的业务数据
          </span>
        </div>
      </template>

      <el-row :gutter="12" class="counter-row">
        <el-col v-for="card in counterCards" :key="card.key" :span="6">
          <div class="counter-card">
            <div class="counter-label">{{ card.label }}</div>
            <div class="counter-value">{{ card.value }}</div>
          </div>
        </el-col>
      </el-row>
      <div class="tip today-row">
        今日新开 {{ overview.today?.opened ?? 0 }} 个 · 今日续期
        {{ overview.today?.renewed ?? 0 }} 次（口径与审计页一致）
      </div>

      <el-tabs v-model="activeTab" class="panel">
        <el-tab-pane label="平台总览" name="overview">
          <el-table :data="overview.expiring" size="small" border>
            <el-table-column prop="username" label="客户" min-width="140" />
            <el-table-column label="所属代理" min-width="120">
              <template #default="{ row }">{{ row.agent_username || "平台直开" }}</template>
            </el-table-column>
            <el-table-column label="功能包" min-width="180">
              <template #default="{ row }">{{ memberNames(row) }}</template>
            </el-table-column>
            <el-table-column label="到期日" width="120">
              <template #default="{ row }">{{ fmtDay(row.expires_at) }}</template>
            </el-table-column>
            <el-table-column label="剩余" width="90">
              <template #default="{ row }">
                <el-tag size="small" :type="row.days_left <= 1 ? 'danger' : 'warning'">
                  {{ row.days_left }} 天
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="操作" width="100">
              <template #default="{ row }">
                <el-button size="small" type="primary" @click="openRenew(row)">续期</el-button>
              </template>
            </el-table-column>
            <template #empty>
              <el-empty description="未来 7 天没有到期的账号" />
            </template>
          </el-table>

          <div class="tip">
            续期只改到期日与功能包，不会自动恢复运行：客户登录后需自己手动启动线路。
          </div>

          <el-alert
            v-if="overview.quota_alerts.length"
            class="warn-block"
            type="warning"
            :closable="false"
            show-icon
            :title="`${overview.quota_alerts.length} 个代理的会员额度已用尽`"
            description="这些代理名下还有未到期的客户，续费时无法再占用额度，需要先给他划拨。"
          >
            <div class="warn-list">
              <el-tag
                v-for="item in overview.quota_alerts"
                :key="item.user_id"
                type="warning"
                size="small"
              >
                {{ item.username }}（在占用 {{ item.held }} 个）
              </el-tag>
            </div>
          </el-alert>
        </el-tab-pane>

        <el-tab-pane label="代理管理" name="agents">
          <div class="toolbar">
            <el-input
              v-model="agentKeyword"
              placeholder="按账号或显示名筛选"
              clearable
              style="width: 220px"
              @keyup.enter="loadAgents"
              @clear="loadAgents"
            />
            <el-button @click="loadAgents">查询</el-button>
          </div>

          <el-table :data="agents" size="small" border>
            <el-table-column prop="username" label="代理账号" min-width="140" />
            <el-table-column label="上级" min-width="120">
              <template #default="{ row }">{{ row.parent_username || "（一级代理）" }}</template>
            </el-table-column>
            <el-table-column label="下级代理" width="100">
              <template #default="{ row }">{{ row.subtree_agents }}</template>
            </el-table-column>
            <el-table-column label="名下会员" width="100">
              <template #default="{ row }">{{ row.subtree_members }}</template>
            </el-table-column>
            <el-table-column label="额度" min-width="260">
              <template #default="{ row }">{{ quotaText(row) }}</template>
            </el-table-column>
            <el-table-column label="状态" width="90">
              <template #default="{ row }">
                <el-tag size="small" :type="row.enabled ? 'success' : 'info'">
                  {{ row.enabled ? "正常" : "已停用" }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="操作" width="330">
              <template #default="{ row }">
                <el-button
                  size="small"
                  :disabled="row.depth > 1"
                  :title="row.depth > 1 ? '隔层代理的额度由他的直属上级划拨' : ''"
                  @click="openAlloc(row, 'allocate')"
                >
                  划拨
                </el-button>
                <el-button
                  size="small"
                  :disabled="row.depth > 1"
                  :title="row.depth > 1 ? '隔层代理的额度由他的直属上级回收' : ''"
                  @click="openAlloc(row, 'reclaim')"
                >
                  回收
                </el-button>
                <el-button size="small" @click="openAdjust(row)">调账</el-button>
                <el-button size="small" @click="showTree(row)">下级树</el-button>
                <el-button
                  size="small"
                  :type="row.enabled ? 'danger' : 'primary'"
                  @click="toggleEnabled(row)"
                >
                  {{ row.enabled ? "停用" : "解停" }}
                </el-button>
              </template>
            </el-table-column>
            <template #empty>
              <el-empty description="还没有代理账号，先在下面开一个一级代理" />
            </template>
          </el-table>

          <el-divider content-position="left">开一级代理</el-divider>
          <el-form label-width="120px" class="open-form">
            <el-form-item label="登录账号">
              <el-input v-model="agentForm.username" placeholder="代理登录用的账号" />
            </el-form-item>
            <el-form-item label="显示名">
              <el-input v-model="agentForm.display_name" placeholder="可留空" />
            </el-form-item>
            <el-form-item label="同时划拨">
              <div class="alloc-row">
                <span class="tip inline">会员</span>
                <el-input-number v-model="agentForm.member_alloc" :min="0" />
                <span class="tip inline">代理</span>
                <el-input-number v-model="agentForm.agent_alloc" :min="0" />
                <span class="tip inline">试用</span>
                <el-input-number v-model="agentForm.trial_alloc" :min="0" />
              </div>
            </el-form-item>
            <el-form-item label="备注">
              <el-input v-model="agentForm.note" placeholder="可留空" />
            </el-form-item>
            <el-form-item>
              <el-button type="primary" :loading="submitting" @click="submitAgent">
                开通一级代理（开号与划拨是两笔）
              </el-button>
              <span class="tip inline">停用代理不影响名下已开会员，会员照常跑到自己的到期日</span>
            </el-form-item>
          </el-form>
        </el-tab-pane>
        <el-tab-pane label="会员管理" name="members">
          <div class="toolbar">
            <el-select
              v-model="memberFilter.agent_user_id"
              clearable
              placeholder="所属代理"
              style="width: 180px"
              @change="loadMembers"
            >
              <el-option
                v-for="item in agentOptions"
                :key="item.value"
                :label="item.label"
                :value="item.value"
              />
            </el-select>
            <el-select
              v-model="memberFilter.status"
              clearable
              placeholder="状态"
              style="width: 130px"
              @change="loadMembers"
            >
              <el-option label="生效中" value="active" />
              <el-option label="已过期" value="expired" />
              <el-option label="已停用" value="suspended" />
            </el-select>
            <el-select
              v-model="memberFilter.module"
              clearable
              placeholder="功能块"
              style="width: 140px"
              @change="loadMembers"
            >
              <el-option
                v-for="item in MODULE_OPTIONS"
                :key="item.value"
                :label="item.label"
                :value="item.value"
              />
            </el-select>
            <el-select
              v-model="memberFilter.quota_type"
              clearable
              placeholder="额度类型"
              style="width: 140px"
              @change="loadMembers"
            >
              <el-option label="会员额度" value="member" />
              <el-option label="试用额度" value="trial" />
              <el-option label="不占额度" value="none" />
            </el-select>
            <el-input
              v-model="memberFilter.keyword"
              placeholder="账号 / 客户名"
              clearable
              style="width: 180px"
              @keyup.enter="loadMembers"
              @clear="loadMembers"
            />
            <el-button @click="loadMembers">查询</el-button>
          </div>

          <el-table :data="members.items" size="small" border>
            <el-table-column prop="username" label="登录账号" min-width="130" />
            <el-table-column prop="display_name" label="客户名" min-width="120" />
            <el-table-column label="所属代理" min-width="110">
              <template #default="{ row }">{{ row.agent_username || "平台直开" }}</template>
            </el-table-column>
            <el-table-column label="功能包" min-width="170">
              <template #default="{ row }">{{ memberNames(row) }}</template>
            </el-table-column>
            <el-table-column label="到期日" width="115">
              <template #default="{ row }">{{ fmtDay(row.expires_at) }}</template>
            </el-table-column>
            <el-table-column label="状态" width="95">
              <template #default="{ row }">
                <el-tag size="small" :type="statusTag(row).type">{{ statusTag(row).text }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="额度占用" width="110">
              <template #default="{ row }">
                <el-tag size="small" :type="row.quota_held ? 'warning' : 'info'">
                  {{ row.quota_held ? "持有" : "已释放" }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="操作" width="280">
              <template #default="{ row }">
                <el-button size="small" type="primary" @click="openRenew(row)">续期</el-button>
                <el-button size="small" @click="openPlan(row)">改功能包</el-button>
                <el-button size="small" @click="openAdjust(row)">调账</el-button>
                <el-button
                  size="small"
                  :type="row.enabled ? 'danger' : 'primary'"
                  @click="toggleEnabled(row)"
                >
                  {{ row.enabled ? "停用" : "解停" }}
                </el-button>
              </template>
            </el-table-column>
            <template #empty>
              <el-empty description="还没有会员账号" />
            </template>
          </el-table>
          <el-pagination
            class="pager"
            layout="total, prev, pager, next"
            :total="members.total"
            :page-size="memberFilter.limit"
            :current-page="memberFilter.offset / memberFilter.limit + 1"
            @current-change="onMemberPage"
          />

          <el-divider content-position="left">开会员</el-divider>
          <el-form label-width="120px" class="open-form">
            <el-form-item label="登录账号">
              <el-input v-model="memberForm.username" placeholder="会员登录用的账号" />
            </el-form-item>
            <el-form-item label="开通天数">
              <el-input-number v-model="memberForm.days" :min="1" :max="3650" />
              <span class="tip inline">到期日当天 23:59:59 断，无宽限期</span>
            </el-form-item>
            <el-form-item label="功能包">
              <el-select
                v-model="memberForm.template_code"
                clearable
                placeholder="选模板可自动勾选功能块"
                @change="onMemberTemplate"
              >
                <el-option
                  v-for="item in templates"
                  :key="item.code"
                  :label="item.name"
                  :value="item.code"
                />
              </el-select>
            </el-form-item>
            <el-form-item label="功能块">
              <el-checkbox-group v-model="memberForm.modules">
                <el-checkbox
                  v-for="item in MODULE_OPTIONS"
                  :key="item.value"
                  :value="item.value"
                >
                  {{ item.label }}
                </el-checkbox>
              </el-checkbox-group>
            </el-form-item>
            <el-form-item label="归属代理">
              <el-select
                v-model="memberForm.owner_agent_id"
                clearable
                placeholder="不选 = 平台直开，不占额度"
                style="width: 260px"
              >
                <el-option
                  v-for="item in agentOptions"
                  :key="item.value"
                  :label="item.label"
                  :value="item.value"
                />
              </el-select>
              <span class="tip inline">选了就从该代理账上扣 1 个会员额度</span>
            </el-form-item>
            <el-form-item label="显示名">
              <el-input v-model="memberForm.display_name" placeholder="可留空" />
            </el-form-item>
            <el-form-item label="备注">
              <el-input v-model="memberForm.note" placeholder="可留空，会写进额度流水" />
            </el-form-item>
            <el-form-item>
              <el-button type="primary" :loading="submitting" @click="submitMember">
                开通会员
              </el-button>
            </el-form-item>
          </el-form>
        </el-tab-pane>
        <el-tab-pane label="额度台账" name="ledger">
          <div class="toolbar">
            <el-select
              v-model="ledgerFilter.agent_user_id"
              clearable
              placeholder="代理"
              style="width: 180px"
              @change="loadLedger"
            >
              <el-option
                v-for="item in agentOptions"
                :key="item.value"
                :label="item.label"
                :value="item.value"
              />
            </el-select>
            <el-select
              v-model="ledgerFilter.quota_type"
              clearable
              placeholder="额度类型"
              style="width: 140px"
              @change="loadLedger"
            >
              <el-option
                v-for="item in QUOTA_OPTIONS"
                :key="item.value"
                :label="item.label"
                :value="item.value"
              />
            </el-select>
            <el-date-picker
              v-model="ledgerFilter.start"
              type="date"
              placeholder="起始日期"
              value-format="YYYY-MM-DD"
              style="width: 160px"
              @change="loadLedger"
            />
            <el-date-picker
              v-model="ledgerFilter.end"
              type="date"
              placeholder="结束日期"
              value-format="YYYY-MM-DD"
              style="width: 160px"
              @change="loadLedger"
            />
            <el-button @click="loadLedger">查询</el-button>
            <el-button type="primary" plain @click="exportLedger">导出台账</el-button>
          </div>

          <el-table :data="ledger.items" size="small" border>
            <el-table-column label="时间" width="170">
              <template #default="{ row }">{{ fmt(row.created_at) }}</template>
            </el-table-column>
            <el-table-column prop="actor_username" label="操作者" min-width="110" />
            <el-table-column prop="subject_username" label="对象" min-width="110" />
            <el-table-column prop="quota_label" label="额度类型" width="100" />
            <el-table-column label="变动量" width="100">
              <template #default="{ row }">
                <span :class="row.change >= 0 ? 'delta-up' : 'delta-down'">
                  {{ row.change >= 0 ? `+${row.change}` : `-${Math.abs(row.change)}` }}
                </span>
              </template>
            </el-table-column>
            <el-table-column prop="balance_after" label="变动后余额" width="110" />
            <el-table-column prop="action_label" label="动作" width="120" />
            <el-table-column prop="note" label="备注" min-width="160" />
            <template #empty>
              <el-empty description="没有额度变动记录" />
            </template>
          </el-table>
          <el-pagination
            class="pager"
            layout="total, prev, pager, next"
            :total="ledger.total"
            :page-size="ledgerFilter.limit"
            :current-page="ledgerFilter.offset / ledgerFilter.limit + 1"
            @current-change="onLedgerPage"
          />
        </el-tab-pane>

        <el-tab-pane label="到期看板" name="expiry">
          <div v-for="group in expiryGroups" :key="group.key" class="bucket">
            <div class="bucket-title">
              <span>{{ group.label }}</span>
              <el-tag size="small" type="info">{{ group.count }}</el-tag>
            </div>
            <el-table :data="group.items" size="small" border>
              <el-table-column prop="username" label="客户" min-width="140" />
              <el-table-column label="所属代理" min-width="110">
                <template #default="{ row }">{{ row.agent_username || "平台直开" }}</template>
              </el-table-column>
              <el-table-column label="到期日" width="120">
                <template #default="{ row }">{{ fmtDay(row.expires_at) }}</template>
              </el-table-column>
              <el-table-column label="状态" width="95">
                <template #default="{ row }">
                  <el-tag size="small" :type="statusTag(row).type">{{ statusTag(row).text }}</el-tag>
                </template>
              </el-table-column>
              <el-table-column label="额度占用" width="110">
                <template #default="{ row }">
                  <el-tag size="small" :type="row.quota_held ? 'warning' : 'info'">
                    {{ row.quota_held ? "持有" : "已释放" }}
                  </el-tag>
                </template>
              </el-table-column>
              <el-table-column label="操作" width="100">
                <template #default="{ row }">
                  <el-button size="small" type="primary" @click="openRenew(row)">续期</el-button>
                </template>
              </el-table-column>
              <template #empty>
                <el-empty description="无" />
              </template>
            </el-table>
          </div>
        </el-tab-pane>
      </el-tabs>
    </el-card>
    <el-dialog
      v-model="allocVisible"
      :title="allocMode === 'allocate' ? '划拨额度' : '回收额度'"
      width="420px"
    >
      <el-form label-width="90px">
        <el-form-item label="对象">{{ allocRow?.username }}</el-form-item>
        <el-form-item label="额度类型">
          <el-select v-model="allocForm.quota_type">
            <el-option
              v-for="item in QUOTA_OPTIONS"
              :key="item.value"
              :label="item.label"
              :value="item.value"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="数量">
          <el-input-number v-model="allocForm.count" :min="1" />
        </el-form-item>
        <el-form-item label="备注">
          <el-input v-model="allocForm.note" placeholder="可留空" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="allocVisible = false">取消</el-button>
        <el-button type="primary" @click="submitAlloc">确认</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="renewVisible" title="续期" width="460px">
      <el-form label-width="110px">
        <el-form-item label="客户">{{ renewRow?.username }}</el-form-item>
        <el-form-item label="增加天数">
          <el-input-number v-model="renewForm.days" :min="1" :max="3650" />
        </el-form-item>
        <el-form-item label="功能包">
          <el-select
            v-model="renewForm.template_code"
            clearable
            placeholder="不选 = 保持现有功能包"
            @change="onRenewTemplate"
          >
            <el-option
              v-for="item in templates"
              :key="item.code"
              :label="item.name"
              :value="item.code"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="功能块">
          <el-checkbox-group v-model="renewForm.modules">
            <el-checkbox v-for="item in MODULE_OPTIONS" :key="item.value" :value="item.value">
              {{ item.label }}
            </el-checkbox>
          </el-checkbox-group>
        </el-form-item>
        <el-form-item label="备注">
          <el-input v-model="renewForm.note" placeholder="可留空" />
        </el-form-item>
      </el-form>
      <el-alert
        type="info"
        :closable="false"
        show-icon
        title="续期不会自动恢复运行"
        description="到期日从今天重新起算；客户登录后需要自己手动启动线路。额度已释放的会重新从归属代理账上扣 1 个。"
      />
      <template #footer>
        <el-button @click="renewVisible = false">取消</el-button>
        <el-button type="primary" @click="submitRenew">确认续期</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="planVisible" title="改功能包" width="460px">
      <el-form label-width="110px">
        <el-form-item label="客户">{{ planRow?.username }}</el-form-item>
        <el-form-item label="模板">
          <el-select
            v-model="planForm.template_code"
            clearable
            placeholder="可选"
            @change="onPlanTemplate"
          >
            <el-option
              v-for="item in templates"
              :key="item.code"
              :label="item.name"
              :value="item.code"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="功能块">
          <el-checkbox-group v-model="planForm.modules">
            <el-checkbox v-for="item in MODULE_OPTIONS" :key="item.value" :value="item.value">
              {{ item.label }}
            </el-checkbox>
          </el-checkbox-group>
        </el-form-item>
      </el-form>
      <div class="tip">
        改完立即生效；已配的 TG 账号、机器人、线路、线索全部保留，到期日不变。
      </div>
      <template #footer>
        <el-button @click="planVisible = false">取消</el-button>
        <el-button type="primary" @click="submitPlan">保存</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="adjustVisible" title="手工调账" width="420px">
      <el-form label-width="90px">
        <el-form-item label="对象">{{ adjustRow?.username }}</el-form-item>
        <el-form-item label="额度类型">
          <el-select v-model="adjustForm.quota_type">
            <el-option
              v-for="item in QUOTA_OPTIONS"
              :key="item.value"
              :label="item.label"
              :value="item.value"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="增减">
          <el-input-number v-model="adjustForm.delta" />
        </el-form-item>
        <el-form-item label="备注">
          <el-input v-model="adjustForm.note" placeholder="必填，用于对账" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="adjustVisible = false">取消</el-button>
        <el-button type="primary" @click="submitAdjust">确认调账</el-button>
      </template>
    </el-dialog>

    <el-drawer
      v-model="drawerVisible"
      size="620px"
      :title="`下级树 · ${drawerAgent?.username || ''}`"
    >
      <el-table :data="drawerTree" size="small" border>
        <el-table-column prop="username" label="账号" min-width="140" />
        <el-table-column label="类型" width="90">
          <template #default="{ row }">
            {{ row.account_type === "agent" ? "代理" : "会员" }}
          </template>
        </el-table-column>
        <el-table-column label="层级" width="110">
          <template #default="{ row }">
            <el-tag size="small" :type="row.is_direct ? 'success' : 'info'">
              {{ row.is_direct ? "直属" : `隔层(${row.depth}层)` }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="到期时间" min-width="170">
          <template #default="{ row }">{{ fmt(row.expires_at) }}</template>
        </el-table-column>
      </el-table>
      <div class="tip">只读视图：代理看不到任何下级会员的业务数据。</div>
    </el-drawer>

    <el-dialog v-model="issuedOpen" title="开通成功（初始密码只显示这一次）" width="460px">
      <div v-if="issued">
        <div class="issued-line">账号：{{ issued.account.username }}</div>
        <div class="issued-line">初始密码：{{ issued.initial_password }}</div>
        <div class="issued-line">
          功能块：{{ (issued.module_labels || []).join(" · ") || "仅基础功能" }}
        </div>
      </div>
      <template #footer>
        <el-button @click="issuedOpen = false">关闭</el-button>
        <el-button type="primary" @click="copyIssued">复制账号密码</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.hint {
  color: #909399;
  font-size: 12px;
}
.counter-row {
  margin-bottom: 8px;
}
.counter-card {
  border: 1px solid #ebeef5;
  border-radius: 6px;
  padding: 10px 12px;
}
.counter-label {
  color: #909399;
  font-size: 12px;
}
.counter-value {
  font-size: 22px;
  font-weight: 600;
  margin-top: 4px;
}
.today-row {
  margin: 6px 0 0;
}
.panel {
  margin-top: 12px;
}
.toolbar {
  display: flex;
  gap: 8px;
  align-items: center;
  margin-bottom: 10px;
  flex-wrap: wrap;
}
.tip {
  color: #909399;
  font-size: 12px;
  margin-top: 8px;
}
.tip.inline {
  margin-left: 8px;
}
.open-form {
  max-width: 760px;
}
.alloc-row {
  display: flex;
  align-items: center;
  gap: 8px;
}
.warn-block {
  margin-top: 12px;
}
.warn-list {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
  margin-top: 6px;
}
.bucket {
  margin-bottom: 18px;
}
.bucket-title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-weight: 600;
  margin-bottom: 8px;
}
.pager {
  margin-top: 10px;
  justify-content: flex-end;
}
.delta-up {
  color: #67c23a;
}
.delta-down {
  color: #f56c6c;
}
.issued-line {
  margin-bottom: 6px;
}
</style>