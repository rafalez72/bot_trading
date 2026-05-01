// Vercel Edge proxy genérico → clob.polymarket.com
//
// Lee el path target de query param `p`. Forwardea método, headers POLY_*,
// query params (excepto p), y body. Routing: vercel.json reescribe
// /clob/<anything> → /api/clob?p=<anything>.
//
// Bot usa host `https://...vercel.app/clob` → py-clob-client genera URLs
// como `/clob/auth/api-key`, `/clob/balance-allowance?asset_type=COLLATERAL`,
// etc. El rewrite las captura todas y las pasa a este handler.

export const config = { runtime: 'edge' };

const TARGET_HOST = 'https://clob.polymarket.com';

export default async function handler(req) {
    const url = new URL(req.url);
    const path = url.searchParams.get('p') || '';

    // Construir query string sin el param 'p'
    const params = new URLSearchParams();
    for (const [k, v] of url.searchParams) {
        if (k !== 'p') params.append(k, v);
    }
    const qs = params.toString();
    const targetUrl = TARGET_HOST + '/' + path + (qs ? '?' + qs : '');

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
        const body = await req.text();
        if (body) {
            init.body = body;
            const ct = req.headers.get('content-type');
            if (ct) headers.set('Content-Type', ct);
        }
    }

    try {
        const response = await fetch(targetUrl, init);
        const body = await response.text();
        return new Response(body, {
            status: response.status,
            headers: {
                'Content-Type': response.headers.get('Content-Type') || 'application/json',
                'Access-Control-Allow-Origin': '*',
                'X-Proxy-Target': targetUrl.substring(0, 100),
            },
        });
    } catch (e) {
        return new Response(JSON.stringify({ error: String(e), target: targetUrl }), {
            status: 500,
            headers: { 'Content-Type': 'application/json' },
        });
    }
}
