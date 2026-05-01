// Endpoint dedicado /api/balance — forwardea GET /balance-allowance del CLOB.
//
// Acepta los headers L2 (POLY_ADDRESS, POLY_SIGNATURE, POLY_TIMESTAMP,
// POLY_API_KEY, POLY_PASSPHRASE) y los query params (?asset_type=COLLATERAL).

export const config = { runtime: 'edge' };

const TARGET = 'https://clob.polymarket.com/balance-allowance';

export default async function handler(req) {
    const url = new URL(req.url);
    const targetUrl = TARGET + url.search;

    const headers = new Headers();
    for (const [k, v] of req.headers) {
        if (k.toUpperCase().startsWith('POLY_')) {
            headers.set(k, v);
        }
    }
    headers.set('Accept', 'application/json');
    headers.set('User-Agent', 'Mozilla/5.0 (compatible; PMProxy/1.0)');

    try {
        const response = await fetch(targetUrl, { method: 'GET', headers });
        const body = await response.text();
        return new Response(body, {
            status: response.status,
            headers: {
                'Content-Type': response.headers.get('Content-Type') || 'application/json',
                'Access-Control-Allow-Origin': '*',
            },
        });
    } catch (e) {
        return new Response(JSON.stringify({ error: String(e), target: targetUrl }), {
            status: 500,
            headers: { 'Content-Type': 'application/json' },
        });
    }
}
