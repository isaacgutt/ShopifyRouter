const { getActiveStores, insertRouteDecision } = require("./db");
const { getAvailabilityForOrder, moveFulfillmentOrder, getFulfillmentOrders, executeSplitFulfillment } = require("./shopify");
const { chooseStore, chooseSplitStores } = require("./routerLogic");

const express = require("express");
const crypto = require("crypto");
const { Pool } = require("pg");

const app = express();

// IMPORTANT: raw body required for Shopify HMAC verification
app.use(express.raw({ type: "application/json" }));

const SHOPIFY_WEBHOOK_SECRET = process.env.SHOPIFY_WEBHOOK_SECRET;
const DATABASE_URL = process.env.DATABASE_URL;

if (!SHOPIFY_WEBHOOK_SECRET) {
  console.error("❌ Missing SHOPIFY_WEBHOOK_SECRET env var");
  process.exit(1);
}
if (!DATABASE_URL) {
  console.error("❌ Missing DATABASE_URL env var");
  process.exit(1);
}

const pool = new Pool({ connectionString: DATABASE_URL });

function verifyShopifyWebhook(req) {
  const hmac = req.headers["x-shopify-hmac-sha256"];
  if (!hmac) return false;

  const digest = crypto
    .createHmac("sha256", SHOPIFY_WEBHOOK_SECRET)
    .update(req.body, "utf8")
    .digest("base64");

  try {
    return crypto.timingSafeEqual(Buffer.from(digest), Buffer.from(hmac));
  } catch {
    return false;
  }
}

app.get("/health", async (req, res) => {
  try {
    await pool.query("SELECT 1");
    res.status(200).send("OK");
  } catch (e) {
    console.error("DB health failed:", e.message);
    res.status(500).send("DB NOT OK");
  }
});

app.post("/webhooks/orders-paid", async (req, res) => {
  // 1) Verify signature
  if (!verifyShopifyWebhook(req)) {
    console.error("❌ Invalid Shopify webhook signature");
    return res.status(401).send("Invalid signature");
  }

  // 2) Parse payload
  let order;
  try {
    order = JSON.parse(req.body.toString("utf8"));
  } catch (e) {
    console.error("❌ Bad JSON:", e.message);
    return res.status(400).send("Bad JSON");
  }

  const orderId = order?.id;
  if (!orderId) return res.status(400).send("Missing order.id");

  // Helpful headers for debugging + traceability
  const topic = req.headers["x-shopify-topic"] || null;
  const shopDomain = req.headers["x-shopify-shop-domain"] || null;
  const webhookId = req.headers["x-shopify-webhook-id"] || null;

  // 3) Insert event (idempotency via UNIQUE(order_id))
  let inserted = false;
  try {
    const result = await pool.query(
      `
      INSERT INTO webhook_events (order_id, topic, shop_domain, webhook_id, payload)
      VALUES ($1, $2, $3, $4, $5::jsonb)
      ON CONFLICT (order_id) DO NOTHING
      RETURNING id
      `,
      [orderId, topic, shopDomain, webhookId, req.body.toString("utf8")]
    );
    inserted = result.rowCount === 1;
  } catch (e) {
    console.error("❌ DB insert failed:", e.message);
    // If DB is down, return 500 so Shopify retries later (don’t lose events)
    return res.status(500).send("DB error");
  }

  // 4) Log outcome
  if (!inserted) {
    console.log("↩️ Duplicate webhook ignored:", {
      order_id: orderId,
      order_name: order?.name,
      topic,
    });
    return res.status(200).send("OK");
  }

  console.log("✅ New order stored:", {
    order_id: orderId,
    order_name: order?.name,
    total_price: order?.total_price,
    financial_status: order?.financial_status,
    topic,
    shop_domain: shopDomain,
  });

  // ✅ REAL routing: inventory-aware store selection
  try {
    const lineItems = (order.line_items || [])
      .filter((li) => li.variant_id && li.quantity)
      .map((li) => ({
        variant_id: Number(li.variant_id),
        quantity: Number(li.quantity),
        sku: li.sku || null,
        title: li.title || null,
      }));
    console.log("🧾 Using these order variants:", lineItems);

    if (lineItems.length === 0) {
      console.log("⚠️ No variant line items to route; skipping routing", { order_id: orderId });
    } else {
      // 🛡️ Safety net: flag orders exceeding 5 total units
      const totalUnits = lineItems.reduce((sum, li) => sum + li.quantity, 0);
      const overLimit = totalUnits > 5;
      if (overLimit) {
        console.warn("🚨 SAFETY NET: order exceeds 5-unit limit", {
          order_id: orderId,
          order_name: order?.name,
          totalUnits,
        });
      }

      const stores = await getActiveStores(pool);
      const locationIds = stores.map((s) => Number(s.shopify_location_id));
      console.log("🏬 locationIds used:", locationIds);

      if (locationIds.length === 0) {
        console.error("❌ No active stores available for routing (excluding Default/Daoro)");
      } else {
        // Fetch FOs first so we can correct for Shopify's auto-committed inventory.
        // When an order is paid, Shopify immediately commits stock at a random location,
        // reducing `available` by that quantity. We add it back before routing.
        const activeFos = await getFulfillmentOrders(shopDomain, orderId);
        const availability = await getAvailabilityForOrder(shopDomain, lineItems, locationIds);

        // Build correction: for each FO, add back the committed quantity at its auto-assigned location
        for (const fo of activeFos) {
          const locId = String(fo.assigned_location_id);
          for (const li of fo.line_items) {
            const vid = String(li.variant_id);
            if (availability[vid]?.[locId] !== undefined) {
              availability[vid][locId] += li.fulfillable_quantity;
            }
          }
        }
        console.log("📦 Corrected availability (after undoing Shopify auto-commit):", availability);

        const decision = chooseStore({ availability, stores, lineItems });

        // Determine if we need a split (partial + more than one store has stock)
        let splitPlan = null;
        if (decision.partial && Number(decision.chosenLocationId) !== 0) {
          splitPlan = chooseSplitStores({ availability, stores, lineItems });
          if (splitPlan.assignments.length > 1) {
            const allLocationIds = splitPlan.assignments.map((a) => a.store.shopify_location_id);
            decision.chosenLocationId = allLocationIds[0];
            decision.chosenLocationIds = allLocationIds;
            decision.reason = `Split fulfillment across ${allLocationIds.length} stores`;
            decision.details = {
              ...decision.details,
              splitAssignments: splitPlan.assignments.map((a) => ({
                store: a.store.name,
                bodega_code: a.store.bodega_code,
                locationId: a.store.shopify_location_id,
                items: a.items,
              })),
              unassigned: splitPlan.unassigned,
            };
          }
        }

        // Merge safety-net flag into details before DB write
        decision.details = { ...decision.details, overLimit, totalUnits };

        // Persist route decision BEFORE Shopify API calls (idempotent audit trail)
        await insertRouteDecision(pool, {
          orderId,
          chosenLocationId: decision.chosenLocationId,
          chosenLocationIds: decision.chosenLocationIds,
          partial: decision.partial,
          reason: decision.reason,
          details: decision.details,
        });

        console.log("📍 Route stored:", {
          order_id: orderId,
          chosen_location_id: decision.chosenLocationId,
          partial: decision.partial,
          reason: decision.reason,
          overLimit,
          totalUnits,
        });

        // Execute Shopify fulfillment move(s)
        if (decision.chosenLocationId && Number(decision.chosenLocationId) !== 0) {
          try {
            if (splitPlan && splitPlan.assignments.length > 1) {
              // Multi-store split fulfillment
              await executeSplitFulfillment(shopDomain, orderId, splitPlan, activeFos);
              console.log("✅ Split fulfillment complete:", {
                order_id: orderId,
                stores: splitPlan.assignments.map((a) => a.store.name),
                unassigned: splitPlan.unassigned,
              });
            } else {
              // Single-store move (full or best-partial)
              const moveResult = await moveFulfillmentOrder(shopDomain, orderId, decision.chosenLocationId);
              if (moveResult.moved) {
                console.log("✅ Fulfillment moved:", moveResult);
              } else {
                console.warn("⚠️ Fulfillment NOT moved:", moveResult.reason);
              }
            }
          } catch (e) {
            console.error("❌ Fulfillment operation failed:", e.message);
          }
        }
      }
    }
  } catch (e) {
    console.error("❌ Routing failed:", e.message);
  }

  return res.status(200).send("OK");
});




const PORT = process.env.PORT || 3000;
app.listen(PORT, () => console.log(`🚀 Server listening on port ${PORT}`));

