<script setup>
import { ElMessage, ElMessageBox } from "element-plus";
import { computed, onMounted, reactive, ref } from "vue";

import { agentApi } from "../api";
import { auth } from "../stores/auth";

const MODULE_OPTIONS = [
  { value: "carry", label: "搬运帖子" },
  { value: "monitor", label: "监听会员" },
  { value: "discovery", label: "资源发现" },
];

const loading = ref(false);
const activeTab = ref("subordinates");
const quota = ref({ member: 0, agent: 0, trial: 0 });
const held = ref({ member: 0, trial: 0 });
const unlimited = ref(false);
const stats = ref({});
const expiring = ref([]);
const templates = ref([]);
const subordinates = ref([]);
const ledger = ref({ items: [], total: 0 });

const ledgerFilter = reactive({ quota_type: "", action: "", scope: "subtree" });

const openMode = ref("member");
// 会员默认全功能：开通了就能用搬运 / 监听 / 资源发现，代理系统除外。
const FULL_MODULES = ["carry", "monitor", "discovery"];

const memberForm = reactive({
  username: "",
  days: 30,
  template_code: "",
  modules: [...FULL_MODULES],
  display_name: "",
  note: "",
});
const trialForm = reactive({ username: "", template_code: "trial_carry", display_name: "" });
const agentForm = reactive({
  username: "",
  display_name: "",
  member_alloc: 0,
  agent_alloc: 0,
  trial_alloc: 0,
});
const submitting = ref(false);
const issued = ref(null);

const renewVisible = ref(false);
const renewRow = ref(null);
const renewDays = ref(30);

const adjustVisible = ref(false);
const adjustRow = ref(null);
const adjustForm = reactive({ quota_type: "member", delta: 10, note: "" });

const isPlatform = computed(() => auth.isPlatform);
const quotaCards = computed(() => [
  { key: "member", label: "会员额度", value: quota.value.member, used: held.value.member },
  { key: "agent", label: "代理额度", value: quota.value.agent, used: null },
  { key: "trial", label: "试用额度", value: quota.value.trial, used: held.value.trial },
]);

function fmt(value) {
  return value ? new Date(value).toLocaleString("zh-CN") : "-";
}

function statusTag(row) {
  if (!row.enabled) return { type: "info", text: "已停用" };
  if (row.tenant_status === "expired" || row.expired) return { type: "danger", text: "已过期" };
  if (row.tenant_status === "suspended") return { type: "info", text: "已停用" };
  if (row.account_type === "agent") return { type: "warning", text: "代理" };
  return { type: "success", text: "生效中" };
}

function quotaText(row) {
  if (!row.quota) return "-";
  return `会员 ${row.quota.member} / 代理 ${row.quota.agent} / 试用 ${row.quota.trial}`;
}

function remindedTip(stages) {
  if (!stages || !stages.length) return "";
  return `已提醒 ${stages.map((stage) => String(stage).replace("d", " 天")).join(" / ")}`;
}

async function loadAll() {
  loading.value = true;
  try {
    const [quotaRes, statsRes, expiringRes, templateRes, subRes, ledgerRes] = await Promise.all([
      agentApi.quota(),
      agentApi.stats(),
      agentApi.expiring({ days: 7 }),
      agentApi.templates(),
      agentApi.subordinates(),
      agentApi.ledger({ ...ledgerFilter, limit: 50 }),
    ]);
    quota.value = quotaRes.data.quota;
    held.value = quotaRes.data.held;
    unlimited.value = quotaRes.data.unlimited;
    stats.value = statsRes.data;
    expiring.value = expiringRes.data.items;
    templates.value = templateRes.data.items;
    subordinates.value = subRes.data.items;
    ledger.value = ledgerRes.data;
  } catch (error) {
    ElMessage.error(error.message);
  } finally {
    loading.value = false;
  }
}

async function loadLedger() {
  try {
    const { data } = await agentApi.ledger({ ...ledgerFilter, limit: 50 });
    ledger.value = data;
  } catch (error) {
    ElMessage.error(error.message);
  }
}

function onTemplateChange(code) {
  const template = templates.value.find((item) => item.code === code);
  if (template) memberForm.modules = [...template.modules];
}

function afterIssue(data, message) {
  issued.value = { ...data, message };
  ElMessage.success(message);
  memberForm.username = "";
  memberForm.template_code = "";
  memberForm.modules = [...FULL_MODULES];
  trialForm.username = "";
  agentForm.username = "";
  loadAll();
}

async function submitMember() {
  if (!memberForm.username.trim()) {
    ElMessage.warning("请填写会员登录账号");
    return;
  }
  if (!memberForm.modules.length && !memberForm.template_code) {
    ElMessage.warning("会员至少要有一个功能块：一个都不给，客户进去只剩「账号与机器人」");
    return;
  }
  submitting.value = true;
  try {
    const { data } = await agentApi.openMember({
      username: memberForm.username,
      days: memberForm.days,
      modules: memberForm.modules,
      template_code: memberForm.template_code || null,
      display_name: memberForm.display_name || null,
      note: memberForm.note || null,
    });
    afterIssue(data, `会员 ${data.account.username} 已开通`);
  } catch (error) {
    ElMessage.error(error.message);
  } finally {
    submitting.value = false;
  }
}

async function submitTrial() {
  submitting.value = true;
  try {
    const { data } = await agentApi.openTrial({
      username: trialForm.username,
      template_code: trialForm.template_code,
      display_name: trialForm.display_name || null,
    });
    afterIssue(data, `试用账号 ${data.account.username} 已开通（1 天）`);
  } catch (error) {
    ElMessage.error(error.message);
  } finally {
    submitting.value = false;
  }
}

async function submitAgent() {
  submitting.value = true;
  try {
    const allocate = {};
    if (agentForm.member_alloc > 0) allocate.member = agentForm.member_alloc;
    if (agentForm.agent_alloc > 0) allocate.agent = agentForm.agent_alloc;
    if (agentForm.trial_alloc > 0) allocate.trial = agentForm.trial_alloc;
    const { data } = await agentApi.openAgent({
      username: agentForm.username,
      display_name: agentForm.display_name || null,
      allocate: Object.keys(allocate).length ? allocate : null,
    });
    afterIssue(data, `下级代理 ${data.account.username} 已开通`);
  } catch (error) {
    ElMessage.error(error.message);
  } finally {
    submitting.value = false;
  }
}

function openRenew(row) {
  renewRow.value = row;
  renewDays.value = 30;
  renewVisible.value = true;
}

async function submitRenew() {
  try {
    await agentApi.renew(renewRow.value.id, { days: renewDays.value });
    ElMessage.success("已续期，会员登录后需要手动启动线路");
    renewVisible.value = false;
    loadAll();
  } catch (error) {
    ElMessage.error(error.message);
  }
}

async function toggleEnabled(row) {
  const next = !row.enabled;
  try {
    await ElMessageBox.confirm(
      next
        ? `确认启用 ${row.username}？启用后该账号的线路仍需其本人登录手动启动。`
        : `确认停用 ${row.username}？停用后所有功能立即停止，设置会保留。`,
      next ? "启用账号" : "停用账号",
      { type: "warning", confirmButtonText: "确认", cancelButtonText: "取消" },
    );
    await agentApi.setEnabled(row.id, next);
    ElMessage.success(next ? "已启用" : "已停用");
    loadAll();
  } catch (error) {
    if (error && error.message && !error.message.includes("cancel")) {
      ElMessage.error(error.message);
    }
  }
}

function adjust(row) {
  adjustRow.value = row;
  Object.assign(adjustForm, { quota_type: "member", delta: 10, note: "" });
  adjustVisible.value = true;
}

async function submitAdjust() {
  try {
    await agentApi.adjust(adjustRow.value.id, { ...adjustForm });
    ElMessage.success("调账完成");
    adjustVisible.value = false;
    loadAll();
  } catch (error) {
    ElMessage.error(error.message);
  }
}

function copyIssued() {
  if (!issued.value) return;
  const text = `账号：${issued.value.account.username}\n初始密码：${issued.value.initial_password}`;
  navigator.clipboard?.writeText(text);
  ElMessage.success("账号与初始密码已复制");
}

onMounted(loadAll);
</script>

<template>
  <div class="agent-console">
    <el-card shadow="never">
      <template #header>
        <div class="header">
          <span>代理工作台</span>
          <span class="hint">
            {{ auth.username }} · 代理账号只负责开号，不带任何业务功能
          </span>
        </div>
      </template>

      <el-alert
        v-if="unlimited"
        type="info"
        :closable="false"
        show-icon
        title="平台账号不受额度限制"
        description="平台开的会员不占用任何代理额度，也不需要向上级申请。"
      />

      <el-row :gutter="12" class="quota-row">
        <el-col v-for="card in quotaCards" :key="card.key" :span="8">
          <div class="quota-card">
            <div class="quota-label">{{ card.label }}</div>
            <div class="quota-value">{{ unlimited ? "不限" : card.value }}</div>
            <div v-if="card.used" class="quota-sub">已开占用 {{ card.used }} 个</div>
          </div>
        </el-col>
      </el-row>

      <el-descriptions :column="5" border size="small" class="stats-row">
        <el-descriptions-item label="本月开号">{{ stats.opened_this_month ?? 0 }}</el-descriptions-item>
        <el-descriptions-item label="本月续期">{{ stats.renewed_this_month ?? 0 }}</el-descriptions-item>
        <el-descriptions-item label="活跃会员">{{ stats.active_members ?? 0 }}</el-descriptions-item>
        <el-descriptions-item label="即将到期">{{ stats.expiring_soon ?? 0 }}</el-descriptions-item>
        <el-descriptions-item label="已过期">{{ stats.expired ?? 0 }}</el-descriptions-item>
      </el-descriptions>

      <el-alert
        v-if="expiring.length"
        class="warn-row"
        type="warning"
        :closable="false"
        show-icon
        :title="`有 ${expiring.length} 个账号 7 天内到期`"
        description="到期会释放占用额度；续费需要重新占用 1 个会员额度，请预留额度。"
      >
        <div class="warn-list">
          <el-tag v-for="item in expiring" :key="item.tenant_id" type="warning" size="small">
            {{ item.username }} · {{ fmt(item.expires_at) }}
            <span v-if="item.reminded_stages?.length" class="reminded">
              · {{ remindedTip(item.reminded_stages) }}
            </span>
          </el-tag>
        </div>
      </el-alert>
    </el-card>

    <el-card shadow="never" class="panel">
      <el-tabs v-model="activeTab">
        <el-tab-pane label="我的下级" name="subordinates">
          <el-table v-loading="loading" :data="subordinates" size="small" border>
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
            <el-table-column label="状态" width="100">
              <template #default="{ row }">
                <el-tag size="small" :type="statusTag(row).type">{{ statusTag(row).text }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="到期时间" min-width="170">
              <template #default="{ row }">{{ fmt(row.expires_at) }}</template>
            </el-table-column>
            <el-table-column label="剩余天数" width="100">
              <template #default="{ row }">
                {{ row.days_left === null || row.days_left === undefined ? "-" : `${row.days_left} 天` }}
              </template>
            </el-table-column>
            <el-table-column label="额度" min-width="190">
              <template #default="{ row }">{{ quotaText(row) }}</template>
            </el-table-column>
            <el-table-column label="操作" width="170" fixed="right">
              <template #default="{ row }">
                <el-button
                  size="small"
                  :disabled="!row.is_direct || row.account_type !== 'member'"
                  @click="openRenew(row)"
                >
                  续期
                </el-button>
                <el-button
                  size="small"
                  :type="row.enabled ? 'danger' : 'primary'"
                  :disabled="!row.is_direct"
                  @click="toggleEnabled(row)"
                >
                  {{ row.enabled ? "停用" : "启用" }}
                </el-button>
              </template>
            </el-table-column>
            <template #empty>
              <el-empty description="还没有下级账号，先在「开号」里开一个" />
            </template>
          </el-table>
          <div class="tip">
            隔层账号只读：能看，不能改。代理看不到下级会员的业务数据（TG 账号 / 线路 / 线索）。
          </div>
        </el-tab-pane>

        <el-tab-pane label="开号" name="open">
          <el-radio-group v-model="openMode" class="mode-row">
            <el-radio-button value="member">开正式会员</el-radio-button>
            <el-radio-button value="trial">开试用（1 天）</el-radio-button>
            <el-radio-button value="agent">开下级代理</el-radio-button>
          </el-radio-group>

          <el-form v-if="openMode === 'member'" label-width="110px" class="open-form">
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
                @change="onTemplateChange"
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
              <span class="tip inline">会员默认全功能，想少给就手动取消勾选</span>
            </el-form-item>
            <el-form-item label="显示名">
              <el-input v-model="memberForm.display_name" placeholder="可留空" />
            </el-form-item>
            <el-form-item label="备注">
              <el-input v-model="memberForm.note" placeholder="可留空，会写进额度流水" />
            </el-form-item>
            <el-form-item>
              <el-button type="primary" :loading="submitting" @click="submitMember">
                开通会员（占用 1 个会员额度）
              </el-button>
            </el-form-item>
          </el-form>

          <el-form v-else-if="openMode === 'trial'" label-width="110px" class="open-form">
            <el-form-item label="登录账号">
              <el-input v-model="trialForm.username" placeholder="试用账号" />
            </el-form-item>
            <el-form-item label="试用包">
              <el-radio-group v-model="trialForm.template_code">
                <el-radio value="trial_carry">搬运（1 条线路）</el-radio>
                <el-radio value="trial_monitor">监听（1 条线路）</el-radio>
              </el-radio-group>
            </el-form-item>
            <el-form-item label="显示名">
              <el-input v-model="trialForm.display_name" placeholder="可留空" />
            </el-form-item>
            <el-form-item>
              <el-button type="primary" :loading="submitting" @click="submitTrial">
                开通试用（固定 1 天，占用 1 个试用额度）
              </el-button>
            </el-form-item>
          </el-form>

          <el-form v-else label-width="110px" class="open-form">
            <el-form-item label="登录账号">
              <el-input v-model="agentForm.username" placeholder="下级代理登录账号" />
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
            <el-form-item>
              <el-button type="primary" :loading="submitting" @click="submitAgent">
                开通下级代理（占用 1 个代理额度）
              </el-button>
            </el-form-item>
          </el-form>
        </el-tab-pane>

        <el-tab-pane label="额度流水" name="ledger">
          <div class="filter-row">
            <el-select v-model="ledgerFilter.quota_type" clearable placeholder="额度类型" size="small">
              <el-option label="会员额度" value="member" />
              <el-option label="代理额度" value="agent" />
              <el-option label="试用额度" value="trial" />
            </el-select>
            <el-select v-model="ledgerFilter.scope" size="small">
              <el-option label="我的流水" value="self" />
              <el-option label="整棵子树" value="subtree" />
            </el-select>
            <el-button size="small" @click="loadLedger">查询</el-button>
          </div>
          <el-table :data="ledger.items" size="small" border>
            <el-table-column prop="created_at" label="时间" min-width="170">
              <template #default="{ row }">{{ fmt(row.created_at) }}</template>
            </el-table-column>
            <el-table-column prop="subject_username" label="对象" min-width="120" />
            <el-table-column prop="quota_label" label="额度" width="100" />
            <el-table-column prop="action_label" label="动作" width="120" />
            <el-table-column label="变动" width="80">
              <template #default="{ row }">
                <span :class="row.change > 0 ? 'plus' : 'minus'">{{ row.change }}</span>
              </template>
            </el-table-column>
            <el-table-column prop="balance_after" label="变动后余额" width="110" />
            <el-table-column prop="actor_username" label="操作者" width="120" />
            <el-table-column prop="note" label="备注" min-width="140" />
            <template #empty>
              <el-empty description="还没有额度流水" />
            </template>
          </el-table>
        </el-tab-pane>

        <el-tab-pane v-if="isPlatform" label="平台调账" name="adjust">
          <el-alert
            type="info"
            :closable="false"
            show-icon
            title="平台调账不受层级限制"
            description="代理之间的划拨只能由代理自己做；平台要发额度时，用代理列表里的「调账」按钮。"
          />
          <el-table :data="subordinates.filter((row) => row.account_type === 'agent')" size="small">
            <el-table-column prop="username" label="代理账号" min-width="140" />
            <el-table-column label="额度" min-width="190">
              <template #default="{ row }">{{ quotaText(row) }}</template>
            </el-table-column>
            <el-table-column label="操作" width="120">
              <template #default="{ row }">
                <el-button size="small" @click="adjust(row)">调账</el-button>
              </template>
            </el-table-column>
          </el-table>
        </el-tab-pane>
      </el-tabs>
    </el-card>

    <el-dialog v-model="renewVisible" title="续期" width="420px">
      <p class="tip">续期只延长到期时间，不会自动恢复线路——会员登录后需要自己手动启动。</p>
      <el-form label-width="90px">
        <el-form-item label="账号">{{ renewRow?.username }}</el-form-item>
        <el-form-item label="续期天数">
          <el-input-number v-model="renewDays" :min="1" :max="3650" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="renewVisible = false">取消</el-button>
        <el-button type="primary" @click="submitRenew">确认续期</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="adjustVisible" title="平台调账" width="440px">
      <p class="tip">调账可正可负，必须填备注，会写进额度流水。</p>
      <el-form label-width="90px">
        <el-form-item label="账号">{{ adjustRow?.username }}</el-form-item>
        <el-form-item label="额度类型">
          <el-select v-model="adjustForm.quota_type">
            <el-option label="会员额度" value="member" />
            <el-option label="代理额度" value="agent" />
            <el-option label="试用额度" value="trial" />
          </el-select>
        </el-form-item>
        <el-form-item label="变动数量">
          <el-input-number v-model="adjustForm.delta" :min="-100000" :max="100000" />
        </el-form-item>
        <el-form-item label="备注">
          <el-input v-model="adjustForm.note" placeholder="例如：补偿发放" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="adjustVisible = false">取消</el-button>
        <el-button type="primary" @click="submitAdjust">确认调账</el-button>
      </template>
    </el-dialog>

    <el-dialog :model-value="Boolean(issued)" title="开通成功" width="460px" @close="issued = null">
      <p class="tip">初始密码只在这里显示一次，请立刻发给客户；对方首次登录必须改密。</p>
      <el-descriptions :column="1" border size="small">
        <el-descriptions-item label="登录账号">
          {{ issued?.account?.username }}
        </el-descriptions-item>
        <el-descriptions-item label="初始密码">
          <code>{{ issued?.initial_password }}</code>
        </el-descriptions-item>
        <el-descriptions-item v-if="issued?.tenant?.expires_at" label="到期时间">
          {{ fmt(issued.tenant.expires_at) }}
        </el-descriptions-item>
      </el-descriptions>
      <template #footer>
        <el-button @click="copyIssued">复制</el-button>
        <el-button type="primary" @click="issued = null">已发给客户</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.agent-console {
  padding: 4px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.hint,
.tip {
  color: #909399;
  font-size: 12px;
}

.tip {
  margin-top: 8px;
}

.tip.inline {
  margin-left: 8px;
}

.quota-row,
.stats-row {
  margin-top: 12px;
}

.quota-card {
  border: 1px solid #ebeef5;
  border-radius: 6px;
  padding: 12px;
  text-align: center;
}

.quota-label {
  color: #909399;
  font-size: 12px;
}

.quota-value {
  font-size: 26px;
  font-weight: 600;
  line-height: 1.4;
}

.quota-sub {
  color: #e6a23c;
  font-size: 12px;
}

.warn-row {
  margin-top: 12px;
}

.warn-list {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 6px;
}

.reminded {
  margin-left: 4px;
  font-size: 12px;
  opacity: 0.8;
}

.mode-row {
  margin-bottom: 12px;
}

.open-form {
  max-width: 620px;
}

.alloc-row {
  display: flex;
  align-items: center;
  gap: 8px;
}

.filter-row {
  display: flex;
  gap: 8px;
  margin-bottom: 10px;
}

.plus {
  color: #67c23a;
}

.minus {
  color: #f56c6c;
}
</style>