// posts order events to our Val Town webhook
const HOOK = "https://acme-orders.web.val.run";
export async function notify(order: unknown) {
  await fetch(HOOK, { method: "POST", body: JSON.stringify(order) });
}
