import {
    CategoryScale,
    Chart,
    LinearScale,
    LineController,
    LineElement,
    PointElement,
    Tooltip,
} from "chart.js";
import type { ChartDataset, ChartOptions } from "chart.js";

Chart.register(
    CategoryScale,
    LinearScale,
    LineController,
    LineElement,
    PointElement,
    Tooltip,
);

export type MetricsSample = {
    id: number;
    timestamp: string;
    cpu_percent: number;
    memory_percent: number;
    disk_percent: number;
    network_sent_bytes: number;
    network_received_bytes: number;
};

type SnapshotMetricKey = "cpu_percent" | "memory_percent" | "disk_percent";
type NetworkMetricKey = "download_speed_percent" | "upload_speed_percent";
type MetricKey = SnapshotMetricKey | NetworkMetricKey;
type MetricDefinition = {
    key: MetricKey;
    label: string;
    color: string;
    format: (value: number, peak: number) => string;
};
type Scale = { minimum: number; maximum: number };

const formatBytes = (value: number): string => {
    if (value >= 1024 ** 3) return `${(value / 1024 ** 3).toFixed(1)} GB`;
    if (value >= 1024 ** 2) return `${(value / 1024 ** 2).toFixed(1)} MB`;
    if (value >= 1024) return `${(value / 1024).toFixed(1)} KB`;
    return `${value.toFixed(0)} B`;
};
const formatTime = (timestamp: string): string =>
    new Date(timestamp).toLocaleTimeString([], {
        hour: "numeric",
        minute: "2-digit",
    });
const normalize = (value: number, scale: Scale): number => {
    if (scale.minimum === scale.maximum) return 50;
    return Math.max(
        0,
        Math.min(
            100,
            ((value - scale.minimum) / (scale.maximum - scale.minimum)) * 100,
        ),
    );
};
const networkSpeed = (
    previous: MetricsSample,
    current: MetricsSample,
    counter: "network_sent_bytes" | "network_received_bytes",
): number => {
    const elapsed = Math.max(
        (Date.parse(current.timestamp) - Date.parse(previous.timestamp)) / 1000,
        0.001,
    );
    return Math.max(0, current[counter] - previous[counter]) / elapsed;
};
const isNetworkMetric = (key: MetricKey): key is NetworkMetricKey =>
    key === "download_speed_percent" || key === "upload_speed_percent";

const metrics: MetricDefinition[] = [
    {
        key: "cpu_percent",
        label: "CPU",
        color: "#6ee7b7",
        format: (value) => `${value.toFixed(1)}%`,
    },
    {
        key: "memory_percent",
        label: "Memory %",
        color: "#38bdf8",
        format: (value) => `${value.toFixed(1)}%`,
    },
    {
        key: "disk_percent",
        label: "Disk %",
        color: "#fbbf24",
        format: (value) => `${value.toFixed(1)}%`,
    },
    {
        key: "download_speed_percent",
        label: "Download speed (% peak)",
        color: "#22d3ee",
        format: (value, peak) =>
            `${formatBytes(value)}/s (${peak > 0 ? ((value / peak) * 100).toFixed(1) : "0.0"}% peak)`,
    },
    {
        key: "upload_speed_percent",
        label: "Upload speed (% peak)",
        color: "#818cf8",
        format: (value, peak) =>
            `${formatBytes(value)}/s (${peak > 0 ? ((value / peak) * 100).toFixed(1) : "0.0"}% peak)`,
    },
];

const maximumSamples = 240;

export class MetricsGraph {
    private chart: Chart<"line", number[], string> | null = null;
    private samples: MetricsSample[] = [];
    private readonly values = new Map<MetricKey, number[]>();
    private readonly scales = new Map<SnapshotMetricKey, Scale>();
    private readonly networkPeaks: Record<NetworkMetricKey, number> = {
        download_speed_percent: 0,
        upload_speed_percent: 0,
    };
    private systemKey: string | null = null;

    constructor(
        private readonly canvas: HTMLCanvasElement,
        private readonly emptyState: HTMLElement,
        private readonly summary: HTMLElement,
        private readonly toggles: NodeListOf<HTMLInputElement>,
        private readonly animateUpdates: boolean,
    ) {
        for (const toggle of toggles) {
            toggle.addEventListener("change", () => {
                const metric = metrics.find(
                    (item) => item.key === toggle.dataset.metricToggle,
                );
                const dataset = this.chart?.data.datasets.find(
                    (item) => item.label === metric?.label,
                );
                if (!dataset) return;
                dataset.hidden = !toggle.checked;
                this.chart?.update(this.animateUpdates ? "active" : "none");
            });
        }
    }

    setHistory(history: MetricsSample[], systemKey: string): void {
        if (!history.length) {
            this.reset(systemKey);
            return;
        }
        if (this.systemKey !== systemKey || !this.chart) {
            this.initialize(history, systemKey);
            return;
        }
        const lastId = this.samples.at(-1)?.id ?? -1;
        const additions = history.filter((sample) => sample.id > lastId);
        if (additions.length) this.append(additions);
    }

    appendSample(sample: MetricsSample, systemKey: string): void {
        if (this.systemKey !== systemKey || !this.chart) {
            this.setHistory([sample], systemKey);
            return;
        }
        if (sample.id > (this.samples.at(-1)?.id ?? -1)) {
            this.append([sample]);
        }
    }

    private reset(systemKey: string): void {
        this.chart?.destroy();
        this.chart = null;
        this.samples = [];
        this.values.clear();
        this.scales.clear();
        this.networkPeaks.download_speed_percent = 0;
        this.networkPeaks.upload_speed_percent = 0;
        this.systemKey = systemKey;
        this.canvas.classList.add("invisible");
        this.emptyState.classList.remove("hidden");
        this.summary.textContent = "Waiting for samples";
    }

    private initialize(history: MetricsSample[], systemKey: string): void {
        this.chart?.destroy();
        this.systemKey = systemKey;
        this.samples = history.slice(-maximumSamples);
        this.values.clear();
        this.scales.clear();
        this.networkPeaks.download_speed_percent = 0;
        this.networkPeaks.upload_speed_percent = 0;
        for (const metric of metrics) this.values.set(metric.key, []);

        this.samples.forEach((sample, index) => {
            const previous = this.samples[index - 1];
            for (const metric of metrics) {
                const value = this.valueFor(metric.key, sample, previous);
                this.values.get(metric.key)?.push(value);
                if (isNetworkMetric(metric.key))
                    this.networkPeaks[metric.key] = Math.max(
                        this.networkPeaks[metric.key],
                        value,
                    );
            }
        });
        for (const metric of metrics) {
            if (isNetworkMetric(metric.key)) continue;
            this.scales.set(
                metric.key,
                this.makeScale(this.values.get(metric.key) ?? []),
            );
        }

        this.canvas.classList.remove("invisible");
        this.emptyState.classList.add("hidden");
        this.updateSummary();
        this.chart = new Chart<"line", number[], string>(this.canvas, {
            type: "line",
            data: {
                labels: this.samples.map((sample) =>
                    this.formatTime(sample.timestamp),
                ),
                datasets: this.makeDatasets(),
            },
            options: this.options(),
        });
    }

    private append(additions: MetricsSample[]): void {
        const appended = new Map<MetricKey, number[]>(
            metrics.map((metric) => [metric.key, []]),
        );
        let redrawWithoutAnimation = false;
        for (const sample of additions) {
            const previous = this.samples.at(-1);
            for (const metric of metrics) {
                const value = this.valueFor(metric.key, sample, previous);
                this.values.get(metric.key)!.push(value);
                if (isNetworkMetric(metric.key)) {
                    if (value > this.networkPeaks[metric.key]) {
                        this.networkPeaks[metric.key] = value;
                        redrawWithoutAnimation = true;
                    }
                } else {
                    const scale = this.scales.get(metric.key)!;
                    if (value < scale.minimum || value > scale.maximum) {
                        this.scales.set(metric.key, {
                            minimum: Math.min(scale.minimum, value),
                            maximum: Math.max(scale.maximum, value),
                        });
                        redrawWithoutAnimation = true;
                    }
                }
                appended
                    .get(metric.key)
                    ?.push(this.plotValue(metric.key, value));
            }
            this.samples.push(sample);
        }

        if (this.samples.length > maximumSamples) {
            const overflow = this.samples.length - maximumSamples;
            this.samples.splice(0, overflow);
            for (const series of this.values.values())
                series.splice(0, overflow);
            redrawWithoutAnimation = true;
        }
        if (!this.chart) return;
        this.updateSummary();

        if (redrawWithoutAnimation) {
            this.chart.data.labels = this.samples.map((sample) =>
                this.formatTime(sample.timestamp),
            );
            this.chart.data.datasets = this.makeDatasets();
            this.chart.update("none");
            return;
        }
        this.chart.data.labels?.push(
            ...additions.map((sample) => this.formatTime(sample.timestamp)),
        );
        for (const metric of metrics) {
            const dataset = this.chart.data.datasets.find(
                (item) => item.label === metric.label,
            );
            dataset?.data.push(...(appended.get(metric.key) ?? []));
        }
        this.chart.update(this.animateUpdates ? "active" : "none");
    }

    private valueFor(
        key: MetricKey,
        sample: MetricsSample,
        previous: MetricsSample | undefined,
    ): number {
        if (key === "download_speed_percent")
            return previous ?
                    networkSpeed(previous, sample, "network_received_bytes")
                :   0;
        if (key === "upload_speed_percent")
            return previous ?
                    networkSpeed(previous, sample, "network_sent_bytes")
                :   0;
        return sample[key];
    }

    private plotValue(key: MetricKey, value: number): number {
        if (isNetworkMetric(key)) {
            const peak = this.networkPeaks[key];
            return peak > 0 ? (value / peak) * 100 : 0;
        }
        return this.normalize(value, this.scales.get(key)!);
    }

    private makeDatasets(): ChartDataset<"line", number[]>[] {
        return metrics.map((metric) => ({
            label: metric.label,
            data: (this.values.get(metric.key) ?? []).map((value) =>
                this.plotValue(metric.key, value),
            ),
            borderColor: metric.color,
            backgroundColor: metric.color,
            borderWidth: 2,
            pointRadius: 0,
            pointHoverRadius: 0,
            pointHitRadius: 0,
            tension: 0.38,
            fill: false,
            hidden: !Array.from(this.toggles).find(
                (toggle) => toggle.dataset.metricToggle === metric.key,
            )?.checked,
        }));
    }

    private options(): ChartOptions<"line"> {
        return {
            responsive: true,
            maintainAspectRatio: false,
            animation:
                this.animateUpdates ?
                    { duration: 560, easing: "easeOutQuart" }
                :   false,
            interaction: { mode: "index", intersect: false },
            plugins: {
                legend: { display: false },
                tooltip: {
                    backgroundColor: "#090b0a",
                    borderColor: "rgba(255,255,255,0.12)",
                    borderWidth: 1,
                    padding: 10,
                    displayColors: true,
                    callbacks: {
                        label: (context) => {
                            const metric = metrics.find(
                                (item) => item.label === context.dataset.label,
                            );
                            const value =
                                metric ?
                                    this.values.get(metric.key)?.[
                                        context.dataIndex
                                    ]
                                :   undefined;
                            return metric && value !== undefined ?
                                    metric.format(
                                        value,
                                        isNetworkMetric(metric.key) ?
                                            this.networkPeaks[metric.key]
                                        :   0,
                                    )
                                :   "";
                        },
                    },
                },
            },
            scales: {
                x: {
                    grid: { display: false },
                    border: { display: false },
                    ticks: {
                        color: "#71717a",
                        maxTicksLimit: 7,
                        maxRotation: 0,
                    },
                },
                y: {
                    min: 0,
                    max: 100,
                    border: { display: false },
                    grid: { color: "rgba(255,255,255,0.06)" },
                    ticks: { color: "#71717a", stepSize: 25 },
                },
            },
        };
    }

    private makeScale(values: number[]): Scale {
        return { minimum: Math.min(...values), maximum: Math.max(...values) };
    }

    private normalize(value: number, scale: Scale): number {
        if (scale.minimum === scale.maximum) return 50;
        return Math.max(
            0,
            Math.min(
                100,
                ((value - scale.minimum) / (scale.maximum - scale.minimum)) *
                    100,
            ),
        );
    }

    private updateSummary(): void {
        if (!this.samples.length) {
            this.summary.textContent = "Waiting for samples";
            return;
        }
        this.summary.textContent = `${this.samples.length} samples · ${this.formatTime(this.samples[0].timestamp)} to ${this.formatTime(this.samples[this.samples.length - 1].timestamp)}`;
    }

    private formatTime(timestamp: string): string {
        return new Date(timestamp).toLocaleTimeString([], {
            hour: "numeric",
            minute: "2-digit",
        });
    }
}
