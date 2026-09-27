<script setup>
import { ElMessage, ElMessageBox } from "element-plus";
import { onMounted, reactive, ref } from "vue";

import { outreachApi } from "../../api";

const loading = ref(false);
const rows = ref([]);
const total = ref(0);
const kind = ref("");
const dialogVisible = ref(false);
const editing = ref(null);
const form = reactive({ name: "", kind: "first_contact", text: "" });

const KINDS = [
  { value: "first_contact", label: "首条招呼" },
  { value: "follow_up", label: "跟进" },
  { value: "auto_reply", label: "自动回复" },
];

async function load() {
  loading.value = true;
  try {
    const params = {};
    if (kind.value) params.kind = kind.value;
    const { data } = await outreachApi.templates(params);
    rows.value = data.items;
    total.value = data.total;
  } catch (error) {
    ElMessage.error(error.message);
  } finally {
    loading.value = false;
  }
}

function openCreate() {
  editing.value = null;
  Object.assign(form, { name: "", kind: "first_contact", text: "" });
  dialogVisible.value = true;
}

function openEdit(row) {
  editing.value = row;
  Object.assign(form, { name: row.name, kind: row.kind, text: row.text });
  dialogVisible.value = true;
}

async function save() {
  try {
    if (editing.value) {
      await outreachApi.updateTemplate(editing.value.id, {
        name: form.name,
        kind: form.kind,
        text: form.text,
      });
    } else {
      await outreachApi.createTemplate({
        name: form.name,
        kind: form.kind,
        text: form.text,
      });
    }
    dialogVisible.value = false;
    ElMessage.success("已保存");
    load();
  } catch (error) {
    ElMessage.error(error.message);
  }
}

async function adopt(row) {
  try {
    await outreachApi.adoptTemplate(row.id);
    ElMessage.success("已选用为我的模板");
    load();
  } catch (error) {
    ElMessage.error(error.message);
  }
}

async function remove(row) {
  try {
    await ElMessageBox.confirm(`确认删除模板 ${row.name}？`, "删除模板", {
      type: "warning",
      confirmButtonText: "删除",
      cancelButtonText: "取消",
    });
    await outreachApi.removeTemplate(row.id);
    ElMessage.success("已删除");
    load();
  } catch (error) {
    if (error?.message && !error.message.includes("cancel")) {
      ElMessage.error(error.message);
    }
  }
}

function kindLabel(value) {
  return KINDS.find((item) => item.value === value)?.label || value;
}

onMounted(load);
</script>

<template>
  <div>
    <div class="toolbar">
      <h2 class="page-title">话术模板</h2>
      <span class="card-hint">共 {{ total }} 套</span>
      <div class="spacer" />
      <el-select v-model="kind" size="small" style="width: 140px" @change="load">
        <el-option label="全部类型" value="" />
        <el-option v-for="item in KINDS" :key="item.value" :label="item.label" :value="item.value" />
      </el-select>
      <el-button size="small" type="primary" @click="openCreate">新建模板</el-button>
      <el-button size="small" @click="load">刷新</el-button>
    </div>

    <el-table v-loading="loading" :data="rows" size="small" border>
      <el-table-column prop="id" label="ID" width="60" />
      <el-table-column prop="name" label="名称" min-width="140" />
      <el-table-column label="范围" width="100">
        <template #default="{ row }">
          <el-tag size="small" :type="row.scope === 'platform' ? 'info' : 'success'">
            {{ row.scope === "platform" ? "平台共享" : "我的" }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="类型" width="100">
        <template #default="{ row }">{{ kindLabel(row.kind) }}</template>
      </el-table-column>
      <el-table-column label="内容" min-width="260">
        <template #default="{ row }">{{ row.text }}</template>
      </el-table-column>
      <el-table-column prop="version" label="版本" width="70" />
      <el-table-column label="启用" width="80">
        <template #default="{ row }">{{ row.enabled ? "是" : "否" }}</template>
      </el-table-column>
      <el-table-column label="操作" width="200" fixed="right">
        <template #default="{ row }">
          <el-button
            v-if="row.scope === 'platform'"
            size="small"
            link
            type="primary"
            @click="adopt(row)"
          >
            选用
          </el-button>
          <template v-else>
            <el-button size="small" link type="primary" @click="openEdit(row)">编辑</el-button>
            <el-button size="small" link type="danger" @click="remove(row)">删除</el-button>
          </template>
        </template>
      </el-table-column>
    </el-table>

    <el-alert
      class="panel"
      type="warning"
      :closable="false"
      show-icon
      title="首条招呼不要带链接或广告"
      description="第一条消息只用来确认对方是否愿意沟通：说明身份与来意，并明确如果不需要就不再打扰。平台模板只读，选用后会复制成你自己的模板。"
    />

    <el-dialog v-model="dialogVisible" :title="editing ? '编辑模板' : '新建模板'" width="520px">
      <el-form label-position="top">
        <el-form-item label="名称">
          <el-input v-model="form.name" placeholder="例如：首条确认" />
        </el-form-item>
        <el-form-item label="类型">
          <el-select v-model="form.kind" style="width: 100%">
            <el-option v-for="item in KINDS" :key="item.value" :label="item.label" :value="item.value" />
          </el-select>
        </el-form-item>
        <el-form-item label="话术内容">
          <el-input v-model="form.text" type="textarea" :rows="5" placeholder="你好，我是……，想确认一下你是否愿意了解。" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" @click="save">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.panel {
  margin-top: 12px;
}
</style>
