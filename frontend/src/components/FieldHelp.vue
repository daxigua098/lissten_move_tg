<script setup>
import { ref } from "vue";

/**
 * F-R17 的 ⓘ 帮助窗。
 *
 * 只用于"选错要付出代价"的开关：内容固定三段（优点 / 缺点与风险 / 适合场景）
 * 加一句推荐，给出建议但不替用户决策。文案必须与实际行为一致。
 */
defineProps({
  title: { type: String, required: true },
  pros: { type: Array, default: () => [] },
  cons: { type: Array, default: () => [] },
  fit: { type: Array, default: () => [] },
  recommend: { type: String, default: "" },
  label: { type: String, default: "用途说明" },
});

const visible = ref(false);
</script>

<template>
  <span class="field-help">
    <span
      class="help-icon"
      role="button"
      tabindex="0"
      :title="label"
      @click="visible = true"
      @keyup.enter="visible = true"
      >ⓘ</span
    >

    <el-dialog v-model="visible" :title="title" width="620px" top="8vh">
      <div class="help-body">
        <section>
          <h4 class="good">优点</h4>
          <ul>
            <li v-for="item in pros" :key="item">{{ item }}</li>
          </ul>
        </section>
        <section>
          <h4 class="bad">缺点与风险</h4>
          <ul>
            <li v-for="item in cons" :key="item">{{ item }}</li>
          </ul>
        </section>
        <section>
          <h4 class="fit">适合场景</h4>
          <ul>
            <li v-for="item in fit" :key="item">{{ item }}</li>
          </ul>
        </section>
        <p v-if="recommend" class="recommend">{{ recommend }}</p>
      </div>
      <template #footer>
        <el-button type="primary" @click="visible = false">我知道了</el-button>
      </template>
    </el-dialog>
  </span>
</template>

<style scoped>
.field-help {
  display: inline-flex;
  align-items: center;
  margin-left: 4px;
}

.help-icon {
  display: inline-grid;
  place-items: center;
  width: 16px;
  height: 16px;
  border-radius: 50%;
  border: 1px solid var(--tg-border);
  color: var(--tg-muted);
  font-size: 11px;
  line-height: 1;
  cursor: pointer;
  user-select: none;
}

.help-icon:hover {
  color: var(--tg-accent);
  border-color: var(--tg-accent);
}

.help-body {
  max-height: 62vh;
  overflow-y: auto;
  font-size: 13px;
  line-height: 1.75;
}

.help-body section {
  margin-bottom: 12px;
}

.help-body h4 {
  margin: 0 0 4px;
  font-size: 14px;
}

.help-body ul {
  margin: 0;
  padding-left: 20px;
}

.good {
  color: #67c23a;
}

.bad {
  color: #f56c6c;
}

.fit {
  color: #409eff;
}

.recommend {
  margin: 0;
  padding: 8px 10px;
  border-left: 3px solid #d9ecff;
  background: #f4f8ff;
  color: #606266;
}
</style>
