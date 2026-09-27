<script setup>
import { ElMessage } from "element-plus";
import { reactive, ref, watch } from "vue";

import { accountsApi } from "../api";

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  account: { type: Object, default: null },
  title: { type: String, default: "登录执行账号" },
});
const emit = defineEmits(["update:modelValue", "logged-in"]);

const stage = ref("idle");
const busy = ref(false);
const result = ref(null);
const form = reactive({ code: "", password: "", force_sms: false });
// 自动取码：接码平台 / 2925 邮箱
const mail = reactive({ user: "", password: "" });
const fetching = ref("");
const autoRunning = ref(false);
const autoMessage = ref("");
let autoTimer = null;

function stopAuto() {
  if (autoTimer) {
    clearInterval(autoTimer);
    autoTimer = null;
  }
  autoRunning.value = false;
}

async function pollAuto() {
  try {
    const { data } = await accountsApi.autoLoginStatus();
    const item = data.items.find((row) => row.account_id === props.account.id);
    if (!item) return;
    autoMessage.value = `${item.stage_label}：${item.message}`;
    if (item.status === "pending") return;
    stopAuto();
    if (item.status === "success") {
      stage.value = "active";
      result.value = {};
      ElMessage.success("已通过接码平台登录成功");
      emit("logged-in");
    } else {
      ElMessage.error(item.message || "自动登录失败");
    }
  } catch (error) {
    stopAuto();
    ElMessage.error(error.message);
  }
}

async function startAutoLogin() {
  autoRunning.value = true;
  autoMessage.value = "正在提交自动登录…";
  try {
    await accountsApi.autoLogin([props.account.id]);
    ElMessage.success("已开始自动登录，正在等接码平台返回验证码");
    stopAuto();
    autoRunning.value = true;
    autoTimer = setInterval(pollAuto, 3000);
    pollAuto();
  } catch (error) {
    stopAuto();
    ElMessage.error(error.message);
  }
}

async function openPlatform() {
  try {
    const { data } = await accountsApi.codeUrl(props.account.id);
    window.open(data.code_url, "_blank", "noopener");
  } catch (error) {
    ElMessage.error(error.message);
  }
}

function cooldownMinutes() {
  const left = Number(props.account?.code_cooldown_remaining || 0);
  return left > 0 ? Math.max(1, Math.ceil(left / 60)) : 0;
}

async function fetchCode(source) {
  if (source === "mail2925" && (!mail.user.trim() || !mail.password)) {
    ElMessage.warning("请先填写 2925 主邮箱和密码");
    return;
  }
  fetching.value = source;
  try {
    const payload = { source, timeout_seconds: 120 };
    if (source === "mail2925") {
      payload.mail_user = mail.user.trim();
      payload.mail_pass = mail.password;
    }
    const { data } = await accountsApi.fetchLoginCode(props.account.id, payload);
    form.code = data.code;
    if (data.password) form.password = data.password;
    ElMessage.success(
      source === "logincode"
        ? "已从接码平台取到验证码，确认或修改后再提交"
        : `已从 ${data.alias} 取到验证码，确认或修改后再提交`,
    );
  } catch (error) {
    ElMessage.error(error.message);
  } finally {
    fetching.value = "";
  }
}

watch(
  () => props.modelValue,
  (open) => {
    if (!open) return;
    stage.value = "idle";
    result.value = null;
    Object.assign(form, { code: "", password: "", force_sms: false });
  },
);

async function close(force = false) {
  stopAuto();
  if (!force && props.account && ["code_sent", "password_required"].includes(stage.value)) {
    try {
      await accountsApi.loginCancel(props.account.id);
    } catch {
      // 取消失败不阻塞关闭
    }
  }
  emit("update:modelValue", false);
}

async function sendCode() {
  busy.value = true;
  try {
    const { data } = await accountsApi.loginStart(props.account.id, form.force_sms);
    stage.value = "code_sent";
    ElMessage.success(`验证码已发送到 ${data.phone_masked} 对应的 Telegram`);
  } catch (error) {
    ElMessage.error(error.message);
  } finally {
    busy.value = false;
  }
}

async function submitCode() {
  if (!form.code.trim()) {
    ElMessage.warning("请输入验证码");
    return;
  }
  busy.value = true;
  try {
    const { data } = await accountsApi.loginVerify(props.account.id, form.code.trim());
    if (data.status === "password_required") {
      stage.value = "password_required";
      ElMessage.info("该账号开启了两步验证，请继续输入密码");
    } else {
      stage.value = "active";
      result.value = data;
      ElMessage.success("登录成功");
      emit("logged-in");
    }
  } catch (error) {
    ElMessage.error(error.message);
  } finally {
    busy.value = false;
  }
}

async function submitPassword() {
  if (!form.password) {
    ElMessage.warning("请输入两步验证密码");
    return;
  }
  busy.value = true;
  try {
    const { data } = await accountsApi.loginPassword(props.account.id, form.password);
    stage.value = "active";
    result.value = data;
    ElMessage.success("登录成功");
    emit("logged-in");
  } catch (error) {
    ElMessage.error(error.message);
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <el-dialog
    :model-value="modelValue"
    :title="title"
    width="460px"
    :close-on-click-modal="false"
    @update:model-value="(value) => { if (!value) close(); }"
  >
    <div v-if="account" class="login-body">
      <el-alert
        type="info"
        :closable="false"
        show-icon
        :title="`账号：${account.name}（${account.phone_masked}）`"
        description="验证码会发到该手机号对应的 Telegram App 里，不是短信。"
      />

      <div v-if="stage === 'idle'" class="login-step">
        <p class="card-hint">点击下方按钮让 Telegram 发送登录验证码。</p>
        <el-checkbox v-model="form.force_sms">改用短信接收验证码（收不到 App 消息时勾选）</el-checkbox>
        <el-button type="primary" :loading="busy" @click="sendCode">发送验证码</el-button>
        <el-button type="success" :loading="autoRunning" @click="startAutoLogin">
          通过接码平台登录
        </el-button>
        <el-button v-if="account.has_code_url" size="small" @click="openPlatform">
          打开接码平台
        </el-button>
        <p v-if="autoMessage" class="card-hint">{{ autoMessage }}</p>
        <p v-if="account.code_host" class="card-hint">已绑定接码地址：{{ account.code_host }}</p>
        <p v-if="account.has_code || account.has_2fa" class="card-hint">
          已保存：
          {{ account.has_code ? "登录验证码" : "" }}{{ account.has_code && account.has_2fa ? " + " : "" }}{{ account.has_2fa ? "二级密码" : "" }}
        </p>
        <p v-if="cooldownMinutes()" class="card-hint">
          接码平台冷却中，约 {{ cooldownMinutes() }} 分钟后可重试
        </p>
      </div>

      <div v-else-if="stage === 'code_sent'" class="login-step">
        <el-input
          v-model="form.code"
          size="large"
          placeholder="输入 Telegram 收到的验证码"
          @keyup.enter="submitCode"
        />
        <div class="code-sources">
          <el-button size="small" :loading="fetching === 'logincode'" @click="fetchCode('logincode')">
            从接码平台取码
          </el-button>
          <el-button size="small" :loading="fetching === 'mail2925'" @click="fetchCode('mail2925')">
            从 2925 邮箱取码
          </el-button>
        </div>
        <div class="login-actions">
          <el-input v-model="mail.user" size="small" placeholder="2925 主邮箱（user@2925.com）" />
          <el-input
            v-model="mail.password"
            size="small"
            type="password"
            show-password
            placeholder="2925 密码"
          />
        </div>
        <p class="card-hint">
          取到的验证码会填进上面的输入框；账号改绑邮箱后也可以直接手动输入验证码。
        </p>
        <div class="login-actions">
          <el-button link type="primary" :loading="busy" @click="sendCode">
            没收到？重新发送
          </el-button>
          <div class="spacer" />
          <el-button @click="close()">取消</el-button>
          <el-button type="primary" :loading="busy" @click="submitCode">提交</el-button>
        </div>
      </div>

      <div v-else-if="stage === 'password_required'" class="login-step">
        <el-alert
          type="warning"
          :closable="false"
          show-icon
          title="该账号开启了两步验证"
          description="请输入你在 Telegram 设置的两步验证密码（不是登录验证码）。"
        />
        <el-input
          v-model="form.password"
          type="password"
          show-password
          size="large"
          placeholder="两步验证密码"
          @keyup.enter="submitPassword"
        />
        <div class="login-actions">
          <div class="spacer" />
          <el-button @click="close()">取消</el-button>
          <el-button type="primary" :loading="busy" @click="submitPassword">提交</el-button>
        </div>
      </div>

      <div v-else class="login-step">
        <el-result icon="success" title="登录成功">
          <template #sub-title>
            <span v-if="result?.username">@{{ result.username }} · ID {{ result.tg_user_id }}</span>
            <span v-else>session 已生成</span>
          </template>
        </el-result>
        <el-button type="primary" @click="close(true)">完成</el-button>
      </div>
    </div>
  </el-dialog>
</template>

<style scoped>
.code-sources {
  display: flex;
  gap: 8px;
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
