import ElementPlus from "element-plus";
import zhCn from "element-plus/es/locale/lang/zh-cn";
import { createApp } from "vue";
import "element-plus/dist/index.css";

import App from "./App.vue";
import { authApi } from "./api";
import router from "./router";
import { auth } from "./stores/auth";
import "./style.css";

createApp(App).use(router).use(ElementPlus, { locale: zhCn }).mount("#app");

// 刷新页面后补一次身份同步：菜单与路由守卫依赖 account_type / modules，
// 老会话（P2 之前存的）里没有这些字段。401 由响应拦截器统一处理。
if (auth.isAuthenticated) {
  authApi
    .check()
    .then(({ data }) => auth.update(data))
    .catch(() => {});
}

// 页面版本自检：浏览器若仍在用旧的前端产物，自动刷新一次，避免"改了却看不到"
const currentBundle = import.meta.url.split("/").pop();
fetch("/api/meta", { cache: "no-store" })
  .then((response) => (response.ok ? response.json() : null))
  .then((meta) => {
    if (!meta?.bundle || meta.bundle === currentBundle) return;
    if (sessionStorage.getItem("bundle-reloaded") === meta.bundle) return;
    sessionStorage.setItem("bundle-reloaded", meta.bundle);
    window.location.reload();
  })
  .catch(() => {});
