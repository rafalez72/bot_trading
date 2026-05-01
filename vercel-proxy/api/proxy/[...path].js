// Vercel Edge proxy — generic forwarder a clob.polymarket.com
//
// Reescribe la URL: /api/proxy/<path>?<query> → https://clob.polymarket.com/<path>?<query>
// Forwardea TODOS los headers POLY_* que vengan en la request.
//
// Útil para: /auth/api-key (POST/GET), /balance-allowance (GET), etc.

export const config = { runtime: 'edge' };

const TARGET_HOST = 'https://clob.polymarket.com';

export default async function handler(req) {
    const url = new URL(req.url);
    // Strip /api/proxy prefix → keep path + query
    const path = url.pathname.replace(/^\/api\/proxy/, '') || '/';
    const targetUrl = TARGET_HOST + path + url.search;

    const headers = new Headers();
    for (const [k, v] of req.headers) {
        if (k.toUpperCase().startsWith('POLY_')) {
            headers.set(k, v);
        }
    }
    headers.set('Accept', 'application/json');
    headers.set('User-Agent', 'Mozilla/5.0 (compatible; PMProxy/1.0)');

    const init = {
        method: req.method,
        headers,
    };
    if (req.method !== 'GET' && req.method !== 'HEAD') {
        init.body = await req.text();
    }

    try {
        const response = await fetch(targetUrl, init);
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
