<script setup lang="ts">
import MaterialIcon from "./components/MaterialIcon.vue";
import { computed, onUnmounted, ref, watch } from "vue";
import { useRoute } from "vue-router";
import { useQueryClient } from "@tanstack/vue-query";
import { useApiQuery as query, useAction } from "./queries";
const route = useRoute(),
  cache = useQueryClient();
const { action, busy } = useAction();
const navigationOpen = ref(false);
watch(
  () => route.path,
  () => {
    navigationOpen.value = false;
  },
);
const nav = [
  ["/", "概览", "dashboard"],
  ["/running", "执行中课程", "videocam"],
  ["/courses", "待执行课程", "calendar"],
  ["/executions", "执行记录", "history"],
  ["/files", "产物浏览", "folder"],
  ["/homework", "作业", "assignment"],
  ["/classroom-events", "点名与二维码", "qr"],
  ["/settings", "全部设置", "settings"],
  ["/auth", "登录与鉴权", "lock"],
  ["/logs", "服务日志", "logs"],
] as const;
const title = computed(
  () => nav.find((x) => x[0] === route.path)?.[1] || "概览",
);
const health = query("health", "/health", 10000);
let stream: EventSource | undefined;
let reconnect: ReturnType<typeof setTimeout> | undefined;
function connect() {
  stream = new EventSource("/api/events");
  stream.onerror = () => {
    stream?.close();
    reconnect = setTimeout(connect, 3000);
  };
  stream.addEventListener("state", (event) => {
    const state = JSON.parse((event as MessageEvent).data);
    const previous = cache.getQueryData<any[]>(["executions"]);
    cache.setQueryData(["executions"], state.executions);
    if (JSON.stringify(previous) !== JSON.stringify(state.executions)) {
      for (const key of [
        "files",
        "courses",
        "lectures",
        "assignments",
        "health",
      ])
        cache.invalidateQueries({ queryKey: [key] });
    }
  });
}
connect();
onUnmounted(() => {
  clearTimeout(reconnect);
  stream?.close();
});
</script>
<template>
  <div class="layout">
    <aside class="app-sidebar" :class="{ 'is-open': navigationOpen }">
      <div class="brand-row">
        <RouterLink class="brand" to="/"
          >AutoCanvas<span class="brand-dot">·</span></RouterLink
        >
        <button
          class="menu-toggle"
          type="button"
          :aria-expanded="navigationOpen"
          aria-controls="app-navigation"
          @click="navigationOpen = !navigationOpen"
        >
          {{ navigationOpen ? "收起导航" : "导航" }}
        </button>
      </div>
      <div id="app-navigation" class="sidebar-navigation">
        <nav aria-label="工作空间">
          <RouterLink
            v-for="item in nav.slice(0, 7)"
            :key="item[0]"
            :to="item[0]"
          >
            <MaterialIcon class="nav-icon" :name="item[2]" />{{ item[1] }}
          </RouterLink>
        </nav>
        <div class="sidebar-bottom">
          <nav aria-label="服务与设置">
            <RouterLink
              v-for="item in nav.slice(7)"
              :key="item[0]"
              :to="item[0]"
            >
              <MaterialIcon class="nav-icon" :name="item[2]" />{{ item[1] }}
            </RouterLink>
          </nav>
        </div>
      </div>
    </aside>
    <main class="app-main">
      <div class="content">
        <div
          v-if="health.isError.value && route.path !== '/homework'"
          class="error-banner"
        >
          无法连接 API。请启动 AutoCanvas 服务，并检查 WebUI
          的代理地址。<el-button @click="cache.invalidateQueries()"
            >重试</el-button
          >
        </div>
        <div
          v-else-if="health.data.value && health.data.value.status !== 'ok'"
          class="notice-strip"
          role="status"
        >
          服务状态：{{ health.data.value.status }}
          <RouterLink to="/logs">查看日志</RouterLink>
        </div>
        <div class="page-heading">
          <h1>{{ title }}</h1>
          <el-button
            v-if="route.path === '/' || route.path === '/courses'"
            :loading="busy"
            @click="action('/api/sync')"
            ><MaterialIcon
              name="sync"
              class="button-icon"
            />同步课表与课程</el-button
          >
        </div>
        <RouterView />
      </div>
    </main>
  </div>
</template>
