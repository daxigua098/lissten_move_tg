<script setup>
import { ElMessage } from "element-plus";
import { onMounted, reactive, ref } from "vue";

import { botsApi, outreachApi } from "../../api";

const loading = ref(false);
const saving = ref(false);
const autoTemplates = ref([]);
const bots = ref([]);
const form = reactive({
  cooldown_hours: 2,
  cross_account_lock_days: 30,
  strict_permanent_lock: false,
  follow_up_days: 7,
  follow_up_max: 1,
  work_start: "",
  work_end: "",
  daily_pool_cap: null,
  kill_switch: false,
  delete_session_on_account_delete: false,
  reply_mode: "human",
  auto_reply_enabled: false,
  auto_reply_max_rounds: 3,
  auto_reply_template_id: null,
  handoff_bot_enabled: false,
  handoff_bot_id: null,
});

async function load() {
  loading.value = true;
  try {
    const [{ data }, templates, botList] = await Promise.all([
      outreachApi.settings(),
      outreachApi.templates({ kind: "auto_reply" }),
      botsApi.list({ limit: 100 }),
    ]);
    autoTemplates.value = templates.data.items.filter((item) => item.enabled);
    bots.value = botList.data.items.filter((item) => item.enabled);
    form.cooldown_hours = Math.round(data.default_cooldown_seconds / 3600);
    form.cross_account_lock_days = data.cross_account_lock_days;
    form.strict_permanent_lock = data.strict_permanent_lock;
    form.follow_up_days = data.follow_up_days;
    form.follow_up_max = data.follow_up_max;
    const hours = data.working_hours?.length === 2 ? data.working_hours : ["", ""];
    form.work_start = hours[0];
    form.work_end = hours[1];
    form.daily_pool_cap = data.daily_pool_cap;
    form.kill_switch = data.kill_switch;
    form.delete_session_on_account_delete = data.delete_session_on_account_delete;
    form.reply_mode = data.reply_mode || "human";
    form.auto_reply_enabled = !!data.auto_reply_enabled;
    form.auto_reply_max_rounds = data.auto_reply_max_rounds ?? 3;
    form.auto_reply_template_id = data.auto_reply_template_id || null;
    form.handoff_bot_enabled = !!data.handoff_bot_enabled;
    form.handoff_bot_id = data.handoff_bot_id || null;
  } catch (error) {
    ElMessage.error(error.message);
  } finally {
    loading.value = false;
  }
}

async function save() {
  saving.value = true;
  try {
    const hours = form.work_start && form.work_end ? [form.work_start, form.work_end] : [];
    await outreachApi.updateSettings({
      default_cooldown_seconds: Math.round(Number(form.cooldown_hours) * 3600),
      cross_account_lock_days: Number(form.cross_account_lock_days),
      strict_permanent_lock: form.strict_permanent_lock,
      follow_up_days: Number(form.follow_up_days),
      follow_up_max: Number(form.follow_up_max),
      working_hours: hours,
      daily_pool_cap: form.daily_pool_cap ? Number(form.daily_pool_cap) : null,
      kill_switch: form.kill_switch,
      delete_session_on_account_delete: form.delete_session_on_account_delete,
      reply_mode: form.reply_mode,
      auto_reply_enabled: form.auto_reply_enabled,
      auto_reply_max_rounds: Number(form.auto_reply_max_rounds),
      auto_reply_template_id: form.auto_reply_template_id || null,
      handoff_bot_enabled: form.handoff_bot_enabled,
      handoff_bot_id: form.handoff_bot_id || null,
    });
    ElMessage.success("已保存");
    load();
  } catch (error) {
    ElMessage.error(error.message);
  } finally {
    saving.value = false;
  }
}

onMounted(load);
</script>

<template>
  <div v-loading="loading">
    <div class="toolbar">
      <h2 class="page-title">冷触达策略</h2>
      <div class="spacer" />
      <el-button size="small" type="primary" :loading="saving" @click="save">保存</el-button>
      <el-button size="small" @click="load">刷新</el-button>
    </div>

    <el-form label-width="200px" style="max-width: 640px">
      <el-form-item label="每次首触冷却（小时）">
        <el-input-number v-model="form.cooldown_hours" :min="0" :max="24" />
      </el-form-item>
      <el-form-item label="跨账号重新联系（天）">
        <el-input-number v-model="form.cross_account_lock_days" :min="0" :max="3650" />
        <span class="card-hint">填 0 表示关闭；开启就必须 ≥ 14 天</span>
      </el-form-item>
      <el-form-item label="严格模式：永久只联系一次">
        <el-switch v-model="form.strict_permanent_lock" />
      </el-form-item>
      <el-form-item label="原账号跟进（天后）">
        <el-input-number v-model="form.follow_up_days" :min="0" :max="365" />
      </el-form-item>
      <el-form-item label="最多跟进次数">
        <el-input-number v-model="form.follow_up_max" :min="0" :max="5" />
      </el-form-item>
      <el-form-item label="发送时段">
        <el-time-select v-model="form.work_start" start="00:00" step="00:30" end="23:30" placeholder="开始" />
        <span class="spacer" />
        <el-time-select v-model="form.work_end" start="00:00" step="00:30" end="23:30" placeholder="结束" />
        <span class="card-hint">留空表示不限制</span>
      </el-form-item>
      <el-form-item label="租户每日冷聊总量">
        <el-input-number v-model="form.daily_pool_cap" :min="1" :max="100000" placeholder="不限" />
        <span class="card-hint">留空表示只受账号额度约束</span>
      </el-form-item>
      <el-form-item label="回复模式">
        <el-radio-group v-model="form.reply_mode">
          <el-radio-button value="human">A · 人工接管</el-radio-button>
          <el-radio-button value="auto">B · 自动回复</el-radio-button>
        </el-radio-group>
        <span class="card-hint">自动回复只对已回复的会话生效，绝不用于首条</span>
      </el-form-item>
      <el-form-item label="开启自动回复">
        <el-switch v-model="form.auto_reply_enabled" />
      </el-form-item>
      <el-form-item label="自动回复轮次上限">
        <el-input-number v-model="form.auto_reply_max_rounds" :min="0" :max="20" />
        <span class="card-hint">超过就转人工</span>
      </el-form-item>
      <el-form-item label="自动回复话术">
        <el-select v-model="form.auto_reply_template_id" clearable placeholder="不指定则用任意启用的自动回复模板">
          <el-option
            v-for="item in autoTemplates"
            :key="item.id"
            :label="item.name"
            :value="item.id"
          />
        </el-select>
      </el-form-item>
      <el-form-item label="允许把会话转交给 Bot">
        <el-switch v-model="form.handoff_bot_enabled" />
        <span class="card-hint">只在用户已回复的会话里可发链接</span>
      </el-form-item>
      <el-form-item label="承接 Bot">
        <el-select v-model="form.handoff_bot_id" clearable placeholder="选择承接私聊的 Bot">
          <el-option
            v-for="item in bots"
            :key="item.id"
            :label="item.bot_username ? `${item.name}（@${item.bot_username}）` : item.name"
            :value="item.id"
          />
        </el-select>
      </el-form-item>
      <el-form-item label="紧急熔断（全部停止）">
        <el-switch v-model="form.kill_switch" />
      </el-form-item>
      <el-form-item label="删除账号时清理 session 文件">
        <el-switch v-model="form.delete_session_on_account_delete" />
      </el-form-item>
    </el-form>

    <el-alert
      class="panel"
      type="warning"
      :closable="false"
      show-icon
      title="这些值是保守运营参数，不是 Telegram 官方规则"
      description="官方不公开每个账号每日首次私聊的上限与冷却时间。冷触达属于实验功能：只能降低风险，不能消除风险。"
    />
  </div>
</template>

<style scoped>
.panel {
  margin-top: 12px;
}
</style>
