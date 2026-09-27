<script setup>
import { ElMessage, ElMessageBox } from "element-plus";
import { computed, onMounted, reactive, ref } from "vue";

import { routesApi, sourcesApi, targetsApi } from "../api";
import RouteEditor from "../components/RouteEditor.vue";
import { auth } from "../stores/auth";
import { collectRoleMismatches, targetRoleFullLabel, targetRoleShortLabel, targetRoleTagType } from "../targetRoles";

const loading = ref(false);
const routes = ref([]);
const sources = ref([]);
const targets = ref([]);
const editorVisible = ref(false);
const editingId = ref(null);
// 只有被授权的功能块才出现对应的业务类型：A 搬运 = carry，B 监听 = monitor
const canCarry = computed(() => auth.hasModule("carry"));
const canMonitor = computed(() => auth.hasModule("monitor"));

const matrix = reactive({
  business_type: canCarry.value ? "A" : "B",
  source_chat_ids: [],
  target_chat_ids: [],
});

const matrixWarnings = computed(() =>
  collectRoleMismatches(
    matrix.business_type,
    targets.value.filter((item) => matrix.target_chat_ids.includes(item.id)),
  ),
);

async function load() {
  loading.value = true;
  try {
    const [routeList, sourceList, targetList] = await Promise.all([
      routesApi.list({ limit: 200 }),
      sourcesApi.list({ limit: 300 }),
      targetsApi.list({ limit: 300 }),
    ]);
    routes.value = routeList.data.items;
    sources.value = sourceList.data.items;
    targets.value = targetList.data.items;
  } catch (error) {
    ElMessage.error(error.message);
  } finally {
    loading.value = false;
  }
}

async function createMatrix() {
  if (!matrix.source_chat_ids.length || !matrix.target_chat_ids.length) {
    ElMessage.warning("请同时勾选监听源与接收目标");
    return;
  }
  try {
    const { data } = await routesApi.matrix({
      source_chat_ids: matrix.source_chat_ids,
      target_chat_ids: matrix.target_chat_ids,
      business_type: matrix.business_type,
      a_config: matrix.business_type === "A" ? { ad_policy: "none" } : undefined,
    });
    const skipped = data.skipped.length ? `，跳过 ${data.skipped.length} 条已存在` : "";
    ElMessage.success(`已创建 ${data.created} 条线路${skipped}`);
    matrix.source_chat_ids = [];
    matrix.target_chat_ids = [];
    load();
  } catch (error) {
    ElMessage.error(error.message);
  }
}

function openCreate() {
  editingId.value = null;
  editorVisible.value = true;
}

function openEdit(row) {
  editingId.value = row.id;
  editorVisible.value = true;
}

async function toggleEnabled(row) {
  try {
    // 多源线路是「一个源一行」，启停要整条一起改
    await routesApi.update(row.id, { enabled: row.enabled, apply_to_bundle: true });
    ElMessage.success(row.enabled ? "线路已启用" : "线路已停用");
  } catch (error) {
    ElMessage.error(error.message);
    load();
  }
}

async function remove(row) {
  const count = row.source_chat_ids?.length || 1;
  try {
    await ElMessageBox.confirm(
      count > 1
        ? `「${row.name}」同时监听了 ${count} 个源，删除会把这一组一起删掉，确认？`
        : `确认删除线路「${row.name}」？`,
      "删除线路",
      { type: "warning", confirmButtonText: "删除", cancelButtonText: "取消" },
    );
    await routesApi.remove(row.id);
    ElMessage.success("线路已删除");
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
      <h2 class="page-title">线路管理</h2>
      <span class="card-hint">共 {{ routes.length }} 条线路</span>
      <div class="spacer" />
      <el-button size="small" type="primary" @click="openCreate">新建线路</el-button>
      <el-button size="small" @click="load">刷新</el-button>
    </div>

    <el-card shadow="never" class="panel-gap">
      <template #header>快速建线（同一批源 × 同一批目标）</template>
      <div class="matrix">
        <div class="matrix-side">
          <div class="matrix-head">业务类型</div>
          <el-radio-group v-model="matrix.business_type">
            <el-radio-button v-if="canCarry" value="A">A 搬运帖子</el-radio-button>
            <el-radio-button v-if="canMonitor" value="B">B 监听会员</el-radio-button>
          </el-radio-group>
          <p class="card-hint">
            <template v-if="canCarry && canMonitor">
              A 线搬运内容并按频率挂广告；B 线监听会员发言并生成线索卡片。
            </template>
            <template v-else-if="canCarry">
              A 线把源群内容按频率搬运到接收群，并可挂广告素材。
            </template>
            <template v-else>
              B 线监听源群里会员的发言，命中关键词后生成线索卡片。
            </template>
          </p>
        </div>
        <div class="matrix-side">
          <div class="matrix-head">监听源（{{ sources.length }}）</div>
          <el-select
            v-model="matrix.source_chat_ids"
            multiple
            collapse-tags
            placeholder="选择监听源"
            style="width: 100%"
          >
            <el-option
              v-for="item in sources"
              :key="item.id"
              :label="item.name || item.title || item.username"
              :value="item.id"
            />
          </el-select>
        </div>
        <div class="matrix-side">
          <div class="matrix-head">接收目标（{{ targets.length }}）</div>
          <el-select
            v-model="matrix.target_chat_ids"
            multiple
            collapse-tags
            placeholder="选择接收目标"
            style="width: 100%"
          >
            <el-option
              v-for="item in targets"
              :key="item.id"
              :label="`${item.name || item.title || item.username}（${targetRoleFullLabel(item.target_role)}）`"
              :value="item.id"
            />
          </el-select>
        </div>
        <el-alert
          v-if="matrixWarnings.length"
          class="matrix-warn"
          type="error"
          :closable="false"
          show-icon
          title="接收目标的用途与业务类型不一致（只是提醒，不会阻止创建）"
        >
          <p v-for="item in matrixWarnings" :key="item.name">{{ item.name }}：{{ item.text }}</p>
        </el-alert>
        <div class="matrix-foot">
          <span class="card-hint">
            将创建 {{ matrix.source_chat_ids.length * matrix.target_chat_ids.length }} 条线路，已存在的自动跳过
          </span>
          <el-button type="primary" size="small" @click="createMatrix">创建线路</el-button>
        </div>
      </div>
    </el-card>

    <el-card shadow="never" class="panel-gap">
      <template #header>线路列表</template>
      <el-table :data="routes" size="small" border>
        <el-table-column prop="id" label="ID" width="70" />
        <el-table-column label="线路名" min-width="200">
          <template #default="{ row }">
            <span>{{ row.name }}</span>
            <el-tag v-if="row.source_count > 1" size="small" type="info" class="bundle-tag">
              多源 · {{ row.source_count }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="类型" width="90">
          <template #default="{ row }">
            <el-tag :type="row.business_type === 'A' ? 'success' : 'warning'" size="small">
              {{ row.business_type === "A" ? "A 搬运" : "B 监听" }}
            </el-tag>
          </template>
        </el-table-column>
        <!-- 一条线路一行：多个监听源全部列在这一格里 -->
        <el-table-column label="监听源" min-width="220">
          <template #default="{ row }">
            <span v-if="row.sources.length" class="chip-list">
              <span v-for="item in row.sources" :key="item.chat_id" class="chip-item">
                <span>{{ item.name }}</span>
                <el-tag v-if="!item.enabled" size="small" type="info">已停</el-tag>
              </span>
            </span>
            <span v-else class="card-hint">未配置</span>
          </template>
        </el-table-column>
        <el-table-column label="接收目标" min-width="240">
          <template #default="{ row }">
            <span v-if="row.targets.length" class="chip-list">
              <span v-for="item in row.targets" :key="item.chat_id" class="chip-item">
                <span>{{ item.name || item.title || item.username }}</span>
                <el-tag size="small" :type="targetRoleTagType(item.target_role)">
                  {{ targetRoleShortLabel(item.target_role) }}
                </el-tag>
                <!-- 只在部分监听源上生效的目标要标出来，否则看不出一致性 -->
                <el-tag
                  v-if="item.source_count < row.source_count"
                  size="small"
                  type="warning"
                >
                  仅 {{ item.source_count }}/{{ row.source_count }} 个源
                </el-tag>
                <el-tag v-if="!item.enabled" size="small" type="info">已停</el-tag>
              </span>
            </span>
            <span v-else class="card-hint">未配置</span>
          </template>
        </el-table-column>
        <el-table-column label="告警" min-width="160">
          <template #default="{ row }">
            <el-tag v-if="row.warnings.length" size="small" type="danger">
              {{ row.warnings[0] }}
            </el-tag>
            <span v-else class="card-hint">正常</span>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="90">
          <template #default="{ row }">
            <el-switch
              v-model="row.enabled"
              size="small"
              @change="() => toggleEnabled(row)"
            />
            <el-tag v-if="row.mixed_enabled" size="small" type="warning" class="bundle-tag">
              部分停用
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="150" fixed="right">
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
      title="改完什么时候生效：哪些要重启，哪些不用"
      description="新建/删除线路、换监听源、改业务类型（A↔B）→ 到「运行总览」点「重启」才生效；改配置（关键词组、净化、广告）、增删接收目标、启停线路 → 立即生效，不用重启。"
    />

    <RouteEditor
      v-model="editorVisible"
      :route-id="editingId"
      :sources="sources"
      :targets="targets"
      @saved="load"
    />
  </div>
</template>

<style scoped>
.panel-gap {
  margin-top: 12px;
}

.matrix {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
  gap: 14px;
}

.matrix-head {
  font-size: 13px;
  color: #64748b;
  margin-bottom: 6px;
}

.matrix-foot {
  grid-column: 1 / -1;
  display: flex;
  align-items: center;
  gap: 12px;
}

.matrix-foot .card-hint {
  margin-right: auto;
}

.matrix-warn {
  grid-column: 1 / -1;
}

.matrix-warn p {
  margin: 2px 0;
  line-height: 1.6;
}

.chip-list {
  display: inline-flex;
  flex-wrap: wrap;
  gap: 6px 10px;
}

.chip-item {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}

.bundle-tag {
  margin-left: 6px;
}
</style>
