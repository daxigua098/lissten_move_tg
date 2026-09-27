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
      </div>

      <div v-else-if="stage === 'code_sent'" class="login-step">
        <el-input
          v-model="form.code"
          size="large"
          placeholder="输入 Telegram 收到的验证码"
          @keyup.enter="submitCode"
        />
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
