// Dashboard simple: una sola vista Live, refresh cada 10s.
const REFRESH_SEC = 10;

function dashboard() {
    return {
        loading: false,
        lastUpdate: '—',
        countdown: REFRESH_SEC,
        summary: null,        // /api/summary (para wallets activos)
        liveSummary: null,    // /api/live/summary (mode, totales, top, open)
        liveTrades: [],       // /api/live/trades?limit=10

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
                const [s, ls, lt] = await Promise.all([
                    fetch('/api/summary').then(r => r.json()).catch(() => null),
                    fetch('/api/live/summary').then(r => r.json()).catch(() => null),
                    fetch('/api/live/trades?limit=10').then(r => r.json()).catch(() => []),
                ]);
                this.summary = s;
                this.liveSummary = ls;
                this.liveTrades = Array.isArray(lt) ? lt : (lt?.trades || []);
                this.lastUpdate = new Date().toLocaleTimeString('es-AR', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
                this.countdown = REFRESH_SEC;
            } catch (e) {
                console.error('refresh error', e);
            } finally {
                this.loading = false;
            }
        },

        // Helpers de formato
        fmtUsd(n) {
            const v = Number(n || 0);
            const sign = v < 0 ? '-' : '';
            const a = Math.abs(v);
            if (a >= 1e3) return `${sign}$${(a/1e3).toFixed(1)}k`;
            return `${sign}$${a.toFixed(2)}`;
        },
        fmtUsdSigned(n) {
            const v = Number(n || 0);
            const sign = v > 0 ? '+' : (v < 0 ? '−' : '');
            const a = Math.abs(v);
            if (a >= 1e3) return `${sign}$${(a/1e3).toFixed(2)}k`;
            return `${sign}$${a.toFixed(2)}`;
        },
        fmtPct(n) { return `${Math.round(Number(n||0)*100)}%`; },
        shortWallet(w) { return w ? w.slice(0,6) + '…' + w.slice(-4) : ''; },
        hhmm(ts) {
            if (!ts) return '';
            const d = new Date(ts * 1000);
            return d.toLocaleTimeString('es-AR', { hour: '2-digit', minute: '2-digit' });
        },

        // Computeds
        get modeLabel() {
            const m = this.liveSummary?.mode;
            if (m === 'live_real') return { txt: 'LIVE REAL', cls: 'text-rose-300 bg-rose-950/50 border-rose-800/60' };
            if (m === 'live_dry')  return { txt: 'DRY-RUN',  cls: 'text-amber-300 bg-amber-950/40 border-amber-800/50' };
            return { txt: 'OFF',     cls: 'text-slate-400 bg-slate-900 border-slate-700' };
        },
        get killActive() { return !!this.summary?.kill_switch?.active; },
        get pnl24h() { return Number(this.liveSummary?.today?.pnl_usdc ?? 0); },
        get pnlTotal() { return Number(this.liveSummary?.totals?.pnl_usdc ?? 0); },
        get winRate() { return this.liveSummary?.totals?.win_rate ?? 0; },
        get openCount() { return this.liveSummary?.open?.n ?? 0; },
        get walletsActive() { return this.summary?.copying?.active ?? 0; },
        get walletsDropped() { return this.summary?.copying?.dropped ?? 0; },
    };
}
