// Serialize full-state PUTs. New edits remain optimistic while an older request completes.
// On failure only that batch rolls back; later edits are replayed over the last server state.
/** @typedef {import('./plan-types').PlanState} PlanState */
/** @param {PlanState} state @param {Partial<PlanState>} patch */
function operation(state, patch) {
  const before = new Set(state.checked_item_ids), after = new Set(patch.checked_item_ids ?? state.checked_item_ids);
  const added = [...after].filter(id => !before.has(id)), removed = [...before].filter(id => !after.has(id));
  /** @param {PlanState} base @returns {PlanState} */
  return base => ({
    start_date: patch.start_date === undefined ? base.start_date : patch.start_date,
    checked_item_ids: [...new Set([...base.checked_item_ids.filter(id => !removed.includes(id)), ...added])],
  });
}
/** @param {{initial: PlanState, write: (state: PlanState) => Promise<PlanState>,
 * changed: (state: PlanState, saving: boolean, succeeded: boolean) => void, failed: () => void}} options */
export function createPlanState({ initial, write, changed, failed }) {
  let confirmed = structuredClone(initial), optimistic = confirmed, sending = false;
  /** @type {((state: PlanState) => PlanState)[]} */
  let pending = [];
  /** @type {ReturnType<typeof setTimeout> | undefined} */
  let timer;
  let succeeded = false;
  const publish = () => changed(optimistic, sending || pending.length > 0, succeeded);
  async function flush() {
    if (sending || !pending.length) return;
    sending = true;
    const batch = pending;
    pending = [];
    const sent = batch.reduce((state, apply) => apply(state), confirmed);
    try { confirmed = await write(sent); succeeded = true; }
    catch { succeeded = false; failed(); }
    sending = false;
    optimistic = pending.reduce((state, apply) => apply(state), confirmed);
    publish();
    if (pending.length) await flush();
  }
  return {
    get busy() { return sending || pending.length > 0; },
    /** @param {Partial<PlanState>} patch */
    change(patch) {
      const apply = operation(optimistic, patch);
      pending.push(apply);
      optimistic = apply(optimistic);
      publish();
      clearTimeout(timer);
      timer = setTimeout(flush, 120);
    },
    /** Remote refreshes are ignored during a write; they must not overwrite a newer local edit.
     * @param {PlanState} state */
    replace(state) {
      if (sending || pending.length) return;
      confirmed = optimistic = state;
      succeeded = false;
      publish();
    },
  };
}
