<script setup>
import { ElMessage } from "element-plus";
import { onMounted, ref } from "vue";

import { outreachApi } from "../../api";

const loading = ref(false);
const rows = ref([]);
const total = ref(0);
const state = ref("");

const STATES = [
  { value: "", label: "全部" },
  { value: "QUEUED", label: "排队中" },
  { value: "CONTACTED", label: "已联系" },
  { value: "REPLIED", label: "已回复" },
  { value: "REFUSED", label: "已拒绝" },
  { value: "FROZEN", label: "已冻结" },
];

async function load() {
  loading.value = true;
  try {
    const params = { limit: 100 };
    if (state.value) params.state = state.value;
    const { data } = await outreachApi.contacts(params);
    rows.value = data.items;
    total.value = data.total;
  } catch (error) {
    ElMessage.error(error.message);
  } finally {
    loading.value = false;
  }
}

async function takeover(row) {
  try {
    await outreachApi.takeover(row.id);
    ElMessage.success(`已接管 ${row.display_name || row.tg_user_id}，不会再自动动作`);
    load();
  } catch (error) {
    ElMessage.error(error.message);
  }
}

async function resumeAuto(row) {
  try {
    await outreachApi.resumeAuto(row.id);
    ElMessage.success("已交回自动回复");
    load();
  } catch (error) {
    ElMessage.error(error.message);
  }
}

async function suppress(row) {
  try {
    await outreachApi.suppress(row.id);
    ElMessage.success("已加入永久免打扰");
    load();
  } catch (error) {
    ElMessage.error(error.message);
  }
}

async function unsuppress(row) {
  try {
    await outreachApi.unsuppress(row.id);
    ElMessage.success("已解除免打扰");
    load();
  } catch (error) {
    ElMessage.error(error.message);
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
      <h2 class="page-title">联系人</h2>
      <span class="card-hint">共 {{ total }} 人</span>
      <div class="spacer" />
      <el-select v-model="state" size="small" style="width: 140px" @change="load">
        <el-option v-for="item in STATES" :key="item.value" :label="item.label" :value="item.value" />
      </el-select>
      <el-button size="small" @click="load">刷新</el-button>
    </div>

    <el-table v-loading="loading" :data="rows" size="small" border>
      <el-table-column prop="id" label="ID" width="60" />
      <el-table-column label="用户" min-width="170">
        <template #default="{ row }">
          {{ row.display_name || row.username || row.tg_user_id }}
          <span class="card-hint">ID {{ row.tg_user_id }}</span>
        </template>
      </el-table-column>
      <el-table-column label="状态" width="110">
        <template #default="{ row }">
          <el-tag size="small">{{ row.state_label }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="contact_count" label="联系次数" width="90" />
      <el-table-column label="首次联系" width="170">
        <template #default="{ row }">{{ fmt(row.first_contact_at) }}</template>
      </el-table-column>
      <el-table-column label="跨账号锁至" width="170">
        <template #default="{ row }">{{ fmt(row.global_lock_until) }}</template>
      </el-table-column>
      <el-table-column label="免打扰" width="90">
        <template #default="{ row }">
          <el-tag v-if="row.do_not_contact" type="danger" size="small">是</el-tag>
          <span v-else class="card-hint">否</span>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="200" fixed="right">
        <template #default="{ row }">
          <el-button
            size="small"
            link
            type="primary"
            :disabled="row.contact_state !== 'REPLIED'"
            @click="takeover(row)"
          >
            接管
          </el-button>
          <el-button
            v-if="row.reply_state === 'HUMAN'"
            size="small"
            link
            @click="resumeAuto(row)"
          >
            恢复自动
          </el-button>
          <el-button v-if="!row.do_not_contact" size="small" link type="danger" @click="suppress(row)">
            拉黑
          </el-button>
          <el-button v-else size="small" link @click="unsuppress(row)">解除</el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-alert
      class="panel"
      type="info"
      :closable="false"
      show-icon
      title="一个用户只有一份联系档案"
      description="首触全账号池只发一次；用户回复后会话永久归属原账号，不会因为冷却结束而换号；跨账号重新联系默认锁定 30 天，严格模式下永久锁定。"
    />
  </div>
</template>

<style scoped>
.panel {
  margin-top: 12px;
}
</style>
