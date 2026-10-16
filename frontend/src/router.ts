import { createRouter, createWebHashHistory } from "vue-router";
import ChatView from "./views/ChatView.vue";
import SettingsView from "./views/SettingsView.vue";
import UsageView from "./views/UsageView.vue";
import DeliveriesView from "./views/DeliveriesView.vue";
import SkillsView from "./views/SkillsView.vue";
import QueueView from "./views/QueueView.vue";
import EvaluationsView from "./views/EvaluationsView.vue";
import IndexesView from "./views/IndexesView.vue";
import HistoryView from "./views/HistoryView.vue";
import SessionsView from "./views/SessionsView.vue";
export const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: "/", redirect: "/chat" },
    { path: "/chat/:sessionId?", name: "chat", component: ChatView },
    { path: "/settings", component: SettingsView },
    { path: "/usage", component: UsageView },
    { path: "/skills", component: SkillsView },
    { path: "/queue", component: QueueView },
    { path: "/history", component: HistoryView },
    { path: "/sessions", component: SessionsView },
    {
      path: "/knowledge",
      component: IndexesView,
    },
    {
      path: "/evaluations",
      component: EvaluationsView,
    },
    {
      path: "/deliveries",
      component: DeliveriesView,
    },
    { path: "/:pathMatch(.*)*", redirect: "/chat" },
  ],
});
