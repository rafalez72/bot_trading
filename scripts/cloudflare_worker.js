/**
 * Cloudflare Worker — proxy para Polymarket /auth/api-key.
 *
 * Por qué: Polymarket tiene WAF que bloquea AR + datacenters conocidos
 * (Azure, AWS, M247, Cloudflare WARP). Pero Cloudflare Workers corren
 * dentro de la propia red de Cloudflare → polymarket.com está también
 * en Cloudflare → la request es intra-CF y no aplica el bloqueo regional.
 *
 * Flujo:
 *  1. Cliente computa los headers POLY_* localmente con su private key
 *  2. POST a este Worker con esos headers
 *  3. Worker forwardea a clob.polymarket.com manteniendo los headers
 *  4. Devuelve la respuesta tal cual
 *
 * El Worker NUNCA ve la private key — solo ve la firma ya construida.
 *
 * Deploy en https://dash.cloudflare.com → Workers & Pages → Create Worker
 * Copiar y pegar este código → Save and Deploy.
 */

const TARGET = "https://clob.polymarket.com/auth/api-key";

export default {
    async fetch(request) {
        // Forward POST y GET (create vs derive)
        const headers = new Headers();
        for (const [k, v] of request.headers) {
            const upper = k.toUpperCase();
            if (upper.startsWith("POLY_")) {
                headers.set(k, v);
            }
        }
        // Acept: para que Polymarket devuelva JSON
        headers.set("Accept", "application/json");
        // User-Agent neutral (algunos UAs son blockeados específicamente)
        headers.set("User-Agent", "Mozilla/5.0 (compatible; PMProxy/1.0)");

        const init = {
            method: request.method,
            headers,
        };

        try {
            const response = await fetch(TARGET, init);
            const body = await response.text();
            return new Response(body, {
                status: response.status,
                headers: {
                    "Content-Type": response.headers.get("Content-Type") || "application/json",
                    "Access-Control-Allow-Origin": "*",
                },
            });
        } catch (e) {
            return new Response(JSON.stringify({ error: String(e) }), {
                status: 500,
                headers: { "Content-Type": "application/json" },
            });
        }
    },
};
