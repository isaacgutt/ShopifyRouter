const fetch = require("node-fetch");

const TOKEN = process.env.SHOPIFY_ADMIN_ACCESS_TOKEN;
const API_VERSION = process.env.SHOPIFY_ADMIN_API_VERSION || "2024-10";

function toNumericId(id) {
  if (typeof id === "number") return id;
  if (typeof id === "string") {
    if (id.includes("/")) return Number(id.split("/").pop());
    return Number(id);
  }
  return Number(id);
}

function chunk(arr, size) {
  const out = [];
  for (let i = 0; i < arr.length; i += size) out.push(arr.slice(i, i + size));
  return out;
}

async function shopifyREST(shopDomain, path, options = {}) {
  if (!shopDomain) throw new Error("Missing shopDomain");
  if (!TOKEN) throw new Error("Missing SHOPIFY_ADMIN_ACCESS_TOKEN");

  const url = `https://${shopDomain}/admin/api/${API_VERSION}/${path}`;
  const res = await fetch(url, {
    method: options.method || "GET",
    headers: {
      "X-Shopify-Access-Token": TOKEN,
      "Content-Type": "application/json",
    },
    body: options.body ? JSON.stringify(options.body) : undefined,
  });

  const text = await res.text();
  let json;
  try {
    json = JSON.parse(text);
  } catch {
    json = { raw: text };
  }

  if (!res.ok) {
    throw new Error(`Shopify REST error ${res.status}: ${JSON.stringify(json)}`);
  }
  return json;
}

// variant_id -> inventory_item_id (both numeric)
async function getInventoryItemIds(shopDomain, lineItems) {
  const variantIds = lineItems
    .map((li) => toNumericId(li.variant_id))
    .filter(Boolean);

  const map = {};

  for (const idsChunk of chunk(variantIds, 50)) {
    const idsParam = idsChunk.join(",");
    const data = await shopifyREST(
      shopDomain,
      `variants.json?ids=${idsParam}&fields=id,inventory_item_id`
    );

    for (const v of data.variants || []) {
      map[Number(v.id)] = Number(v.inventory_item_id);
    }
  }

  // Fallback for any missing variant IDs
  const missing = variantIds.filter((id) => !map[id]);
  for (const id of missing) {
    try {
      const data = await shopifyREST(
        shopDomain,
        `variants/${id}.json?fields=id,inventory_item_id`
      );
      if (data.variant) {
        map[Number(data.variant.id)] = Number(data.variant.inventory_item_id);
      }
    } catch (e) {
      console.warn("⚠️ Missing variant in Shopify:", id, e.message);
    }
  }

  return map;
}

async function getAvailabilityForOrder(shopDomain, lineItems, locationIds) {
  const normalizedLineItems = (lineItems || [])
    .filter((li) => li.variant_id && li.quantity)
    .map((li) => ({
      variant_id: toNumericId(li.variant_id),
      quantity: Number(li.quantity),
    }));

  const locationIdsNum = (locationIds || []).map((id) => Number(id)).filter(Boolean);
  const activeLocSet = new Set(locationIdsNum.map((x) => String(x)));

  const availability = {};
  for (const li of normalizedLineItems) availability[li.variant_id] = {};

  const variantToInv = await getInventoryItemIds(shopDomain, normalizedLineItems);

  const inventoryItemIds = Array.from(
    new Set(
      normalizedLineItems
        .map((li) => variantToInv[Number(li.variant_id)])
        .filter(Boolean)
    )
  );

  console.log("variantToInv:", variantToInv);
  console.log("inventoryItemIds:", inventoryItemIds);

  if (inventoryItemIds.length === 0) {
    for (const li of normalizedLineItems) {
      for (const loc of locationIdsNum) {
        availability[li.variant_id][String(loc)] = 0;
      }
    }
    return availability;
  }

  const invChunks = chunk(inventoryItemIds, 50);
  const allLevels = [];

  for (const invChunk of invChunks) {
    const invIdsParam = invChunk.join(",");
    const locIdsParam = locationIdsNum.join(",");

    const levels = await shopifyREST(
      shopDomain,
      `inventory_levels.json?inventory_item_ids=${invIdsParam}&location_ids=${locIdsParam}`
    );

    for (const lvl of levels.inventory_levels || []) {
      allLevels.push(lvl);
    }
  }

  console.log("allLevels count:", allLevels.length);
  console.log(
    "allLevels locations:",
    [...new Set(allLevels.map((l) => l.location_id))]
  );
  console.log("activeLocSet:", [...activeLocSet]);

  const invToLocAvail = {};
  for (const lvl of allLevels) {
    const locId = String(lvl.location_id);
    if (!activeLocSet.has(locId)) continue;

    const invId = String(lvl.inventory_item_id);
    if (!invToLocAvail[invId]) invToLocAvail[invId] = {};
    invToLocAvail[invId][locId] = Number(lvl.available ?? 0);
  }

  for (const li of normalizedLineItems) {
    const invId = String(variantToInv[Number(li.variant_id)]);
    const locMap = invToLocAvail[invId] || {};

    for (const loc of locationIdsNum) {
      const k = String(loc);
      availability[li.variant_id][k] = Number(locMap[k] ?? 0);
    }
  }

  for (const li of normalizedLineItems) {
    for (const loc of locationIdsNum) {
      const k = String(loc);
      if (availability[li.variant_id][k] === undefined) {
        availability[li.variant_id][k] = 0;
      }
    }
  }

  console.log("availability:", availability);
  return availability;
}

async function shopifyGraphQL(shopDomain, query, variables = {}) {
  if (!shopDomain) throw new Error("Missing shopDomain");
  if (!TOKEN) throw new Error("Missing SHOPIFY_ADMIN_ACCESS_TOKEN");

  const url = `https://${shopDomain}/admin/api/${API_VERSION}/graphql.json`;
  const res = await fetch(url, {
    method: "POST",
    headers: {
      "X-Shopify-Access-Token": TOKEN,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ query, variables }),
  });

  const text = await res.text();
  let json;
  try { json = JSON.parse(text); } catch { json = { raw: text }; }

  if (!res.ok || json.errors) {
    throw new Error(`Shopify GraphQL error ${res.status}: ${JSON.stringify(json)}`);
  }
  return json;
}

async function moveFulfillmentOrder(shopDomain, orderId, newLocationId) {
  // Try REST first
  let data = await shopifyREST(
    shopDomain,
    `orders/${orderId}/fulfillment_orders.json?status=any`
  );

  let fo = (data.fulfillment_orders || []).find(
    (f) => f.status !== "closed" && f.status !== "cancelled"
  );

  // Fallback to GraphQL if REST returns []
  if (!fo) {
    const gql = await shopifyGraphQL(
      shopDomain,
      `query ($id: ID!) {
        order(id: $id) {
          fulfillmentOrders(first: 10) {
            edges { node { id status assignedLocation { location { id } } } }
          }
        }
      }`,
      { id: `gid://shopify/Order/${orderId}` }
    );

    const node = gql?.data?.order?.fulfillmentOrders?.edges?.[0]?.node;
    if (!node) return { moved: false, reason: "no fulfillment orders visible" };

    fo = {
      id: toNumericId(node.id),
      status: node.status,
      assigned_location_id: toNumericId(node.assignedLocation?.location?.id),
    };
  }

  const foId = toNumericId(fo.id);

  await shopifyREST(shopDomain, `fulfillment_orders/${foId}/move.json`, {
    method: "POST",
    body: { fulfillment_order: { new_location_id: Number(newLocationId) } },
  });

  return {
    moved: true,
    fulfillmentOrderId: foId,
    fromLocationId: fo.assigned_location_id,
    toLocationId: Number(newLocationId),
    status: fo.status,
  };
}



// Returns all active fulfillment orders for an order, with their line items.
async function getFulfillmentOrders(shopDomain, orderId) {
  const data = await shopifyREST(
    shopDomain,
    `orders/${orderId}/fulfillment_orders.json?status=any`
  );

  const fos = (data.fulfillment_orders || []).filter(
    (f) => f.status !== "closed" && f.status !== "cancelled"
  );

  if (fos.length > 0) {
    return fos.map((f) => ({
      id: f.id,
      gid: `gid://shopify/FulfillmentOrder/${f.id}`,
      status: f.status,
      assigned_location_id: Number(f.assigned_location_id),
      line_items: (f.line_items || []).map((li) => ({
        id: li.id,
        gid: `gid://shopify/FulfillmentOrderLineItem/${li.id}`,
        variant_id: Number(li.variant_id),
        fulfillable_quantity: li.fulfillable_quantity,
      })),
    }));
  }

  // GraphQL fallback
  const gql = await shopifyGraphQL(
    shopDomain,
    `query ($id: ID!) {
      order(id: $id) {
        fulfillmentOrders(first: 10) {
          edges {
            node {
              id
              status
              assignedLocation {
                location { id }
              }
              lineItems(first: 50) {
                edges {
                  node {
                    id
                    remainingQuantity
                    variant { id }
                  }
                }
              }
            }
          }
        }
      }
    }`,
    { id: `gid://shopify/Order/${orderId}` }
  );

  return (gql?.data?.order?.fulfillmentOrders?.edges || [])
    .filter(({ node }) => node.status !== "CLOSED" && node.status !== "CANCELLED")
    .map(({ node }) => ({
      id: toNumericId(node.id),
      gid: node.id,
      status: node.status,
      assigned_location_id: toNumericId(node.assignedLocation?.location?.id),
      line_items: (node.lineItems?.edges || []).map(({ node: li }) => ({
        id: toNumericId(li.id),
        gid: li.id,
        variant_id: toNumericId(li.variant?.id),
        fulfillable_quantity: li.remainingQuantity,
      })),
    }));
}

// Splits line items out of a fulfillment order into a new FO, leaving the rest in a remainingFulfillmentOrder.
// foGid: "gid://shopify/FulfillmentOrder/<n>"
// lineItemInputs: [{ id: "gid://shopify/FulfillmentOrderLineItem/<n>", quantity: <n> }]
async function splitFulfillmentOrder(shopDomain, foGid, lineItemInputs) {
  const result = await shopifyGraphQL(
    shopDomain,
    `mutation FulfillmentOrderSplit($fulfillmentOrderSplits: [FulfillmentOrderSplitInput!]!) {
      fulfillmentOrderSplit(fulfillmentOrderSplits: $fulfillmentOrderSplits) {
        fulfillmentOrderSplits {
          fulfillmentOrder {
            id
            status
            lineItems(first: 50) {
              edges { node { id remainingQuantity variant { id } } }
            }
          }
          remainingFulfillmentOrder {
            id
            status
            lineItems(first: 50) {
              edges { node { id remainingQuantity variant { id } } }
            }
          }
        }
        userErrors { field message }
      }
    }`,
    {
      fulfillmentOrderSplits: [{
        fulfillmentOrderId: foGid,
        fulfillmentOrderLineItems: lineItemInputs,
      }],
    }
  );

  const userErrors = result?.data?.fulfillmentOrderSplit?.userErrors || [];
  if (userErrors.length > 0) {
    throw new Error(`fulfillmentOrderSplit errors: ${JSON.stringify(userErrors)}`);
  }

  return result.data.fulfillmentOrderSplit.fulfillmentOrderSplits;
}

// Moves a fulfillment order to a new location, skipping if it's already there.
async function moveOrSkipIfAlreadyThere(shopDomain, foId, locationId, label, storeName) {
  try {
    await shopifyREST(shopDomain, `fulfillment_orders/${foId}/move.json`, {
      method: "POST",
      body: { fulfillment_order: { new_location_id: Number(locationId) } },
    });
    console.log(`✂️ ${label}: moved FO ${foId} -> ${storeName}`);
  } catch (e) {
    if (e.message.includes("Cannot move to the current origin location")) {
      console.log(`✂️ ${label}: FO ${foId} already at ${storeName}, skipping move`);
    } else {
      throw e;
    }
  }
}

// Executes split fulfillment: carves FO line items per store assignment, moves each carved FO.
async function executeSplitFulfillment(shopDomain, orderId, splitPlan, preloadedFos = null) {
  const fos = preloadedFos || await getFulfillmentOrders(shopDomain, orderId);
  if (!fos.length) {
    console.warn(`⚠️ No active FOs found for order ${orderId}`);
    return;
  }
  if (fos.length > 1) {
    console.warn(`⚠️ ${fos.length} active FOs found for order ${orderId}, using first`);
  }

  let currentFoId = fos[0].id;
  let currentFoGid = fos[0].gid;
  let currentLineItems = fos[0].line_items;

  const { assignments } = splitPlan;

  for (let i = 0; i < assignments.length; i++) {
    const assignment = assignments[i];
    const isLast = i === assignments.length - 1;

    if (isLast) {
      // Last store: just move the remaining FO directly (no split needed)
      await moveOrSkipIfAlreadyThere(shopDomain, currentFoId, assignment.store.shopify_location_id, `Split[${i}] last store`, assignment.store.name);
    } else {
      // Build GID inputs for this store's items
      const lineItemInputs = assignment.items.map((item) => {
        const foLI = currentLineItems.find((li) => li.variant_id === item.variant_id);
        if (!foLI) throw new Error(`FO line item not found for variant_id ${item.variant_id} in FO ${currentFoId}`);
        return { id: foLI.gid, quantity: item.quantity };
      });

      console.log(`✂️ Split[${i}] splitting FO ${currentFoId} for ${assignment.store.name}:`, lineItemInputs);

      const splits = await splitFulfillmentOrder(shopDomain, currentFoGid, lineItemInputs);
      // Shopify semantics:
      //   fulfillmentOrder         = original FO, now containing the NON-specified items (leftovers)
      //   remainingFulfillmentOrder = new FO containing the SPECIFIED items (what we want at this store)
      const forThisStore = splits[0].remainingFulfillmentOrder;
      const leftovers    = splits[0].fulfillmentOrder;

      const forThisStoreId = toNumericId(forThisStore.id);
      await moveOrSkipIfAlreadyThere(shopDomain, forThisStoreId, assignment.store.shopify_location_id, `Split[${i}]`, assignment.store.name);

      // Continue with the leftover FO (non-specified items) for next iteration
      currentFoId = toNumericId(leftovers.id);
      currentFoGid = leftovers.id;
      currentLineItems = (leftovers.lineItems?.edges || []).map(({ node }) => ({
        id: toNumericId(node.id),
        gid: node.id,
        variant_id: toNumericId(node.variant?.id),
        fulfillable_quantity: node.remainingQuantity,
      }));
    }
  }
}

module.exports = { getAvailabilityForOrder, moveFulfillmentOrder, getFulfillmentOrders, splitFulfillmentOrder, executeSplitFulfillment };

