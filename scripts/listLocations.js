require("dotenv").config({ path: require("path").resolve(__dirname, "../.env") });
const fetch = require("node-fetch");

const SHOP = process.env.SHOPIFY_SHOP_DOMAIN; // e.g. bhjoyeria.myshopify.com
const TOKEN = process.env.SHOPIFY_ADMIN_ACCESS_TOKEN;

async function run() {
  if (!SHOP) throw new Error("Missing SHOPIFY_SHOP_DOMAIN");
  if (!TOKEN) throw new Error("Missing SHOPIFY_ADMIN_ACCESS_TOKEN");

  const url = `https://${SHOP}/admin/api/2024-10/graphql.json`;
  console.log("Requesting:", url);

  const query = `
    query {
      locations(first: 50) {
        edges {
          node {
            id
            name
            isActive
          }
        }
      }
    }
  `;

  const res = await fetch(url, {
    method: "POST",
    headers: {
      "X-Shopify-Access-Token": TOKEN,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ query }),
  });

  const text = await res.text();
  console.log("HTTP", res.status, res.statusText);
  // If it's an error, show body and stop
  if (!res.ok) {
    console.log("Body:", text);
    process.exit(1);
  }

  const json = JSON.parse(text);

  if (json.errors) {
    console.log("GraphQL errors:", JSON.stringify(json.errors, null, 2));
    process.exit(1);
  }

  const edges = json.data?.locations?.edges || [];
  edges.forEach(({ node }) => {
    const numericId = node.id.split("/").pop(); // gid://shopify/Location/123 -> 123
    console.log(`${numericId} | ${node.name} | active=${node.isActive}`);
  });
}

run().catch((e) => {
  console.error("ERROR:", e.message);
  process.exit(1);
});

