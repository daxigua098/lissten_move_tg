<script setup>
import { ref } from "vue";

defineProps({
  label: { type: String, default: "匹配说明" },
  // link=true 渲染成文字链（放在表单里），否则是普通按钮
  link: { type: Boolean, default: false },
});

const visible = ref(false);
</script>

<template>
  <el-button v-if="link" size="small" link type="primary" @click="visible = true">
    {{ label }}
  </el-button>
  <el-button v-else size="small" @click="visible = true">{{ label }}</el-button>

  <el-dialog v-model="visible" title="关键词是怎么判断命中的" width="760px" top="6vh">
    <div class="help">
      <p class="lead">
        一条消息进来，会按下面的顺序判断。理解这三步，就知道关键词该怎么写了。
      </p>

      <h4>第 0 步：先归一化</h4>
      <ul>
        <li>去掉零宽字符、<b>删掉所有空白</b>（空格、换行、制表符）</li>
        <li>英文统一转小写</li>
      </ul>
      <p class="hint">所以「篮 球」「篮　球」和「篮球」在系统眼里是同一个词。</p>

      <h4>第 1 步：排除词（一票否决）</h4>
      <p>
        消息里出现<b>任意一个排除词</b>（共享排除词组 + 本线路自定义词，取并集），
        整条直接丢弃——不记线索、也不推卡片。全量监听同样生效。
      </p>

      <h4>第 2 步：包含匹配（优先，得分 1.0）</h4>
      <p>
        关键词的<b>主词</b>或它的<b>别名</b>只要是消息里的一个子串，就算命中，方式记为
        <code>contains</code>，得分 1.0，并且不再往下算模糊。
      </p>
      <p class="hint">
        「体育 → 篮球」这类"意思相近"就是靠别名实现的，不是靠模糊。别名可以写同义词、
        简称、错别字、英文写法，用逗号或换行分隔。
      </p>

      <h4>第 3 步：模糊匹配（包含没中才算）</h4>
      <ul>
        <li>关键词里的字和消息里的字<b>一个都不重合</b> → 0 分，直接跳过</li>
        <li>关键词比消息还长 → 0 分</li>
        <li>
          在消息上滑动一个<b>和关键词等长</b>的窗口，逐个窗口比对相似度，取最高分，公式是：
          <div class="formula">
            相似度 = 2 × 匹配上的字符数 ÷ (关键词长度 + 窗口长度)
          </div>
        </li>
        <li>最高分 ≥ 阈值就算命中，方式记为 <code>fuzzy</code></li>
      </ul>

      <h4>阈值（按敏感度）</h4>
      <table class="thresholds">
        <thead>
          <tr><th>敏感度</th><th>阈值</th><th>特点</th></tr>
        </thead>
        <tbody>
          <tr><td>宽松（宁可多报）</td><td>0.6</td><td>漏掉的少，噪声多一些</td></tr>
          <tr><td>标准</td><td>0.75</td><td>折中</td></tr>
          <tr><td>严格（宁可少报）</td><td>0.85</td><td>噪声少，容易漏</td></tr>
        </tbody>
      </table>

      <h4>举个例（按宽松档）</h4>
      <table class="examples">
        <thead>
          <tr><th>关键词</th><th>消息</th><th>结果</th></tr>
        </thead>
        <tbody>
          <tr>
            <td>体育（别名：篮球）</td>
            <td>今晚有篮球赛吗</td>
            <td class="ok">✅ 1.0，命中别名「篮球」</td>
          </tr>
          <tr>
            <td>篮球比赛</td>
            <td>今晚篮球比塞谁看</td>
            <td class="ok">✅ 0.75（宽松/标准中，严格不中）</td>
          </tr>
          <tr>
            <td>篮球</td>
            <td>买了副羽毛球拍</td>
            <td class="no">❌ 0.5，不命中</td>
          </tr>
          <tr>
            <td>篮球</td>
            <td>蓝球（错别字）</td>
            <td class="no">❌ 0.5，不命中，要靠别名</td>
          </tr>
        </tbody>
      </table>

      <h4>所以关键词该怎么设计</h4>
      <ol>
        <li>
          <b>优先用「包含 + 别名」</b>：把同义词、简称、常见错别字、英文写法都塞进别名，
          命中质量最高、也最好解释。
        </li>
        <li>
          <b>两个字的词一定要写别名</b>。中文两字词只错一个字相似度就是 0.5，低于宽松档的
          0.6——模糊匹配兜不住，只能靠别名补。
        </li>
        <li><b>三个字以上</b>可以依赖模糊兜错别字（错一个字约 0.75）。</li>
        <li>
          一条消息里即使命中多个关键词，也<b>只记录得分最高的那一个</b>；同一个人 + 同一个
          关键词在冷却窗口内只记一次。
        </li>
        <li>挡噪声交给排除词组，不要靠把关键词写得很长来规避。</li>
        <li>写完用下面的「试跑」粘一段真实消息验证，命中情况一目了然。</li>
      </ol>

      <p class="footnote">
        说明：系统不做语义推断，也不会"猜"你没写过的词。想要"没列过的词也能命中"，
        需要另接本地语义模型（当前未启用）。
      </p>
    </div>

    <template #footer>
      <el-button type="primary" @click="visible = false">我知道了</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.help {
  max-height: 66vh;
  overflow-y: auto;
  padding-right: 8px;
  font-size: 13px;
  line-height: 1.75;
  color: #303133;
}

.help .lead {
  margin-top: 0;
  color: #606266;
}

.help h4 {
  margin: 18px 0 6px;
  font-size: 14px;
}

.help ul,
.help ol {
  margin: 6px 0;
  padding-left: 20px;
}

.help code {
  padding: 0 4px;
  border-radius: 3px;
  background: #f2f3f5;
  font-size: 12px;
}

.formula {
  margin: 6px 0;
  padding: 8px 10px;
  border-radius: 6px;
  background: #f5f7fa;
  font-family: Consolas, Monaco, monospace;
}

.hint {
  margin: 4px 0 0;
  padding: 6px 10px;
  border-left: 3px solid #d9ecff;
  background: #f4f8ff;
  color: #606266;
}

table {
  width: 100%;
  border-collapse: collapse;
  margin: 6px 0;
}

th,
td {
  border: 1px solid #ebeef5;
  padding: 6px 8px;
  text-align: left;
}

th {
  background: #fafafa;
  font-weight: 500;
  color: #606266;
}

.ok {
  color: #67c23a;
}

.no {
  color: #f56c6c;
}

.footnote {
  margin-top: 14px;
  padding-top: 10px;
  border-top: 1px dashed #ebeef5;
  color: #909399;
}
</style>
