function chooseStore({ availability, stores, lineItems }) {
  const evaluations = stores.map((store) => {
    const locId = String(store.shopify_location_id);

    let fulfillable = true;
    let score = 0;
    const missing = [];

    for (const item of lineItems) {
      const avail = availability?.[item.variant_id]?.[locId] ?? 0;

      if (avail >= item.quantity) {
        score += item.quantity;
      } else {
        fulfillable = false;
        score += avail; // partial credit
        missing.push({
          variant_id: item.variant_id,
          required: item.quantity,
          available: avail,
        });
      }
    }

    return { store, fulfillable, score, missing };
  });

  // full fulfillment: first store in priority order that can do it
  const full = evaluations.find((e) => e.fulfillable);
  if (full) {
    return {
      chosenLocationId: full.store.shopify_location_id,
      chosenLocationIds: [full.store.shopify_location_id],
      partial: false,
      reason: `Full fulfillment -> ${full.store.name} (${full.store.bodega_code})`,
      details: { evaluations },
    };
  }

  // best partial
  const best = [...evaluations].sort((a, b) => b.score - a.score)[0];
  if (best && best.score > 0) {
    return {
      chosenLocationId: best.store.shopify_location_id,
      chosenLocationIds: [best.store.shopify_location_id],
      partial: true,
      reason: `Partial fulfillment -> ${best.store.name} (${best.store.bodega_code})`,
      details: { evaluations },
    };
  }

  // fallback
  return {
    chosenLocationId: 0,
    chosenLocationIds: [0],
    partial: false,
    reason: "No stock at any active store -> Default",
    details: { evaluations },
  };
}

// Store-minimizing split: assigns items to as few stores as possible.
// Strategy:
//   1. Iteratively: assign to already-committed stores first, then force-assign
//      items that have only one viable option (opening that store).
//   2. Repeat until no more progress (handles chained dependencies).
//   3. Fallback: any leftover items with multiple options use priority order.
// Returns { assignments: [{ store, items: [{ variant_id, quantity }] }], unassigned: [...] }
function chooseSplitStores({ availability, stores, lineItems }) {
  const remaining = lineItems.map((li) => ({ variant_id: li.variant_id, quantity: li.quantity }));
  const storeMap = {}; // locId -> { store, items: { variant_id: quantity } }

  function availAt(store, variantId) {
    return availability?.[variantId]?.[String(store.shopify_location_id)] ?? 0;
  }

  function assign(store, variantId, qty) {
    const locId = String(store.shopify_location_id);
    if (!storeMap[locId]) storeMap[locId] = { store, items: {} };
    storeMap[locId].items[variantId] = (storeMap[locId].items[variantId] || 0) + qty;
  }

  let changed = true;
  while (changed) {
    changed = false;
    for (const item of remaining) {
      if (item.quantity <= 0) continue;
      const viable = stores.filter((s) => availAt(s, item.variant_id) > 0);

      // Prefer already-committed stores
      for (const store of viable.filter((s) => storeMap[String(s.shopify_location_id)])) {
        if (item.quantity <= 0) break;
        const canTake = Math.min(item.quantity, availAt(store, item.variant_id));
        if (canTake > 0) {
          assign(store, item.variant_id, canTake);
          item.quantity -= canTake;
          changed = true;
        }
      }

      if (item.quantity <= 0) continue;

      // Force-assign if only one uncommitted store can cover it
      const uncommitted = viable.filter((s) => !storeMap[String(s.shopify_location_id)]);
      if (uncommitted.length === 1) {
        const canTake = Math.min(item.quantity, availAt(uncommitted[0], item.variant_id));
        if (canTake > 0) {
          assign(uncommitted[0], item.variant_id, canTake);
          item.quantity -= canTake;
          changed = true;
        }
      }
    }
  }

  // Fallback: still-unresolved items with multiple options — use priority order
  for (const store of stores) {
    if (remaining.every((r) => r.quantity <= 0)) break;
    for (const item of remaining) {
      if (item.quantity <= 0) continue;
      const canTake = Math.min(item.quantity, availAt(store, item.variant_id));
      if (canTake > 0) {
        assign(store, item.variant_id, canTake);
        item.quantity -= canTake;
      }
    }
  }

  // Return in store priority order
  const assignments = stores
    .filter((s) => storeMap[String(s.shopify_location_id)])
    .map((s) => ({
      store: s,
      items: Object.entries(storeMap[String(s.shopify_location_id)].items).map(([vid, qty]) => ({
        variant_id: Number(vid),
        quantity: qty,
      })),
    }));

  return { assignments, unassigned: remaining.filter((r) => r.quantity > 0) };
}

module.exports = { chooseStore, chooseSplitStores };
