// Dashboard 3 tabs: Polymarket | Hyperliquid | dYdX
const REFRESH_SEC = 10;

function dashboard() {
    return {
        tab: 'pm',
        loading: false,
        lastUpdate: '—',
        countdown: REFRESH_SEC,
        summary: null, liveSummary: null, liveTrades: [],
        hlSummary: null, hlTrades: [],
        dxSummary: null, dxTrades: [],
        stratStatus: null,

        async init() {
            await this.refresh();
            setInterval(() => {
                this.countdown = Math.max(0, this.countdown - 1);
                if (this.countdown === 0) {
                    this.refresh();
                    this.countdown = REFRESH_SEC;
                }
            }, 1000);
            if ('serviceWorker' in navigator) {
                navigator.serviceWorker.register('/sw.js').catch(() => {});
            }
        },

        async refresh() {
            this.loading = true;
            try {
                const [s, ls, lt, hs, ht, ds, dt, st] = await Promise.all([
                    fetch('/api/summary').then(r => r.json()).catch(() => null),
                    fetch('/api/live/summary').then(r => r.json()).catch(() => null),
                    fetch('/api/live/trades?limit=10').then(r => r.json()).catch(() => []),
                    fetch('/api/hl/summary').then(r => r.json()).catch(() => null),
                    fetch('/api/hl/trades?limit=10').then(r => r.json()).catch(() => []),
                    fetch('/api/dx/summary').then(r => r.json()).catch(() => null),
                    fetch('/api/dx/trades?limit=10').then(r => r.json()).catch(() => []),
                    fetch('/api/strategies/status').then(r => r.json()).catch(() => null),
                ]);
                this.summary = s; this.liveSummary = ls;
                this.liveTrades = Array.isArray(lt) ? lt : (lt?.trades || []);
                this.hlSummary = hs; this.hlTrades = Array.isArray(ht) ? ht : [];
                this.dxSummary = ds; this.dxTrades = Array.isArray(dt) ? dt : [];
                this.stratStatus = st;
                this.lastUpdate = new Date().toLocaleTimeString('es-AR', {
                    hour: '2-digit', minute: '2-digit', second: '2-digit'
                });
                this.countdown = REFRESH_SEC;
            } catch (e) { console.error('refresh error', e); }
            finally { this.loading = false; }
        },

        fmtUsd(n) {
            const v = Number(n || 0); const sign = v < 0 ? '-' : ''; const a = Math.abs(v);
            if (a >= 1e3) return `${sign}$${(a/1e3).toFixed(1)}k`;
            return `${sign}$${a.toFixed(2)}`;
        },
        fmtUsdSigned(n) {
            const v = Number(n || 0); const sign = v > 0 ? '+' : (v < 0 ? '−' : ''); const a = Math.abs(v);
            if (a >= 1e3) return `${sign}$${(a/1e3).toFixed(2)}k`;
            return `${sign}$${a.toFixed(2)}`;
        },
        fmtPct(n) { return `${Math.round(Number(n||0)*100)}%`; },
        shortWallet(w) { return w ? w.slice(0,6) + '…' + w.slice(-4) : ''; },
        hhmm(ts) {
            if (!ts) return '';
            const d = new Date((typeof ts === 'number' ? ts : parseInt(ts)) * 1000);
            return d.toLocaleTimeString('es-AR', { hour: '2-digit', minute: '2-digit' });
        },

        get modeLabel() {
            const m = this.liveSummary?.mode;
            if (m === 'live_real') return { txt: 'LIVE REAL', cls: 'text-rose-300 bg-rose-950/50 border-rose-800/60' };
            if (m === 'live_dry')  return { txt: 'DRY-RUN',  cls: 'text-amber-300 bg-amber-950/40 border-amber-800/50' };
            return { txt: 'OFF', cls: 'text-slate-400 bg-slate-900 border-slate-700' };
        },
        get killActive() { return !!this.summary?.kill_switch?.active; },

        get stratList() {
            const s = this.stratStatus || {};
            const empty = { open: 0, pnl_24h: 0, pnl_total: 0, last_at: null, enabled: false };
            return [
                { key: 'n1_copybot',  label: 'N1 Copy-bot',   data: s.n1_copybot   || empty },
                { key: 'crypto_arb',  label: 'Crypto-arb N2', data: s.crypto_arb   || empty },
                { key: 'market_maker',label: 'Market Maker',  data: s.market_maker || empty },
                { key: 'spike_arb',   label: 'Spike Arb',     data: s.spike_arb    || empty },
                { key: 'adversarial', label: 'Adversarial',   data: s.adversarial  || empty },
                { key: 'long_horizon',label: 'Long Horizon',  data: s.long_horizon || empty },
                { key: 'hedge',       label: 'Hedge (perp)',  data: s.hedge        || empty },
            ];
        },

        get tabData() {
            if (this.tab === 'pm') return {
                title: 'Polymarket',
                badge: this.modeLabel,
                pnl24h: Number(this.liveSummary?.today?.pnl_usdc ?? 0),
                pnlTotal: Number(this.liveSummary?.totals?.pnl_usdc ?? 0),
                wins24h: this.liveSummary?.today?.wins ?? 0,
                losses24h: this.liveSummary?.today?.losses ?? 0,
                roi: (this.liveSummary?.totals?.roi_pct ?? 0).toFixed(1) + '%',
                wr: this.liveSummary?.totals?.win_rate ?? 0,
                openCount: this.liveSummary?.open?.n ?? 0,
                walletsActive: this.summary?.copying?.active ?? 0,
                walletsDropped: this.summary?.copying?.dropped ?? 0,
                trades: this.liveTrades,
                topWallets: this.liveSummary?.top_wallets || [],
                config: this.liveSummary?.config,
                tradeKey: 'condition_id',
            };
            if (this.tab === 'hl') return {
                title: 'Hyperliquid',
                badge: { txt: 'DRY-RUN', cls: 'text-blue-300 bg-blue-950/40 border-blue-800/50' },
                pnl24h: Number(this.hlSummary?.today?.pnl_usdc ?? 0),
                pnlTotal: Number(this.hlSummary?.totals?.pnl_usdc ?? 0),
                wins24h: this.hlSummary?.today?.wins ?? 0,
                losses24h: this.hlSummary?.today?.losses ?? 0,
                roi: '—',
                wr: this.hlSummary?.totals?.win_rate ?? 0,
                openCount: this.hlSummary?.open?.n ?? 0,
                walletsActive: this.hlSummary?.wallets?.active ?? 0,
                walletsDropped: this.hlSummary?.wallets?.dropped ?? 0,
                trades: this.hlTrades,
                topWallets: this.hlSummary?.top_wallets || [],
                config: this.hlSummary?.config,
                tradeKey: 'coin',
            };
            return {
                title: 'dYdX v4',
                badge: { txt: 'DRY-RUN', cls: 'text-purple-300 bg-purple-950/40 border-purple-800/50' },
                pnl24h: Number(this.dxSummary?.today?.pnl_usdc ?? 0),
                pnlTotal: Number(this.dxSummary?.totals?.pnl_usdc ?? 0),
                wins24h: this.dxSummary?.today?.wins ?? 0,
                losses24h: this.dxSummary?.today?.losses ?? 0,
                roi: '—',
                wr: this.dxSummary?.totals?.win_rate ?? 0,
                openCount: this.dxSummary?.open?.n ?? 0,
                walletsActive: this.dxSummary?.wallets?.active ?? 0,
                walletsDropped: this.dxSummary?.wallets?.dropped ?? 0,
                trades: this.dxTrades,
                topWallets: this.dxSummary?.top_wallets || [],
                config: this.dxSummary?.config,
                tradeKey: 'ticker',
            };
        },
    };
}
