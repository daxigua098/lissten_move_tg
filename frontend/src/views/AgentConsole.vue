<script setup>
import { auth } from "../stores/auth";
</script>

<template>
  <div class="agent-console">
    <el-card shadow="never">
      <template #header>代理工作台</template>
      <el-alert
        type="info"
        :closable="false"
        show-icon
        title="代理账号只负责开号"
        description="代理账号不带任何业务功能：开会员号、给下级代理划拨额度都在这里进行。要自己用搬运 / 监听 / 资源发现，请给自己开一个会员号。"
      />

      <el-descriptions class="agent-info" :column="1" border>
        <el-descriptions-item label="登录账号">{{ auth.username }}</el-descriptions-item>
        <el-descriptions-item label="账号类型">代理</el-descriptions-item>
      </el-descriptions>

      <el-empty description="开号面板 / 我的下级 / 额度台账将在 P3 交付" />
    </el-card>
  </div>
</template>

<style scoped>
.agent-console {
  padding: 4px;
}

.agent-info {
  margin: 16px 0 4px;
}
</style>