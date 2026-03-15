require("dotenv").config({ path: require("path").resolve(__dirname, "../.env") });
const fetch = require("node-fetch");

const SHOP = "zaw46p-46.myshopify.com";
const TOKEN = process.env.SHOPIFY_ADMIN_ACCESS_TOKEN;
const API_VERSION = process.env.SHOPIFY_ADMIN_API_VERSION || "2024-10";

const LOCATION_IDS = [
  111090860399, // AC
  111090729327, // 04
  111090893167, // 09
  111091122543, // 06
  111091155311, // 07
];

const VARIANT_ID = Number(process.argv[2]);

async function get(path) {
  const url = `https://${SHOP}/admin/api/${API_VERSION}/${path}`;
  const res = await fetch(url, { headers: { "X-Shopify-Access-Token": TOKEN } });
  const text = await res.text();
  let json;
  try { json = JSON.parse(text); } catch { json = { raw: text }; }
  return { status: res.status, url, json };
}

(async () => {
  if (!VARIANT_ID) {
    console.log("Usage: node scripts/debugInventory.js <VARIANT_ID>");
    process.exit(1);
  }

  console.log("VARIANT_ID:", VARIANT_ID);

  const v = await get(`variants.json?ids=${VARIANT_ID}`);
  console.log("\nvariants.json:", v.status);
  console.log(JSON.stringify(v.json, null, 2));

  const variant = (v.json.variants || [])[0];
  if (!variant) {
    console.log("\n❌ No variant returned. That would explain everything.");
    process.exit(1);
  }

  const invItemId = variant.inventory_item_id;
  console.log("\ninventory_item_id:", invItemId);
  console.log("inventory_management:", variant.inventory_management);

  const locParam = LOCATION_IDS.join(",");

  const filtered = await get(`inventory_levels.json?inventory_item_ids=${invItemId}&location_ids=${locParam}`);
  console.log("\nlevels (filtered):", filtered.status);
  console.log(JSON.stringify(filtered.json, null, 2));

  const unfiltered = await get(`inventory_levels.json?inventory_item_ids=${invItemId}`);
  console.log("\nlevels (unfiltered):", unfiltered.status);
  console.log(JSON.stringify(unfiltered.json, null, 2));

  console.log("\nSUMMARY (unfiltered):");
  (unfiltered.json.inventory_levels || []).forEach(lvl => {
    console.log(`location_id=${lvl.location_id} available=${lvl.available}`);
  });
})();

