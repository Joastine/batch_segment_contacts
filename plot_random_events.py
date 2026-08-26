#!/usr/bin/env python3
"""从合并事件数据中抽样，并生成包含 20 通道曲线的交互式审核页面。

生成的 HTML 将样本数据和绘图代码全部内嵌，因此无需启动服务器，也不依赖
外部 JavaScript 库；直接用浏览器打开即可审核。
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import random
from pathlib import Path


def parse_args() -> argparse.Namespace:
    """解析随机抽样、批次/坐标筛选和输出文件参数。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "events_csv",
        type=Path,
        nargs="?",
        default=Path("all_batches_events_21x31/combined/all_events.csv"),
        help="combined events CSV (default: all_batches_events_21x31/combined/all_events.csv)",
    )
    parser.add_argument("--count", type=int, default=6)
    parser.add_argument("--seed", type=int, default=20260813)
    parser.add_argument(
        "--batches",
        type=int,
        nargs="+",
        help="only sample these batch IDs, for example: --batches 1 3 8",
    )
    parser.add_argument("--x", type=float, help="only sample this x-coordinate (mm)")
    parser.add_argument("--y", type=float, help="only sample this y-coordinate (mm)")
    parser.add_argument(
        "--sample-ids",
        nargs="+",
        help="review exact sample IDs instead of random sampling",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("random_event_review_21x31.html"),
    )
    args = parser.parse_args()
    if args.count <= 0:
        parser.error("--count must be positive")
    return args


def load_sample(
    path: Path,
    count: int,
    seed: int,
    batches: list[int] | None = None,
    x_filter: float | None = None,
    y_filter: float | None = None,
    requested_sample_ids: list[str] | None = None,
) -> tuple[list[str], list[dict[str, object]], int]:
    """读取 all_events.csv，筛选并返回待审核的完整事件。

    CSV 中同一个 sample_id 对应的所有采样行会被组合为一个事件。指定
    sample_ids 时保持用户给定顺序，否则使用固定随机种子进行可重复抽样。
    """

    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError("events CSV has no header")
        sensor_prefixes = ("TL_", "TR_", "BL_", "BR_", "C_")
        channels = [
            name for name in reader.fieldnames if name.startswith(sensor_prefixes)
        ]
        has_batch_id = "batch_id" in reader.fieldnames
        has_sample_id = "sample_id" in reader.fieldnames
        # 先完成批次和坐标过滤，再按 sample_id 收集完整时间序列。
        grouped: dict[str, list[dict[str, str]]] = {}
        for row in reader:
            batch_id = int(row.get("batch_id") or 0)
            event_id = int(row["event_id"])
            x_mm = float(row["label_x_mm"])
            y_mm = float(row["label_y_mm"])
            if batches is not None and batch_id not in batches:
                continue
            if x_filter is not None and abs(x_mm - x_filter) > 1e-9:
                continue
            if y_filter is not None and abs(y_mm - y_filter) > 1e-9:
                continue
            sample_id = (
                row["sample_id"]
                if has_sample_id and row["sample_id"]
                else f"batch_{batch_id:02d}_event_{event_id:04d}"
            )
            grouped.setdefault(sample_id, []).append(row)

    available_count = len(grouped)
    if requested_sample_ids:
        # 精确审核模式：不执行随机抽样，并检查每个 ID 都存在。
        missing = [sample_id for sample_id in requested_sample_ids if sample_id not in grouped]
        if missing:
            raise ValueError("sample IDs not found after filtering: " + ", ".join(missing))
        chosen = requested_sample_ids
    else:
        # 随机审核模式：相同 seed、筛选条件和输入数据会得到相同事件集合。
        if count > available_count:
            raise ValueError(
                f"requested {count} events, but only {available_count} match the filters"
            )
        chosen = sorted(random.Random(seed).sample(list(grouped), count))
    # 仅保留浏览器绘图需要的字段，以减小生成 HTML 的体积。
    events: list[dict[str, object]] = []
    for sample_id in chosen:
        rows = grouped[sample_id]
        first = rows[0]
        event_id = int(first["event_id"])
        batch_id = int(first.get("batch_id") or 0)
        x_mm = float(first["label_x_mm"])
        y_mm = float(first["label_y_mm"])
        events.append(
            {
                "id": event_id,
                "batch": batch_id,
                "sampleId": sample_id,
                "x": x_mm,
                "y": y_mm,
                "passed": rows[0]["threshold_passed"] == "1",
                "t": [round(float(row["relative_time"]), 6) for row in rows],
                "values": {
                    channel: [round(float(row[channel]), 4) for row in rows]
                    for channel in channels
                },
            }
        )
    return channels, events, available_count


def create_html(
    channels: list[str], events: list[dict[str, object]], seed: int, available_count: int
) -> str:
    """把事件数据序列化到一个自包含的交互式 HTML 字符串中。"""

    # 使用紧凑 JSON；ensure_ascii=False 可让页面中的中文保持可读。
    payload = json.dumps(events, ensure_ascii=False, separators=(",", ":"))
    channel_payload = json.dumps(channels, ensure_ascii=False, separators=(",", ":"))
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>随机接触事件检查</title>
<style>
  :root {{
    color-scheme: light dark;
    --background: #ffffff;
    --foreground: #172033;
    --muted-foreground: #667085;
    --border: #cfd6e4;
    --popover: #ffffff;
    --popover-foreground: #172033;
    --primary: #235ecf;
    --primary-foreground: #ffffff;
    --destructive: #d92d20;
    --viz-series-1: #235ecf;
    --viz-series-2: #667085;
    --viz-series-3: #079455;
    --viz-series-4: #dc6803;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    padding: 20px;
    background: var(--background);
    color: var(--foreground);
    font-family: system-ui, -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
  }}
  .text-small {{ font-size: 13px; }}
  .text-muted {{ color: var(--muted-foreground); }}
  .viz-row, .viz-controls {{ display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }}
  .btn {{
    appearance: none;
    padding: 7px 10px;
    border: 1px solid var(--border);
    border-radius: 6px;
    background: var(--background);
    color: var(--foreground);
    cursor: pointer;
    font: inherit;
  }}
  .btn[aria-pressed="true"] {{
    border-color: var(--primary);
    background: var(--primary);
    color: var(--primary-foreground);
  }}
  .tooltip {{
    position: absolute;
    z-index: 20;
    padding: 7px 9px;
    border: 1px solid var(--border);
    border-radius: 6px;
    background: var(--popover);
    color: var(--popover-foreground);
    box-shadow: 0 4px 14px rgb(0 0 0 / 16%);
    font-size: 13px;
    pointer-events: none;
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      --background: #101828;
      --foreground: #f2f4f7;
      --muted-foreground: #b6c0d0;
      --border: #475467;
      --popover: #1d2939;
      --popover-foreground: #f2f4f7;
      --primary: #84adff;
      --primary-foreground: #102a56;
      --destructive: #f97066;
      --viz-series-1: #84adff;
      --viz-series-2: #98a2b3;
      --viz-series-3: #47cd89;
      --viz-series-4: #fdb022;
    }}
  }}
</style>
</head>
<body>
<section id="contact-event-review">
  <h2>随机接触事件检查</h2>
  <div class="viz-row text-small text-muted" id="cer-meta">候选事件 {available_count} 个 · 随机种子 {seed} · 实线为原始通道数据</div>
  <div class="viz-controls" id="cer-event-buttons" aria-label="选择事件"></div>
  <div class="cer-phase-key text-small" aria-label="阶段说明">
    <span><i class="cer-phase cer-baseline"></i>baseline −2–0 s</span>
    <span><i class="cer-phase cer-contact"></i>contact 0–1 s</span>
    <span><i class="cer-phase cer-separation"></i>separation 1–3 s</span>
    <span><i class="cer-onset"></i>检测接触点 t=0</span>
  </div>
  <div id="cer-heading" class="cer-heading" aria-live="polite"></div>
  <div id="cer-plots" class="cer-plots"></div>
  <div id="cer-tooltip" class="tooltip" role="tooltip" hidden></div>
</section>

<style>
  #contact-event-review {{ position: relative; width: 100%; max-width: 1800px; margin: 0 auto; color: var(--foreground); }}
  #contact-event-review h2 {{ margin-bottom: 0.25rem; }}
  #contact-event-review .cer-phase-key {{ display: flex; flex-wrap: wrap; gap: 0.45rem 1rem; margin: 0.65rem 0; color: var(--muted-foreground); }}
  #contact-event-review .cer-phase-key span {{ display: inline-flex; align-items: center; gap: 0.3rem; }}
  #contact-event-review .cer-phase {{ width: 1rem; height: 0.65rem; display: inline-block; border: 1px solid var(--border); }}
  #contact-event-review .cer-baseline {{ background: color-mix(in srgb, var(--viz-series-2) 14%, transparent); }}
  #contact-event-review .cer-contact {{ background: color-mix(in srgb, var(--viz-series-3) 14%, transparent); }}
  #contact-event-review .cer-separation {{ background: color-mix(in srgb, var(--viz-series-4) 14%, transparent); }}
  #contact-event-review .cer-onset {{ width: 0; height: 0.8rem; display: inline-block; border-left: 2px solid var(--destructive); }}
  #contact-event-review .cer-heading {{ margin: 0.55rem 0 0.35rem; font-weight: 500; }}
  #contact-event-review .cer-plots {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 0.55rem 0.7rem; }}
  #contact-event-review .cer-plot {{ min-width: 0; }}
  #contact-event-review .cer-plot svg {{ display: block; width: 100%; height: 170px; overflow: visible; }}
  #contact-event-review .cer-frame {{ fill: none; stroke: var(--border); stroke-width: 1; }}
  #contact-event-review .cer-grid {{ stroke: var(--border); stroke-width: 1; opacity: 0.65; }}
  #contact-event-review .cer-line {{ fill: none; stroke: var(--viz-series-1); stroke-width: 1.5; vector-effect: non-scaling-stroke; }}
  #contact-event-review .cer-onset-line {{ stroke: var(--destructive); stroke-width: 1.5; }}
  #contact-event-review .cer-separation-line {{ stroke: var(--foreground); stroke-width: 1; stroke-dasharray: 3 3; opacity: 0.65; }}
  #contact-event-review .cer-hover-guide {{ stroke: var(--foreground); stroke-width: 1; pointer-events: none; }}
  #contact-event-review .cer-hover-point {{ fill: var(--viz-series-1); stroke: var(--background); stroke-width: 1.5; pointer-events: none; }}
  #contact-event-review svg text {{ fill: var(--foreground); font-size: 12px; }}
  #contact-event-review svg .cer-tick {{ fill: var(--muted-foreground); }}
  #contact-event-review .cer-tooltip-row {{ white-space: nowrap; }}
  @media (max-width: 820px) {{ #contact-event-review .cer-plots {{ grid-template-columns: repeat(3, minmax(0, 1fr)); }} }}
  @media (max-width: 580px) {{ #contact-event-review .cer-plots {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }} }}
  @media (max-width: 380px) {{ #contact-event-review .cer-plots {{ grid-template-columns: 1fr; }} }}
</style>

<script>
(() => {{
  const root = document.getElementById('contact-event-review');
  const events = {payload};
  const channels = {channel_payload};
  const buttons = root.querySelector('#cer-event-buttons');
  const plots = root.querySelector('#cer-plots');
  const heading = root.querySelector('#cer-heading');
  const tooltip = root.querySelector('#cer-tooltip');
  let selected = 0;

  const svgNS = 'http://www.w3.org/2000/svg';
  const el = (name, attrs = {{}}, text = '') => {{
    const node = document.createElementNS(svgNS, name);
    Object.entries(attrs).forEach(([key, value]) => node.setAttribute(key, value));
    if (text) node.textContent = text;
    return node;
  }};
  const nice = value => {{
    const magnitude = Math.abs(value);
    if (magnitude >= 100) return value.toFixed(0);
    if (magnitude >= 10) return value.toFixed(1);
    return value.toFixed(2);
  }};
  const bisect = (array, target) => {{
    let lo = 0, hi = array.length;
    while (lo < hi) {{ const mid = (lo + hi) >> 1; if (array[mid] < target) lo = mid + 1; else hi = mid; }}
    if (lo === 0) return 0;
    if (lo === array.length) return array.length - 1;
    return target - array[lo - 1] <= array[lo] - target ? lo - 1 : lo;
  }};

  events.forEach((event, index) => {{
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'btn';
    button.setAttribute('aria-pressed', index === 0 ? 'true' : 'false');
    button.textContent = `批次 ${{event.batch}} · 事件 ${{event.id}} · (${{event.x}}, ${{event.y}}) mm`;
    button.addEventListener('click', () => {{ selected = index; renderAll(); }});
    buttons.appendChild(button);
  }});

  function renderPlot(container, event, channel) {{
    container.replaceChildren();
    const width = Math.max(250, container.getBoundingClientRect().width || 250);
    const height = 170;
    const margin = {{top: 23, right: 12, bottom: 38, left: 58}};
    const innerW = width - margin.left - margin.right;
    const innerH = height - margin.top - margin.bottom;
    const values = event.values[channel];
    let yMin = Math.min(...values), yMax = Math.max(...values);
    if (yMin === yMax) {{ yMin -= 1; yMax += 1; }}
    const pad = (yMax - yMin) * 0.07;
    yMin -= pad; yMax += pad;
    const xMin = -2, xMax = 3;
    const sx = t => margin.left + (t - xMin) / (xMax - xMin) * innerW;
    const sy = v => margin.top + (yMax - v) / (yMax - yMin) * innerH;
    const svg = el('svg', {{viewBox: `0 0 ${{width}} ${{height}}`, role: 'img', 'aria-label': `${{channel}} 随时间变化图`}});
    svg.append(el('title', {{}}, `${{channel}}，${{event.sampleId}}`));
    svg.append(el('desc', {{}}, '横轴为检测点前后时间，纵轴为原始传感器读数。'));

    [[-2, 0, 'var(--viz-series-2)'], [0, 1, 'var(--viz-series-3)'], [1, 3, 'var(--viz-series-4)']].forEach(([a,b,c]) => {{
      svg.append(el('rect', {{x: sx(a), y: margin.top, width: sx(b)-sx(a), height: innerH, fill: c, opacity: 0.09}}));
    }});
    const yTicks = [yMin, (yMin + yMax) / 2, yMax];
    yTicks.forEach(value => {{
      svg.append(el('line', {{x1: margin.left, x2: width-margin.right, y1: sy(value), y2: sy(value), class: 'cer-grid'}}));
      svg.append(el('text', {{x: margin.left-6, y: sy(value)+4, 'text-anchor': 'end', class: 'cer-tick'}}, nice(value)));
    }});
    [-2, -1, 0, 1, 2, 3].forEach(value => {{
      svg.append(el('text', {{x: sx(value), y: height-22, 'text-anchor': value === -2 ? 'start' : value === 3 ? 'end' : 'middle', class: 'cer-tick'}}, value.toFixed(1)));
    }});
    svg.append(el('rect', {{x: margin.left, y: margin.top, width: innerW, height: innerH, class: 'cer-frame', 'data-chart-frame': ''}}));
    svg.append(el('line', {{x1: sx(0), x2: sx(0), y1: margin.top, y2: margin.top+innerH, class: 'cer-onset-line'}}));
    svg.append(el('line', {{x1: sx(1), x2: sx(1), y1: margin.top, y2: margin.top+innerH, class: 'cer-separation-line'}}));
    const path = event.t.map((t, i) => `${{i ? 'L' : 'M'}}${{sx(t).toFixed(2)}},${{sy(values[i]).toFixed(2)}}`).join(' ');
    svg.append(el('path', {{d: path, class: 'cer-line'}}));
    svg.append(el('text', {{x: margin.left, y: 15, 'text-anchor': 'start'}}, channel));
    svg.append(el('text', {{x: margin.left + innerW/2, y: height-4, 'text-anchor': 'middle', class: 'axis-title', 'data-axis': 'x'}}, '相对时间 (s)'));
    svg.append(el('text', {{x: 13, y: margin.top+innerH/2, transform: `rotate(-90 13 ${{margin.top+innerH/2}})`, 'text-anchor': 'middle', class: 'axis-title', 'data-axis': 'y'}}, '原始值'));

    const guide = el('line', {{y1: margin.top, y2: margin.top+innerH, class: 'cer-hover-guide', 'data-chart-hover-guide': '', visibility: 'hidden'}});
    const point = el('circle', {{r: 4, class: 'cer-hover-point', 'data-chart-hover-marker': '', visibility: 'hidden'}});
    svg.append(guide, point);
    const overlay = el('rect', {{x: margin.left, y: margin.top, width: innerW, height: innerH, fill: 'transparent', 'data-chart-hit': '', 'data-chart-hover-overlay': 'cross-series'}});
    overlay.addEventListener('pointermove', evt => {{
      const rect = svg.getBoundingClientRect();
      const px = (evt.clientX - rect.left) * width / rect.width;
      const time = xMin + Math.max(0, Math.min(innerW, px-margin.left)) / innerW * (xMax-xMin);
      const index = bisect(event.t, time);
      const gx = sx(time), gy = sy(values[index]);
      guide.setAttribute('x1', gx); guide.setAttribute('x2', gx); guide.setAttribute('visibility', 'visible');
      point.setAttribute('cx', sx(event.t[index])); point.setAttribute('cy', gy); point.setAttribute('visibility', 'visible');
      tooltip.hidden = false;
      tooltip.innerHTML = `<div class="cer-tooltip-row"><strong>${{channel}}</strong></div><div class="cer-tooltip-row">t=${{event.t[index].toFixed(3)}} s · 值=${{nice(values[index])}}</div>`;
      const rootRect = root.getBoundingClientRect();
      tooltip.style.left = `${{evt.clientX-rootRect.left+10}}px`;
      tooltip.style.top = `${{evt.clientY-rootRect.top+10}}px`;
    }});
    overlay.addEventListener('pointerleave', () => {{ guide.setAttribute('visibility','hidden'); point.setAttribute('visibility','hidden'); tooltip.hidden = true; }});
    svg.append(overlay);
    container.append(svg);
  }}

  function renderAll() {{
    const event = events[selected];
    [...buttons.children].forEach((button, index) => button.setAttribute('aria-pressed', index === selected ? 'true' : 'false'));
    heading.textContent = `${{event.sampleId}}　批次 ${{event.batch}}　事件 ${{event.id}}　坐标 (${{event.x}}, ${{event.y}}) mm　阈值判定：${{event.passed ? '通过' : '未通过'}}`;
    plots.replaceChildren();
    channels.forEach(channel => {{
      const container = document.createElement('div');
      container.className = 'cer-plot';
      plots.append(container);
      renderPlot(container, event, channel);
    }});
  }}

  let resizeTimer;
  let lastWidth = Math.round(root.getBoundingClientRect().width);
  window.addEventListener('resize', () => {{
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {{
      const width = Math.round(root.getBoundingClientRect().width);
      if (width !== lastWidth) {{ lastWidth = width; renderAll(); }}
    }}, 100);
  }});
  renderAll();
}})();
</script>
</body>
</html>
"""


def main() -> None:
    """加载审核样本并将交互页面写入指定路径。"""

    args = parse_args()
    channels, events, available_count = load_sample(
        args.events_csv,
        args.count,
        args.seed,
        args.batches,
        args.x,
        args.y,
        args.sample_ids,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        create_html(channels, events, args.seed, available_count), encoding="utf-8"
    )
    selected = ", ".join(str(event["sampleId"]) for event in events)
    print(f"Selected sample IDs: {selected}")
    print(f"Wrote: {args.output}")


if __name__ == "__main__":
    main()
