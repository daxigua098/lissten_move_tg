<script setup>
import { ElMessage } from "element-plus";
import { computed, onMounted, reactive, ref } from "vue";

import { accountsApi, http, outreachApi } from "../../api";

const loading = ref(false);
const rows = ref([]);
const total = ref(0);
const detailVisible = ref(false);
const detailLoading = ref(false);
const detail = ref(null);
const messages = ref([]);
const accountOptions = ref([]);
const dateRange = ref([]);
const filters = reactive({
  status: "SENT",
  message_kind: "",
  trigger_type: "",
  account_id: null,
  keyword: "",
  limit: 50,
  offset: 0,
});

const statusLabels = {
  SENT: "已发送",
  FAILED: "发送失败",
  BLOCKED: "已阻塞",
  UNKNOWN_DELIVERY: "待核实",
  CANCELLED: "已取消",
  QUEUED: "排队中",
};

function params() {
  return {
    status: filters.status || undefined,
    message_kind: filters.message_kind || undefined,
    trigger_type: filters.trigger_type || undefined,
    account_id: filters.account_id || undefined,
    keyword: filters.keyword || undefined,
    start: dateRange.value?.[0] || undefined,
    end: dateRange.value?.[1] || undefined,
    limit: filters.limit,
    offset: filters.offset,
  };
}

async function load() {
  loading.value = true;
  try {
    const { data } = await outreachApi.records(params());
    rows.value = data.items;
    total.value = data.total;
  } catch (error) {
    ElMessage.error(error.message);
  } finally {
    loading.value = false;
  }
}

async function loadAccounts() {
  try {
    const { data } = await accountsApi.list({ purpose: "outreach", limit: 200 });
    accountOptions.value = data.items;
  } catch {
    accountOptions.value = [];
  }
}

function search() {
  filters.offset = 0;
  load();
}

function onPage(page) {
  filters.offset = (page - 1) * filters.limit;
  load();
}

async function openDetail(row) {
  detail.value = row;
  detailVisible.value = true;
  detailLoading.value = true;
  messages.value = [];
  try {
    if (row.contact_id) {
      const { data } = await outreachApi.contactMessages(row.contact_id, { limit: 200 });
      messages.value = data.items;
    }
  } catch (error) {
    ElMessage.error(error.message);
  } finally {
    detailLoading.value = false;
  }
}

function exportCsv() {
  const url = outreachApi.recordsExportUrl(params());
  http
    .get(url, { responseType: "blob" })
    .then((response) => {
      const objectUrl = URL.createObjectURL(response.data);
      const link = document.createElement("a");
      link.href = objectUrl;
      link.download = `冷触达发送记录-${new Date().toISOString().slice(0, 10)}.csv`;
      link.click();
      URL.revokeObjectURL(objectUrl);
      ElMessage.success("已导出 CSV");
    })
    .catch((error) => ElMessage.error(error.message));
}

function recipientName(row) {
  return row.recipient_display_name || row.contact_display_name || row.recipient_username || "-";
}

function recipientUsername(row) {
  return row.recipient_username || row.contact_username || "";
}

function fmt(value) {
  return value ? new Date(value).toLocaleString("zh-CN") : "-";
}

const currentContactTitle = computed(
  () => detail.value?.contact_display_name || detail.value?.recipient_display_name || "联系人",
);

onMounted(async () => {
  await loadAccounts();
  await load();
});
</script>

<template>
  <div>
    <div class="toolbar">
      <h2 class="page-title">发送记录</h2>
      <span class="card-hint">共 {{ total }} 条</span>
      <div class="spacer" />
      <el-button size="small" @click="exportCsv">导出 CSV</el-button>
      <el-button size="small" @click="load">刷新</el-button>
    </div>

    <el-card shadow="never" class="panel">
      <div class="filters">
        <el-select v-model="filters.status" size="small" clearable placeholder="全部状态" style="width: 130px">
          <el-option label="已发送" value="SENT" />
          <el-option label="发送失败" value="FAILED" />
          <el-option label="已阻塞" value="BLOCKED" />
          <el-option label="待核实" value="UNKNOWN_DELIVERY" />
        </el-select>
        <el-select v-model="filters.message_kind" size="small" clearable placeholder="全部类型" style="width: 130px">
          <el-option label="首触消息" value="first_contact" />
          <el-option label="跟进消息" value="follow_up" />
          <el-option label="自动回复" value="auto_reply" />
          <el-option label="Bot 转交" value="handoff" />
          <el-option label="历史记录" value="legacy" />
        </el-select>
        <el-select v-model="filters.trigger_type" size="small" clearable placeholder="全部触发方式" style="width: 150px">
          <el-option label="人工立即发送" value="manual" />
          <el-option label="后台自动调度" value="scheduler" />
          <el-option label="历史任务" value="legacy" />
        </el-select>
        <el-select v-model="filters.account_id" size="small" clearable placeholder="全部发送账号" style="width: 180px">
          <el-option v-for="item in accountOptions" :key="item.id" :label="item.name" :value="item.id" />
        </el-select>
        <el-date-picker
          v-model="dateRange"
          size="small"
          type="datetimerange"
          value-format="YYYY-MM-DDTHH:mm:ss"
          range-separator="至"
          start-placeholder="开始时间"
          end-placeholder="结束时间"
          style="width: 340px"
        />
        <el-input
          v-model="filters.keyword"
          size="small"
          placeholder="搜索正文"
          clearable
          style="width: 170px"
          @keyup.enter="search"
        />
        <el-button size="small" type="primary" @click="search">查询</el-button>
      </div>
    </el-card>

    <el-table v-loading="loading" :data="rows" size="small" border class="panel">
      <el-table-column label="发送时间" width="170">
        <template #default="{ row }">{{ fmt(row.sort_at) }}</template>
      </el-table-column>
      <el-table-column label="接收人" min-width="190">
        <template #default="{ row }">
          <el-button link type="primary" @click="openDetail(row)">{{ recipientName(row) }}</el-button>
          <div class="card-hint">
            {{ recipientUsername(row) ? `@${recipientUsername(row)}` : "" }}
            {{ row.recipient_tg_user_id || row.contact_tg_user_id || "" }}
          </div>
        </template>
      </el-table-column>
      <el-table-column prop="account_name" label="发送账号" min-width="130" />
      <el-table-column label="类型" width="100">
        <template #default="{ row }">{{ row.message_kind_label }}</template>
      </el-table-column>
      <el-table-column label="状态" width="100">
        <template #default="{ row }">
          <el-tag size="small">{{ statusLabels[row.status] || row.status }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="触发方式" width="120">
        <template #default="{ row }">{{ row.trigger_label || "-" }}</template>
      </el-table-column>
      <el-table-column label="内容" min-width="260" show-overflow-tooltip>
        <template #default="{ row }">{{ row.content || "-" }}</template>
      </el-table-column>
      <el-table-column label="附件" width="80">
        <template #default="{ row }">{{ row.media_kind ? "有" : "-" }}</template>
      </el-table-column>
      <el-table-column label="来源群" min-width="130" show-overflow-tooltip>
        <template #default="{ row }">{{ row.source_title || "-" }}</template>
      </el-table-column>
    </el-table>

    <el-pagination
      class="panel"
      layout="prev, pager, next, total"
      :total="total"
      :page-size="filters.limit"
      :current-page="Math.floor(filters.offset / filters.limit) + 1"
      @current-change="onPage"
    />

    <el-drawer v-model="detailVisible" :title="`发送详情 · ${currentContactTitle}`" size="620px">
      <div v-loading="detailLoading">
        <template v-if="detail">
          <el-descriptions :column="1" border size="small" title="接收人">
            <el-descriptions-item label="昵称">{{ recipientName(detail) }}</el-descriptions-item>
            <el-descriptions-item label="用户名">
              {{ recipientUsername(detail) || "-" }}
            </el-descriptions-item>
            <el-descriptions-item label="TG 用户 ID">
              {{ detail.recipient_tg_user_id || detail.contact_tg_user_id || "-" }}
            </el-descriptions-item>
            <el-descriptions-item label="手机号">{{ detail.contact_phone || "-" }}</el-descriptions-item>
          </el-descriptions>

          <el-descriptions class="panel" :column="1" border size="small" title="本次发送">
            <el-descriptions-item label="发送时间">{{ fmt(detail.sort_at) }}</el-descriptions-item>
            <el-descriptions-item label="发送账号">{{ detail.account_name || "-" }}</el-descriptions-item>
            <el-descriptions-item label="类型">{{ detail.message_kind_label }}</el-descriptions-item>
            <el-descriptions-item label="状态">{{ statusLabels[detail.status] || detail.status }}</el-descriptions-item>
            <el-descriptions-item label="触发方式">{{ detail.trigger_label || "-" }}</el-descriptions-item>
            <el-descriptions-item label="操作者">{{ detail.triggered_by || "-" }}</el-descriptions-item>
            <el-descriptions-item label="Telegram 消息 ID">{{ detail.tg_message_id || "-" }}</el-descriptions-item>
            <el-descriptions-item label="来源群">{{ detail.source_title || "-" }}</el-descriptions-item>
            <el-descriptions-item label="关键词">{{ detail.keyword || "-" }}</el-descriptions-item>
            <el-descriptions-item label="错误">{{ detail.last_error || "-" }}</el-descriptions-item>
            <el-descriptions-item label="发送内容">
              <div class="message-text">{{ detail.content || "-" }}</div>
            </el-descriptions-item>
            <el-descriptions-item v-if="detail.media_url" label="附件">
              <a :href="detail.media_url" target="_blank" rel="noreferrer">查看附件</a>
            </el-descriptions-item>
          </el-descriptions>

          <el-timeline v-if="messages.length" class="panel">
            <el-timeline-item
              v-for="item in messages"
              :key="item.id"
              :timestamp="fmt(item.sent_at)"
              :type="item.direction === 'in' ? 'success' : 'primary'"
            >
              <div class="message-line">
                <el-tag size="small" :type="item.direction === 'in' ? 'success' : 'primary'">
                  {{ item.direction === "in" ? "收到" : "发出" }}
                </el-tag>
                <span class="card-hint">{{ item.message_kind_label }}</span>
              </div>
              <div class="message-text">{{ item.text || "-" }}</div>
            </el-timeline-item>
          </el-timeline>
        </template>
      </div>
    </el-drawer>
  </div>
</template>

<style scoped>
.panel {
  margin-top: 12px;
}

.filters {
  display: flex;
  gap: 8px;
  align-items: center;
  flex-wrap: wrap;
}

.message-line {
  display: flex;
  gap: 8px;
  align-items: center;
}

.message-text {
  white-space: pre-wrap;
  word-break: break-word;
}
</style>
