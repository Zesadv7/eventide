import { createRouter, createWebHashHistory } from "vue-router";
import ChatView from "./views/ChatView.vue";
import SettingsView from "./views/SettingsView.vue";
import UsageView from "./views/UsageView.vue";
import CapabilitiesView from "./views/CapabilitiesView.vue";
export const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: "/", redirect: "/chat" },
    { path: "/chat/:sessionId?", name: "chat", component: ChatView },
    { path: "/settings", component: SettingsView },
    { path: "/usage", component: UsageView },
    { path: "/skills", component: CapabilitiesView, props: { area: "skills" } },
    {
      path: "/knowledge",
      component: CapabilitiesView,
      props: { area: "knowledge" },
    },
    {
      path: "/evaluations",
      component: CapabilitiesView,
      props: { area: "evaluations" },
    },
    {
      path: "/deliveries",
      component: CapabilitiesView,
      props: { area: "deliveries" },
    },
    { path: "/:pathMatch(.*)*", redirect: "/chat" },
  ],
});
