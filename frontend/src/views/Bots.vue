<script setup>
import { ElMessage, ElMessageBox } from "element-plus";
import { onMounted, reactive, ref } from "vue";

import { botsApi } from "../api";

const loading = ref(false);
const rows = ref([]);
const total = ref(0);
const dialogVisible = ref(false);
const editingId = ref(null);
const form = reactive({ name: "", token: "", admin_ids: "", is_default: false, note: "" });

async function load() {
  loading.value = true;
  try {
    const { data } = await botsApi.list({ limit: 100 });
    rows.value = data.items;
    total.value = data.total;
  } catch (error) {
    ElMessage.error(error.message);
  } finally {
    loading.value = false;
  }
}

function parseAdminIds(text) {
  return String(text || "")
    .split(/[\s,，、]+/)
    .filter(Boolean)
    .map((item) => Number(item))
    .filter((item) => Number.isInteger(item) && item > 0);
}

function openCreate() {
  editingId.value = null;
  Object.assign(form, { name: "", token: "", admin_ids: "", is_default: false, note: "" });
  dialogVisible.value = true;
}

function openEdit(row) {
  editingId.value = row.id;
  Object.assign(form, {
    name: row.name,
    token: "",
    admin_ids: (row.admin_ids || []).join(", "),
    is_default: row.is_default,
    note: row.note || "",
  });
  dialogVisible.value = true;
}

async function submit() {
  try {
    if (editingId.value) {
      const payload = {
        name: form.name,
        admin_ids: parseAdminIds(form.admin_ids),
        is_default: form.is_default,
        note: form.note || null,
      };
      if (form.token) {
        payload.token = form.token;
      }
      await botsApi.update(editingId.value, payload);
      ElMessage.success("机器人已更新");
    } else {
      await botsApi.create({
        name: form.name,
        token: form.token,
        admin_ids: parseAdminIds(form.admin_ids),
        is_default: form.is_default,
        note: form.note || null,
      });
      ElMessage.success("机器人已绑定（Token 已通过 Telegram 校验）");
    }
    dialogVisible.value = false;
    load();
  } catch (error) {
    ElMessage.error(error.message);
  }
}

async function setDefault(row) {
  try {
    await botsApi.update(row.id, { is_default: true });
    ElMessage.success(`已把 ${row.name} 设为默认通知机器人`);
    load();
  } catch (error) {
    ElMessage.error(error.message);
  }
}

async function toggleEnabled(row) {
  try {
    await botsApi.update(row.id, { enabled: !row.enabled });
    ElMessage.success(row.enabled ? "机器人已停用" : "机器人已启用");
    load();
  } catch (error) {
    ElMessage.error(error.message);
  }
}

async function remove(row) {
  try {
    await ElMessageBox.confirm(`确认解绑机器人 ${row.name}？`, "解绑机器人", {
      type: "warning",
      confirmButtonText: "解绑",
      cancelButtonText: "取消",
    });
    await botsApi.remove(row.id);
    ElMessage.success("机器人已解绑");
    load();
  } catch (error) {
    if (error?.message && !error.message.includes("cancel")) {
      ElMessage.error(error.message);
    }
  }
}

onMounted(load);
</script>

<template>
  <div>
    <div class="toolbar">
      <h2 class="page-title">控制 Bot</h2>
      <span class="card-hint">共 {{ total }} 个机器人</span>
      <div class="spacer" />
      <el-button size="small" type="primary" @click="openCreate">绑定机器人</el-button>
      <el-button size="small" @click="load">刷新</el-button>
    </div>

    <el-table v-loading="loading" :data="rows" size="small" border>
      <el-table-column prop="id" label="ID" width="60" />
      <el-table-column label="名称" min-width="140">
        <template #default="{ row }">
          {{ row.name }}
          <el-tag v-if="row.is_default" size="small" class="tag">默认</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="Bot 用户名" min-width="170">
        <template #default="{ row }">
          <span v-if="row.bot_username">@{{ row.bot_username }}</span>
          <span v-else class="card-hint">-</span>
        </template>
      </el-table-column>
      <el-table-column label="管理员 TG ID" min-width="180">
        <template #default="{ row }">
          <span v-if="row.admin_ids.length">{{ row.admin_ids.join("、") }}</span>
          <span v-else class="card-hint">未配置（无人可下指令）</span>
        </template>
      </el-table-column>
      <el-table-column label="状态" width="100">
        <template #default="{ row }">
          <el-tag :type="row.enabled ? 'success' : 'info'" size="small">
            {{ row.enabled ? "启用" : "停用" }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="note" label="备注" min-width="140" />
      <el-table-column label="操作" width="280" fixed="right">
        <template #default="{ row }">
          <el-button size="small" link type="primary" :disabled="row.is_default" @click="setDefault(row)">
            设为默认
          </el-button>
          <el-button size="small" link @click="toggleEnabled(row)">
            {{ row.enabled ? "停用" : "启用" }}
          </el-button>
          <el-button size="small" link @click="openEdit(row)">换 Token</el-button>
          <el-button size="small" link type="danger" @click="remove(row)">解绑</el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-alert
      class="panel"
      type="info"
      :closable="false"
      show-icon
      title="机器人只负责接收指令与发送通知；实际搬运与会员监听由执行账号完成"
      description="保存 Token 时会调用 Telegram 校验并回显 Bot 用户名；只有「管理员 TG ID」列表中的用户可以下管理指令。"
    />

    <el-dialog
      v-model="dialogVisible"
      :title="editingId ? '换 Token / 修改机器人' : '绑定机器人'"
      width="480px"
    >
      <el-form label-position="top">
        <el-form-item label="名称">
          <el-input v-model="form.name" placeholder="例如：机器人A" />
        </el-form-item>
        <el-form-item :label="editingId ? 'Bot Token（留空表示不修改）' : 'Bot Token'">
          <el-input v-model="form.token" show-password placeholder="123456:ABC-DEF..." />
        </el-form-item>
        <el-form-item label="管理员 TG 用户 ID（逗号分隔）">
          <el-input v-model="form.admin_ids" placeholder="123456789, 987654321" />
        </el-form-item>
        <el-form-item label="备注（可选）">
          <el-input v-model="form.note" />
        </el-form-item>
        <el-form-item>
          <el-checkbox v-model="form.is_default">设为默认通知机器人</el-checkbox>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" @click="submit">保存</el-button>
      </template>
    </el-dialog>
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
