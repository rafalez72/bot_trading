// Vercel Edge Function — proxy para Polymarket /auth/api-key
//
// Por qué Vercel: pool de IPs distinto a Cloudflare Workers / Azure /
// ProtonVPN free. Polymarket bloquea CF Workers pero (aún) no Vercel Edge.
//
// Mismo flujo que el CF Worker:
//   1. Cliente computa los headers POLY_* localmente con su PK
//   2. POST a este endpoint
//   3. Edge Function forwardea a clob.polymarket.com
//   4. Devuelve respuesta tal cual
//
// La PK NUNCA toca este servidor — solo la firma ya construida.

export const config = { runtime: 'edge' };

const TARGET = 'https://clob.polymarket.com/auth/api-key';

export default async function handler(req) {
    const headers = new Headers();
    for (const [k, v] of req.headers) {
        if (k.toUpperCase().startsWith('POLY_')) {
            headers.set(k, v);
        }
    }
    headers.set('Accept', 'application/json');
    headers.set('User-Agent', 'Mozilla/5.0 (compatible; PMProxy/1.0)');

    try {
        const response = await fetch(TARGET, {
            method: req.method,
            headers,
        });
        const body = await response.text();
        return new Response(body, {
            status: response.status,
            headers: {
                'Content-Type': response.headers.get('Content-Type') || 'application/json',
                'Access-Control-Allow-Origin': '*',
            },
        });
    } catch (e) {
        return new Response(JSON.stringify({ error: String(e) }), {
            status: 500,
            headers: { 'Content-Type': 'application/json' },
        });
    }
}
