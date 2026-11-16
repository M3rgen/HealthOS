import { button, card, el, estimated, prices } from './plan-dom.js';
import { text } from './i18n/plan.js';

/** @param {import('./plan-types').ShoppingItem} item @param {import('./plan-types').PlanActions} actions */
function shoppingItem(item, actions) {
  const label = el('label', '', 'plan-shopping-item');
  const input = el('input');
  input.type = 'checkbox';
  input.dataset.itemId = item.id;
  input.checked = actions.state.checked_item_ids.includes(item.id);
  input.onchange = () => {
    const ids = new Set(actions.state.checked_item_ids);
    if (input.checked) ids.add(item.id);
    else ids.delete(item.id);
    actions.changeState({ checked_item_ids: [...ids] });
  };
  const words = el('span');
  words.append(el('strong', item.name), el('span', item.quantity));
  if (item.store) words.append(el('span', item.store, 'fine-print'));
  const price = prices([item]);
  if (price) words.append(el('span', price));
  label.append(input, words);
  return label;
}
/** @param {HTMLElement} node @param {import('./plan-types').Plan} plan @param {import('./plan-types').PlanActions} actions */
export function renderShopping(node, plan, actions) {
  const groups = [...(plan.shopping?.groups ?? [])];
  if (plan.shopping?.one_off?.length) groups.push({ name: text.oneOff, items: plan.shopping.one_off });
  for (const group of groups.filter(group => group.items.length)) {
    const panel = card(group.name);
    for (const item of group.items) panel.append(shoppingItem(item, actions));
    estimated(panel, group.items);
    node.append(panel);
  }
  estimated(node, groups.flatMap(group => group.items));
  const reset = button(text.reset, () => {
    if (window.confirm(text.confirmReset)) actions.changeState({ checked_item_ids: [] });
  });
  reset.id = 'plan-reset';
  node.append(reset);
}
