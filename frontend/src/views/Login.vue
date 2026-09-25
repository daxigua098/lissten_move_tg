<script setup>
import { ElMessage } from "element-plus";
import { reactive, ref } from "vue";
import { useRouter } from "vue-router";

import { authApi } from "../api";
import { auth } from "../stores/auth";

const router = useRouter();
const loading = ref(false);
const form = reactive({ username: "admin", password: "" });

async function submit() {
  if (!form.username || !form.password) {
    ElMessage.warning("请输入账号与密码");
    return;
  }
  loading.value = true;
  try {
    const { data } = await authApi.login(form.username, form.password);
    auth.set(data);
    if (data.must_change_password && !data.is_builtin) {
      ElMessage.warning("首次登录需要修改密码");
      router.push({ name: "change-password" });
    } else {
      ElMessage.success(`欢迎回来，${data.username}`);
      router.push({ name: "dashboard" });
    }
  } catch (error) {
    ElMessage.error(error.message);
  } finally {
    loading.value = false;
  }
}
</script>

<template>
  <div class="login-page">
    <el-card class="login-card" shadow="always">
      <div class="login-brand">
        <span class="login-logo">TG</span>
        <div>
          <div class="login-title">TG 线索运营系统</div>
          <div class="login-sub">内容搬运 · 会员线索生产</div>
        </div>
      </div>

      <el-form label-position="top" @submit.prevent="submit">
        <el-form-item label="账号">
          <el-input v-model="form.username" placeholder="请输入账号" autocomplete="username" />
        </el-form-item>
        <el-form-item label="密码">
          <el-input
            v-model="form.password"
            type="password"
            show-password
            placeholder="请输入密码"
            autocomplete="current-password"
            @keyup.enter="submit"
          />
        </el-form-item>
        <el-button type="primary" class="login-submit" :loading="loading" @click="submit">
          登 录
        </el-button>
      </el-form>

      <el-alert
        class="login-alert"
        type="info"
        :closable="false"
        show-icon
        title="默认管理员 admin / admin123，密码在服务器 .env 中维护"
      />
      <p class="card-hint login-foot">连续 5 次密码错误将锁定 15 分钟，所有登录都会记入审计。</p>
    </el-card>
  </div>
</template>

<style scoped>
.login-page {
  min-height: 100%;
  display: grid;
  place-items: center;
  padding: 32px 16px;
}

.login-card {
  width: 100%;
  max-width: 400px;
}

.login-brand {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 18px;
}

.login-logo {
  width: 36px;
  height: 36px;
  border-radius: 9px;
  background: var(--tg-accent);
  color: #fff;
  display: grid;
  place-items: center;
  font-size: 13px;
}

.login-title {
  font-size: 16px;
  font-weight: 500;
}

.login-sub {
  font-size: 12px;
  color: var(--tg-muted);
}

.login-submit {
  width: 100%;
}

.login-alert {
  margin-top: 16px;
}

.login-foot {
  margin-top: 10px;
}
</style>
