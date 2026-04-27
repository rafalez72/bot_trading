// Estado y lógica del dashboard. Alpine.js component.
const REFRESH_SEC = 10;

function dashboard() {
    return {
        tab: 'home',
        loading: false,
        lastUpdate: '—',
        countdown: REFRESH_SEC,
        summary: null,
        copying: [],
        topTraders: [],
        markets: [],
        learning: [],
        chartBucket: 'hour',
        chart: null,
        hasChartData: false,

        async init() {
            await this.refresh();
            await this.loadChart();
            // Tick del contador (cada segundo)
            setInterval(() => {
                this.countdown = Math.max(0, this.countdown - 1);
                if (this.countdown === 0) {
                    this.refresh();
                    this.loadChart();
                    this.countdown = REFRESH_SEC;
                }
            }, 1000);
            if ('serviceWorker' in navigator) {
                navigator.serviceWorker.register('/sw.js').catch(() => {});
            }
        },

        async loadChart() {
            try {
                const data = await fetch(`/api/pnl-timeline?bucket=${this.chartBucket}`).then(r => r.json());
                this.hasChartData = (data.points || []).length > 0;
                this.renderChart(data.points || []);
            } catch (e) { console.error('chart error', e); }
        },

        renderChart(points) {
            const ctx = document.getElementById('pnlChart');
            if (!ctx) return;
            const labels = points.map(p => {
                const d = new Date(p.t * 1000);
                return this.chartBucket === 'hour'
                    ? d.toLocaleTimeString('es-AR', { hour: '2-digit', minute: '2-digit' })
                    : d.toLocaleDateString('es-AR', { day: '2-digit', month: '2-digit' });
            });
            const data = points.map(p => p.cum_pnl);

            if (this.chart) this.chart.destroy();
            this.chart = new Chart(ctx, {
                type: 'line',
                data: {
                    labels,
                    datasets: [{
                        data,
                        borderColor: '#34d399',
                        backgroundColor: 'rgba(52,211,153,0.12)',
                        fill: true,
                        tension: 0.25,
                        borderWidth: 2,
                        pointRadius: 0,
                        pointHoverRadius: 4,
                    }],
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: { legend: { display: false }, tooltip: {
                        callbacks: { label: (c) => '$' + (c.parsed.y).toFixed(2) }
                    } },
                    scales: {
                        x: { ticks: { color: '#94a3b8', maxTicksLimit: 6, font: { size: 10 } }, grid: { display: false } },
                        y: { ticks: { color: '#94a3b8', font: { size: 10 }, callback: (v) => '$' + v.toFixed(0) }, grid: { color: 'rgba(148,163,184,0.08)' } },
                    },
                },
            });
        },

        async refresh() {
            this.loading = true;
            try {
                const [s, c, t, m, l] = await Promise.all([
                    fetch('/api/summary').then(r => r.json()),
                    fetch('/api/copying').then(r => r.json()),
                    fetch('/api/traders/top?limit=50').then(r => r.json()),
                    fetch('/api/markets/active?limit=20').then(r => r.json()),
                    fetch('/api/learning/events?limit=50').then(r => r.json()),
                ]);
                this.summary = s;
                this.copying = c;
                this.topTraders = t;
                this.markets = m;
                this.learning = l;
                this.lastUpdate = new Date().toLocaleTimeString('es-AR', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
                this.countdown = REFRESH_SEC;
            } catch (e) {
                console.error('refresh error', e);
            } finally {
                this.loading = false;
            }
        },

        async runSelect() {
            this.loading = true;
            try {
                const r = await fetch('/api/select?top=10', { method: 'POST' }).then(r => r.json());
                console.log('select result', r);
                await this.refresh();
            } catch (e) { console.error(e); }
            finally { this.loading = false; }
        },

        // Utilidades de formateo
        fmtUsd(n) {
            const v = Number(n || 0);
            const a = Math.abs(v);
            const sign = v < 0 ? '-' : '';
            if (a >= 1e6) return `${sign}$${(a/1e6).toFixed(2)}M`;
            if (a >= 1e3) return `${sign}$${(a/1e3).toFixed(1)}k`;
            if (a >= 100)  return `${sign}$${a.toFixed(0)}`;
            return `${sign}$${a.toFixed(2)}`;
        },
        fmtUsdSigned(n) {
            const v = Number(n || 0);
            const sign = v > 0 ? '+' : (v < 0 ? '−' : '');
            const a = Math.abs(v);
            if (a >= 1e3) return `${sign}$${(a/1e3).toFixed(2)}k`;
            return `${sign}$${a.toFixed(2)}`;
        },
        fmtPct(n) { return `${(Number(n||0)*100).toFixed(0)}%`; },
        fmtPctSigned(n) {
            const v = Number(n || 0);
            const sign = v > 0 ? '+' : (v < 0 ? '' : '');
            return `${sign}${v.toFixed(1)}%`;
        },
        shortWallet(w) { return w ? w.slice(0,6) + '…' + w.slice(-4) : ''; },
        timeAgo(s) {
            if (!s) return '';
            // SQLite devuelve "YYYY-MM-DD HH:MM:SS" en UTC
            const d = new Date(s.replace(' ', 'T') + 'Z');
            const sec = Math.max(1, Math.floor((Date.now() - d.getTime()) / 1000));
            if (sec < 60)    return `hace ${sec}s`;
            if (sec < 3600)  return `hace ${Math.floor(sec/60)}m`;
            if (sec < 86400) return `hace ${Math.floor(sec/3600)}h`;
            return `hace ${Math.floor(sec/86400)}d`;
        },

        // Iconos y mensajes humanos para los eventos del bot
        evIcon(t) {
            switch (t) {
                case 'size_up':   return '📈';
                case 'size_down': return '📉';
                case 'drop':      return '⛔';
                case 'promote':   return '✅';
                default:          return '⚙️';
            }
        },
        evMessage(ev) {
            const after = Number(ev.after_value ?? 0).toFixed(2);
            const before = Number(ev.before_value ?? 0).toFixed(2);
            switch (ev.event_type) {
                case 'size_up':
                    return `Subió la apuesta para este trader · ${before}× → ${after}×`;
                case 'size_down':
                    return `Bajó la apuesta para este trader · ${before}× → ${after}×`;
                case 'drop':
                    return 'Dejó de copiar a este trader';
                case 'promote':
                    return 'Volvió a copiar a este trader';
                case 'score_update':
                    return 'Actualizó el score de este trader';
                default:
                    return ev.event_type;
            }
        },
        evDetail(ev) {
            const trig = ev.trigger || '';
            // Reescritura de triggers técnicos a español plano
            // "paper_trade #123 WIN (PnL $0.42)" → "Ganó $0.42 en su última copia"
            const winMatch  = trig.match(/WIN \(PnL \$([\-\d.]+)\)/);
            const lossMatch = trig.match(/LOSS \(PnL \$([\-\d.]+)\)/);
            if (winMatch)  return `El trader ganó $${Math.abs(parseFloat(winMatch[1])).toFixed(2)} en la última copia`;
            if (lossMatch) return `El trader perdió $${Math.abs(parseFloat(lossMatch[1])).toFixed(2)} en la última copia`;
            if (ev.event_type === 'drop' && /pérdidas/i.test(trig)) return '5 pérdidas seguidas — protección automática';
            return trig;
        },
    };
}
