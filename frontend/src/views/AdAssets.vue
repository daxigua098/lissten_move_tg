<script setup>
import { ElMessage, ElMessageBox } from "element-plus";
import { computed, onMounted, reactive, ref } from "vue";

import { adAssetsApi, uploadsApi } from "../api";

const loading = ref(false);
const assets = ref([]);
const total = ref(0);
const drawerVisible = ref(false);
const editingId = ref(null);
const references = ref([]);
const uploading = ref(false);
const form = reactive({
  name: "",
  text: "",
  image_path: "",
  link_url: "",
  link_text: "",
  enabled: true,
});

const previewText = computed(() => (form.text || "").replace(/\{源名\}/g, "短剧素材频道"));

// 把库存路径（assets/uploads/xxx.png）转换成可访问 URL
const imageUrl = computed(() => {
  const path = (form.image_path || "").trim();
  if (!path) return "";
  if (path.startsWith("http://") || path.startsWith("https://") || path.startsWith("/")) {
    return path;
  }
  return `/uploads/${path.split("/").pop()}`;
});

async function uploadImage(options) {
  uploading.value = true;
  try {
    const formData = new FormData();
    formData.append("file", options.file);
    const { data } = await uploadsApi.image(formData);
    form.image_path = data.path;
    ElMessage.success(`图片已上传（${data.width}×${data.height}）`);
  } catch (error) {
    ElMessage.error(error.message);
  } finally {
    uploading.value = false;
  }
}

function clearImage() {
  form.image_path = "";
}

async function load() {
  loading.value = true;
  try {
    const { data } = await adAssetsApi.list({ limit: 200 });
    assets.value = data.items;
    total.value = data.total;
  } catch (error) {
    ElMessage.error(error.message);
  } finally {
    loading.value = false;
  }
}

function openCreate() {
  editingId.value = null;
  references.value = [];
  Object.assign(form, {
    name: "",
    text: "",
    image_path: "",
    link_url: "",
    link_text: "",
    enabled: true,
  });
  drawerVisible.value = true;
}

async function openEdit(row) {
  editingId.value = row.id;
  try {
    const { data } = await adAssetsApi.detail(row.id);
    references.value = data.references;
    Object.assign(form, {
      name: data.name,
      text: data.text,
      image_path: data.image_path || "",
      link_url: data.link_url || "",
      link_text: data.link_text || "",
      enabled: data.enabled,
    });
    drawerVisible.value = true;
  } catch (error) {
    ElMessage.error(error.message);
  }
}

function insertVariable(token) {
  form.text = `${form.text || ""}${token}`;
}

async function save() {
  try {
    const payload = {
      name: form.name,
      text: form.text,
      image_path: form.image_path || null,
      link_url: form.link_url || null,
      link_text: form.link_text || null,
      enabled: form.enabled,
    };
    if (editingId.value) {
      const { data } = await adAssetsApi.update(editingId.value, payload);
      ElMessage.success(`已保存，${data.reference_count} 条引用它的线路立即生效`);
    } else {
      await adAssetsApi.create(payload);
      ElMessage.success("素材已创建");
    }
    drawerVisible.value = false;
    load();
  } catch (error) {
    ElMessage.error(error.message);
  }
}

async function toggleEnabled(row) {
  try {
    await adAssetsApi.update(row.id, { enabled: row.enabled });
  } catch (error) {
    ElMessage.error(error.message);
    load();
  }
}

async function remove(row) {
  try {
    if (row.reference_count > 0) {
      await ElMessageBox.confirm(
        `该素材正被 ${row.reference_count} 条线路引用，删除后这些线路会变成"不挂广告"。建议改成停用。`,
        "删除素材",
        { type: "warning", confirmButtonText: "仍然删除", cancelButtonText: "取消" },
      );
      await adAssetsApi.remove(row.id, true);
    } else {
      await ElMessageBox.confirm(`确认删除素材「${row.name}」？`, "删除素材", {
        type: "warning",
        confirmButtonText: "删除",
        cancelButtonText: "取消",
      });
      await adAssetsApi.remove(row.id, false);
    }
    ElMessage.success("素材已删除");
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
  <div v-loading="loading">
    <div class="toolbar">
      <h2 class="page-title">广告素材库</h2>
      <span class="card-hint">共 {{ total }} 个素材 · 线路只引用素材，改素材即改所有引用它的线路</span>
      <div class="spacer" />
      <el-button size="small" type="primary" @click="openCreate">新建素材</el-button>
      <el-button size="small" @click="load">刷新</el-button>
    </div>

    <el-card shadow="never">
      <el-table :data="assets" size="small" border>
        <el-table-column prop="id" label="ID" width="60" />
        <el-table-column prop="name" label="素材名" width="150" />
        <el-table-column label="文案预览" min-width="240">
          <template #default="{ row }">
            <span v-if="row.text" class="preview">{{ row.text }}</span>
            <span v-else class="card-hint">（无文案，只发图片）</span>
          </template>
        </el-table-column>
        <el-table-column label="图片" width="180">
          <template #default="{ row }">
            <span v-if="row.image_path">有</span>
            <span v-else class="card-hint">—</span>
          </template>
        </el-table-column>
        <el-table-column label="链接按钮" width="180">
          <template #default="{ row }">
            <span v-if="row.link_url">{{ row.link_text }}</span>
            <span v-else class="card-hint">—</span>
          </template>
        </el-table-column>
        <el-table-column label="被引用" width="110">
          <template #default="{ row }">
            <el-tag size="small" :type="row.reference_count ? 'success' : 'info'">
              {{ row.reference_count }} 条线路
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="90">
          <template #default="{ row }">
            <el-switch v-model="row.enabled" size="small" @change="() => toggleEnabled(row)" />
          </template>
        </el-table-column>
        <el-table-column label="操作" width="140" fixed="right">
          <template #default="{ row }">
            <el-button size="small" link type="primary" @click="openEdit(row)">编辑</el-button>
            <el-button size="small" link type="danger" @click="remove(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-alert
      class="panel-gap"
      type="info"
      :closable="false"
      show-icon
      title="广告只作用于 A 线搬运，B 线的线索卡片不带广告"
      description="文案、图片、链接按钮至少要填一项；被线路引用的素材删除前会提示影响范围。"
    />

    <el-drawer v-model="drawerVisible" :title="editingId ? '编辑素材' : '新建素材'" size="560px">
      <el-form label-position="top">
        <el-form-item label="素材名（只用于后台识别）">
          <el-input v-model="form.name" placeholder="例如：渠道A文案" />
        </el-form-item>
        <el-form-item label="文案">
          <div class="vars">
            <span class="card-hint">插入变量：</span>
            <el-button size="small" @click="insertVariable('{源名}')">源名</el-button>
            <el-button size="small" @click="insertVariable('{时间}')">时间</el-button>
            <el-button size="small" @click="insertVariable('{原发言人}')">原发言人</el-button>
          </div>
          <el-input v-model="form.text" type="textarea" :rows="4" placeholder="{源名} · 每日更新" />
        </el-form-item>
        <el-form-item label="附加图片（可选）">
          <div class="upload-row">
            <el-upload
              :show-file-list="false"
              :http-request="uploadImage"
              accept="image/png,image/jpeg,image/gif,image/webp"
            >
              <el-button size="small" :loading="uploading">
                {{ form.image_path ? "重新上传" : "选择本地图片上传" }}
              </el-button>
            </el-upload>
            <span class="card-hint">支持 JPG / PNG / GIF / WEBP，最大 5 MB</span>
          </div>
          <div v-if="form.image_path" class="image-preview">
            <img :src="imageUrl" alt="附加图片预览" />
            <div class="image-meta">
              <span class="path">{{ form.image_path }}</span>
              <el-button size="small" link type="danger" @click="clearImage">移除</el-button>
            </div>
          </div>
          <el-input
            v-model="form.image_path"
            size="small"
            class="manual-path"
            placeholder="也可以手动填写已有图片路径"
          />
        </el-form-item>
        <el-form-item label="链接按钮文字（可选）">
          <el-input v-model="form.link_text" placeholder="立即查看" />
        </el-form-item>
        <el-form-item label="链接地址（可选，可带渠道参数）">
          <el-input v-model="form.link_url" placeholder="https://t.me/xxx?start=a" />
        </el-form-item>
        <el-form-item>
          <el-checkbox v-model="form.enabled">启用该素材</el-checkbox>
        </el-form-item>
        <el-divider content-position="left">发送效果预览</el-divider>
        <div class="preview-box">
          <div
            class="preview-media"
            :class="{ 'has-image': Boolean(imageUrl) }"
            :style="imageUrl ? { backgroundImage: `url(${imageUrl})` } : {}"
          >
            <span v-if="!imageUrl">附加图片</span>
          </div>
          <div class="preview-text">{{ previewText || "（无文案）" }}</div>
          <div v-if="form.link_text" class="preview-button">{{ form.link_text }}</div>
        </div>
        <template v-if="editingId">
          <el-divider content-position="left">引用情况</el-divider>
          <div v-if="references.length" class="refs">
            <div v-for="item in references" :key="item.route_id" class="ref-row">
              {{ item.name }}
            </div>
          </div>
          <p v-else class="card-hint">还没有线路引用这个素材。保存后引用它的线路会立即使用新内容。</p>
        </template>
      </el-form>
      <template #footer>
        <el-button @click="drawerVisible = false">取消</el-button>
        <el-button type="primary" @click="save">保存</el-button>
      </template>
    </el-drawer>
  </div>
</template>

<style scoped>
.panel-gap {
  margin-top: 12px;
}

.preview {
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.vars {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
  margin-bottom: 6px;
}

.preview-box {
  border: 1px solid var(--tg-border);
  border-radius: 10px;
  padding: 10px;
  background: #fbfcfe;
}

.preview-media {
  height: 90px;
  border-radius: 8px;
  background: #e2e8f0;
  display: grid;
  place-items: center;
  color: #94a3b8;
  font-size: 13px;
  margin-bottom: 8px;
}

.preview-media.has-image {
  background-size: cover;
  background-position: center;
}

.upload-row {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-bottom: 8px;
}

.image-preview {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px;
  border: 1px solid var(--tg-border);
  border-radius: 8px;
  background: #fbfcfe;
  margin-bottom: 8px;
}

.image-preview img {
  width: 64px;
  height: 64px;
  object-fit: cover;
  border-radius: 6px;
  border: 1px solid var(--tg-border);
  background: #fff;
}

.image-meta {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}

.image-meta .path {
  font-size: 12px;
  color: var(--tg-muted);
  word-break: break-all;
}

.manual-path {
  margin-top: 6px;
}

.preview-text {
  white-space: pre-wrap;
  font-size: 13px;
}

.preview-button {
  margin-top: 8px;
  text-align: center;
  padding: 6px;
  border-radius: 8px;
  border: 1px solid var(--tg-border);
  background: #f1f5f9;
  color: var(--tg-accent);
  font-size: 13px;
}

.refs {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.ref-row {
  padding: 6px 8px;
  border: 1px solid var(--tg-border);
  border-radius: 8px;
  background: #fbfcfe;
  font-size: 13px;
}
</style>
