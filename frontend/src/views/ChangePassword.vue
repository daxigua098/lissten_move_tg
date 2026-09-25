<script setup>
import { ElMessage } from "element-plus";
import { reactive, ref } from "vue";
import { useRouter } from "vue-router";

import { authApi } from "../api";
import { auth } from "../stores/auth";

const router = useRouter();
const loading = ref(false);
const form = reactive({ current: "", next: "", confirm: "" });

async function submit() {
  if (form.next !== form.confirm) {
    ElMessage.warning("两次输入的新密码不一致");
    return;
  }
  loading.value = true;
  try {
    const { data } = await authApi.changePassword(form.current, form.next);
    auth.clear();
    ElMessage.success(`密码已修改，其他设备已被下线（${data.revoked_other_sessions} 个会话）`);
    router.push({ name: "login" });
  } catch (error) {
    ElMessage.error(error.message);
  } finally {
    loading.value = false;
  }
}
</script>

<template>
  <div class="pw-page">
    <el-card class="pw-card">
      <h2 class="page-title">修改密码</h2>
      <p class="card-hint">新密码至少 8 位，且需要同时包含字母与数字。修改后其他设备会被强制下线。</p>

      <el-form label-position="top" class="pw-form" @submit.prevent="submit">
        <el-form-item label="当前密码">
          <el-input v-model="form.current" type="password" show-password />
        </el-form-item>
        <el-form-item label="新密码">
          <el-input v-model="form.next" type="password" show-password />
        </el-form-item>
        <el-form-item label="确认新密码">
          <el-input v-model="form.confirm" type="password" show-password @keyup.enter="submit" />
        </el-form-item>
        <el-button type="primary" :loading="loading" @click="submit">保存新密码</el-button>
        <el-button @click="router.push({ name: 'dashboard' })">返回</el-button>
      </el-form>
    </el-card>
  </div>
</template>

<style scoped>
.pw-page {
  min-height: 100%;
  display: grid;
  place-items: center;
  padding: 32px 16px;
}

.pw-card {
  width: 100%;
  max-width: 460px;
}

.pw-form {
  margin-top: 16px;
}
</style>
