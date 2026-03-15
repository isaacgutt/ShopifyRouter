module.exports = {
  apps: [
    {
      name: "shopify-router",
      script: "index.js",
      env: {
        PORT: "3000",
        SHOPIFY_WEBHOOK_SECRET: process.env.SHOPIFY_WEBHOOK_SECRET,
        DATABASE_URL: process.env.DATABASE_URL,
        SHOPIFY_SHOP_DOMAIN: process.env.SHOPIFY_SHOP_DOMAIN,
        SHOPIFY_ADMIN_ACCESS_TOKEN: process.env.SHOPIFY_ADMIN_ACCESS_TOKEN
      }
    }
  ]
};

