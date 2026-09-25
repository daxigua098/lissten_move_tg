import ElementPlus from "element-plus";
import zhCn from "element-plus/es/locale/lang/zh-cn";
import { createApp } from "vue";
import "element-plus/dist/index.css";

import App from "./App.vue";
import router from "./router";
import "./style.css";

createApp(App).use(router).use(ElementPlus, { locale: zhCn }).mount("#app");
