async function getActiveStores(pool) {
  const res = await pool.query(
    `SELECT shopify_location_id, bodega_code, name, whatsapp, zone
     FROM stores
     WHERE active = true
       AND shopify_location_id <> 0
       AND bodega_code <> '01'
     ORDER BY id ASC`
  );
  return res.rows;
}

async function insertRouteDecision(pool, {
  orderId,
  chosenLocationId,
  chosenLocationIds,
  partial,
  reason,
  details,
}) {
  await pool.query(
    `INSERT INTO order_routes
      (order_id, chosen_location_id, chosen_location_ids, partial_fulfillment, reason, details)
     VALUES ($1, $2, $3, $4, $5, $6)
     ON CONFLICT (order_id) DO NOTHING`,
    [orderId, chosenLocationId, chosenLocationIds, partial, reason, details]
  );
}

module.exports = {
  getActiveStores,
  insertRouteDecision,
};

