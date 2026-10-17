import { inject, onUnmounted } from "vue";
import { routerKey, type Router } from "vue-router";

type Draft = { dirty: () => boolean; guardQueryChanges?: boolean };
const registries = new WeakMap<Router, Set<Draft>>();
const message = "有尚未保存的修改，确定放弃并离开吗？";
export function confirmDiscard(dirty: boolean) {
  return !dirty || window.confirm(message);
}
export function confirmUnsavedChanges(router: Router) {
  return confirmDiscard(
    [...(registries.get(router) || [])].some((source) => source.dirty()),
  );
}
/** Draft values remain exclusively in component memory, including secrets. */
export function useUnsavedChanges(
  dirty: () => boolean,
  options: { guardQueryChanges?: boolean } = {},
) {
  const source: Draft = { dirty, ...options };
  const router = inject(routerKey, undefined);
  if (router) {
    let sources = registries.get(router);
    if (!sources) {
      sources = new Set();
      registries.set(router, sources);
      router.beforeEach(
        (to, from) =>
          to.fullPath === from.fullPath ||
          confirmDiscard(
            [...sources!].some(
              (source) =>
                (to.path !== from.path || source.guardQueryChanges) &&
                source.dirty(),
            ),
          ),
      );
    }
    sources.add(source);
    onUnmounted(() => sources!.delete(source));
  }
  const beforeUnload = (event: BeforeUnloadEvent) => {
    if (!dirty()) return;
    event.preventDefault();
    event.returnValue = "";
  };
  window.addEventListener("beforeunload", beforeUnload);
  onUnmounted(() => window.removeEventListener("beforeunload", beforeUnload));
  return () => confirmDiscard(dirty());
}
