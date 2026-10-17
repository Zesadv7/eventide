import type { Router } from "vue-router";
const trackers = new WeakMap<Router, () => void>();
/** Only committed internal locations are eligible; never trust document.referrer. */
export function trackInternalHistory(router: Router) {
  const existing = trackers.get(router);
  if (existing) return existing;
  const history = router.options.history;
  // Memory history has no browser position. Supply the same entry-local metadata
  // so pop/forward and replacement retain their real history semantics in embeds.
  if (typeof history.state.position !== "number") {
    const push = history.push.bind(history);
    const replace = history.replace.bind(history);
    history.push = (to, data) =>
      push(to, { ...data, position: Number(history.state.position) + 1 });
    history.replace = (to, data) =>
      replace(to, { ...data, position: Number(history.state.position) || 0 });
    history.replace(history.location, history.state);
  }
  const position = () => Number(history.state.position);
  const known = new Map<number, string>();
  if (router.currentRoute.value.matched.length)
    known.set(position(), router.currentRoute.value.fullPath);
  router.afterEach((to, _from, failure) => {
    if (!failure) known.set(position(), to.fullPath);
  });
  const back = () => {
    if (known.has(position() - 1)) router.back();
    else void router.push("/chat");
  };
  trackers.set(router, back);
  return back;
}
