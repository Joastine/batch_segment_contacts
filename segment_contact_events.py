#!/usr/bin/env python3
"""从连续磁传感器数据中分割接触事件，并根据 G-code 添加坐标标签。

G-code 提供接触事件的顺序和二维坐标；传感器数据用于校准 G-code 与采集设备
之间的时钟偏差，并在预测时刻附近确定实际接触点。脚本只依赖 Python 标准库。
"""

from __future__ import annotations

import argparse
import bisect
import csv
import hashlib
import json
import math
import re
import statistics
from pathlib import Path
from typing import Iterable, Sequence


MAG_SUFFIX = "_mag"


def sha256(path: Path) -> str:
    """分块计算文件的 SHA-256，避免一次性将大型 CSV 读入内存。"""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def parse_args() -> argparse.Namespace:
    """解析单批处理参数，并在读取数据前检查参数范围。"""

    parser = argparse.ArgumentParser(
        description="Split 20-channel magnetic contact data and add (x, y) mm labels."
    )
    parser.add_argument("csv_file", type=Path, help="input sensor CSV")
    parser.add_argument("gcode_file", type=Path, help="matching G-code file")
    parser.add_argument(
        "--threshold",
        type=float,
        default=1.0,
        help="relative-change threshold as a fraction (default: 1.0 = 100%%)",
    )
    parser.add_argument(
        "--relative-floor",
        type=float,
        default=10.0,
        help="minimum denominator in relative change, avoiding division near zero (default: 10)",
    )
    parser.add_argument(
        "--before",
        type=float,
        default=2.0,
        help="seconds retained before the detected change (default: 2.0)",
    )
    parser.add_argument(
        "--after",
        type=float,
        default=3.0,
        help="seconds retained after the detected change (default: 3.0)",
    )
    parser.add_argument(
        "--contact-duration",
        type=float,
        default=1.0,
        help="seconds labelled as contact before separation (default: 1.0)",
    )
    parser.add_argument(
        "--bin-seconds",
        type=float,
        default=0.05,
        help="median-resampling interval used for event detection (default: 0.05)",
    )
    parser.add_argument(
        "--smooth-seconds",
        type=float,
        default=0.20,
        help="median smoothing window used for event detection (default: 0.20)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="output directory (default: <input_stem>_events)",
    )
    parser.add_argument(
        "--batch-id",
        type=int,
        help="collection batch number written into labels and event filenames",
    )
    parser.add_argument(
        "--no-individual-files",
        action="store_true",
        help="do not write one CSV per event",
    )
    parser.add_argument(
        "--only-confirmed",
        action="store_true",
        help="exclude below-threshold events from the combined/individual CSVs",
    )
    args = parser.parse_args()
    if args.threshold < 0:
        parser.error("--threshold must be non-negative")
    if args.batch_id is not None and args.batch_id < 0:
        parser.error("--batch-id must be non-negative")
    if args.relative_floor <= 0:
        parser.error("--relative-floor must be positive")
    if min(args.before, args.after, args.contact_duration, args.bin_seconds) <= 0:
        parser.error("time parameters must be positive")
    return args


def median(values: Iterable[float]) -> float:
    """计算中位数并统一返回浮点数。"""

    return float(statistics.median(values))


def percentile(values: Sequence[float], fraction: float) -> float:
    """通过线性插值计算分位数，fraction 的取值范围通常为 0～1。"""

    ordered = sorted(values)
    if not ordered:
        raise ValueError("cannot take percentile of an empty sequence")
    pos = fraction * (len(ordered) - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return ordered[lo]
    return ordered[lo] * (hi - pos) + ordered[hi] * (pos - lo)


def read_sensor_csv(path: Path) -> tuple[list[str], list[float], list[list[float]]]:
    """读取并校验 timestamp 加 20 个传感器通道的原始 CSV。"""

    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        try:
            header = next(reader)
        except StopIteration as exc:
            raise ValueError(f"empty CSV: {path}") from exc
        if not header or header[0].strip().lower() != "timestamp":
            raise ValueError("the first CSV column must be 'timestamp'")
        if len(header) != 21:
            raise ValueError(f"expected timestamp + 20 channels, found {len(header)} columns")
        times: list[float] = []
        values: list[list[float]] = []
        for line_number, row in enumerate(reader, start=2):
            if not row or all(not item.strip() for item in row):
                continue
            if len(row) != len(header):
                raise ValueError(
                    f"row {line_number}: expected {len(header)} columns, found {len(row)}"
                )
            times.append(float(row[0]))
            values.append([float(item) for item in row[1:]])
    if len(times) < 2:
        raise ValueError("the CSV must contain at least two data rows")
    if any(right < left for left, right in zip(times, times[1:])):
        raise ValueError("timestamps must be non-decreasing")
    return header, times, values


def parse_gcode(path: Path) -> tuple[list[dict[str, float]], float]:
    """解析每次向上 Z 运动的起点时间和 XY 坐标，并返回 G-code 总时长。

    当前采集程序在探头向上接近传感器表面时产生接触，因此将 ``new_z > z``
    视为一个接触事件。标签坐标以所有接触点的最小 X/Y 为 (0, 0)。
    """

    x = y = z = elapsed = 0.0
    feed = 2000.0
    contacts: list[dict[str, float]] = []
    number = r"(-?(?:\d+(?:\.\d*)?|\.\d+))"

    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.split(";", 1)[0].strip().upper()
        if not line:
            continue
        if line.startswith("G4"):
            dwell = re.search(r"P" + number, line)
            if dwell:
                elapsed += float(dwell.group(1))
            continue
        if not line.startswith("G1"):
            standalone_feed = re.fullmatch(r"F" + number, line)
            if standalone_feed:
                feed = float(standalone_feed.group(1))
            continue

        feed_match = re.search(r"F" + number, line)
        move_feed = float(feed_match.group(1)) if feed_match else feed

        def coordinate(letter: str, old: float) -> float:
            match = re.search(letter + number, line)
            return float(match.group(1)) if match else old

        new_x = coordinate("X", x)
        new_y = coordinate("Y", y)
        new_z = coordinate("Z", z)
        z_match = re.search(r"Z" + number, line)
        if z_match and new_z > z:
            contacts.append({"gcode_time": elapsed, "gcode_x": x, "gcode_y": y})
        distance = math.dist((x, y, z), (new_x, new_y, new_z))
        if move_feed <= 0:
            raise ValueError("G-code feed rate must be positive")
        elapsed += distance / (move_feed / 60.0)
        x, y, z, feed = new_x, new_y, new_z, move_feed

    if not contacts:
        raise ValueError("no upward Z contact movements were found in the G-code")

    min_x = min(item["gcode_x"] for item in contacts)
    min_y = min(item["gcode_y"] for item in contacts)
    for item in contacts:
        # 将实际接触网格的最小坐标归一化为标签 (0, 0)。对于包含边界的
        # 21 × 31 G-code，最终标签范围正好是 X=0～20、Y=0～30。
        item["label_x_mm"] = item["gcode_x"] - min_x
        item["label_y_mm"] = item["gcode_y"] - min_y
    return contacts, elapsed


def resample_and_smooth(
    times: Sequence[float],
    values: Sequence[Sequence[float]],
    bin_seconds: float,
    smooth_seconds: float,
) -> tuple[list[float], list[list[float]]]:
    """将不规则时间戳数据按固定时间箱重采样，再进行逐通道中位数平滑。

    空时间箱沿用最近一次有效值。返回的时间以原始 CSV 首个时间戳为零点。
    """

    start = times[0]
    bin_count = int((times[-1] - start) / bin_seconds) + 1
    bins: list[list[Sequence[float]]] = [[] for _ in range(bin_count)]
    for timestamp, row in zip(times, values):
        index = min(bin_count - 1, int((timestamp - start) / bin_seconds))
        bins[index].append(row)

    resampled: list[list[float] | None] = []
    previous: list[float] | None = None
    for rows in bins:
        if rows:
            previous = [median(row[channel] for row in rows) for channel in range(20)]
        resampled.append(previous)
    if resampled[0] is None:
        raise ValueError("failed to resample the first sensor row")

    # 居中中位数滤波可去除各通道异步刷新形成的尖峰，同时尽量保留约 1 秒的
    # 接触平台，不像均值滤波那样容易被极端读数拉偏。
    radius = max(0, int(round(smooth_seconds / bin_seconds / 2.0)))
    smoothed: list[list[float]] = []
    filled = [row for row in resampled if row is not None]
    if len(filled) != len(resampled):
        # 正常数据只可能在首个有效时间箱之前出现空箱；这里仍使用前向填充，
        # 以兼容时间戳间隔异常的输入。
        first = filled[0]
        last = first
        normalized: list[list[float]] = []
        for row in resampled:
            if row is not None:
                last = row
            normalized.append(last)
    else:
        normalized = [row for row in resampled if row is not None]

    for index in range(bin_count):
        window = normalized[max(0, index - radius) : min(bin_count, index + radius + 1)]
        smoothed.append(
            [median(row[channel] for row in window) for channel in range(20)]
        )
    relative_times = [(index + 0.5) * bin_seconds for index in range(bin_count)]
    return relative_times, smoothed


def find_activity_onsets(
    smoothed: Sequence[Sequence[float]],
    channel_names: Sequence[str],
    bin_seconds: float,
) -> tuple[list[float], float]:
    """利用五个磁场强度通道寻找粗略接触活动的开始时刻。"""

    mag_indices = [i for i, name in enumerate(channel_names) if name.endswith(MAG_SUFFIX)]
    if len(mag_indices) != 5:
        raise ValueError("expected exactly five *_mag channels")
    envelope = [max(row[index] for index in mag_indices) for row in smoothed]
    # 每约 3.5 秒出现 1 秒接触，活动数据约占文件的 29%，因此使用第 70
    # 百分位作为由当前数据自适应得到的粗检测水平。
    level = percentile(envelope, 0.70)
    max_gap_bins = max(1, int(round(0.35 / bin_seconds)))
    min_run_bins = max(1, int(round(0.25 / bin_seconds)))
    raw_runs: list[tuple[int, int]] = []
    start: int | None = None
    for index, value in enumerate(envelope + [float("-inf")]):
        if value > level and start is None:
            start = index
        elif value <= level and start is not None:
            raw_runs.append((start, index))
            start = None

    merged: list[list[int]] = []
    for run_start, run_end in raw_runs:
        if merged and run_start - merged[-1][1] <= max_gap_bins:
            merged[-1][1] = run_end
        else:
            merged.append([run_start, run_end])
    onsets = [start * bin_seconds for start, end in merged if end - start >= min_run_bins]
    return onsets, level


def linear_fit(xs: Sequence[float], ys: Sequence[float]) -> tuple[float, float]:
    """最小二乘拟合 ``y = slope * x + offset``。"""

    x_mean = statistics.fmean(xs)
    y_mean = statistics.fmean(ys)
    denominator = sum((x - x_mean) ** 2 for x in xs)
    if denominator == 0:
        return 1.0, y_mean - x_mean
    slope = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys)) / denominator
    return slope, y_mean - slope * x_mean


def align_gcode_clock(
    contacts: Sequence[dict[str, float]], activity_onsets: Sequence[float]
) -> tuple[float, float, int]:
    """拟合 G-code 时间到传感器相对时间的线性映射。

    返回时钟缩放系数、时间偏移和最终参与拟合的匹配事件数。全局搜索允许
    采集程序在 G-code 运行前已经启动、在运行结束后仍继续记录。
    """

    if len(activity_onsets) < 2:
        raise ValueError("not enough coarse sensor activity to align the G-code clock")
    gcode_times = [item["gcode_time"] for item in contacts]
    # 记录程序可能在扫描前很久启动，或在扫描结束后仍继续运行，所以不能直接
    # 对齐文件首尾。先用每 10 个 G-code 事件对候选偏移投票，再用全部接触事件
    # 评估得票最高的候选项。
    best: tuple[int, float, float, float] | None = None
    maximum_offset = activity_onsets[-1] - gcode_times[-1] * 0.98 + 10.0
    for slope_step in range(1960, 2061):
        candidate_slope = slope_step / 2000.0  # 0.9800 .. 1.0300
        votes: dict[float, int] = {}
        for gcode_time in gcode_times[::10]:
            projected = gcode_time * candidate_slope
            for onset in activity_onsets:
                candidate_offset = round((onset - projected) * 10.0) / 10.0
                if -10.0 <= candidate_offset <= maximum_offset:
                    votes[candidate_offset] = votes.get(candidate_offset, 0) + 1
        for candidate_offset, _ in sorted(
            votes.items(), key=lambda item: item[1], reverse=True
        )[:40]:
            distances: list[float] = []
            onset_index = 0
            for gcode_time in gcode_times:
                predicted = gcode_time * candidate_slope + candidate_offset
                while (
                    onset_index + 1 < len(activity_onsets)
                    and abs(activity_onsets[onset_index + 1] - predicted)
                    < abs(activity_onsets[onset_index] - predicted)
                ):
                    onset_index += 1
                distances.append(abs(activity_onsets[onset_index] - predicted))
            match_count = sum(distance <= 0.45 for distance in distances)
            median_distance = median(distances)
            candidate = (
                match_count,
                -median_distance,
                candidate_slope,
                candidate_offset,
            )
            if best is None or candidate > best:
                best = candidate
    if best is None:
        raise ValueError("could not align G-code cadence to sensor activity")
    _, _, slope, offset = best

    matched_count = 0
    for _ in range(4):
        pairs: list[tuple[float, float]] = []
        for gcode_time in gcode_times:
            predicted = slope * gcode_time + offset
            position = bisect.bisect_left(activity_onsets, predicted)
            candidates = activity_onsets[max(0, position - 1) : min(len(activity_onsets), position + 1)]
            if candidates:
                nearest = min(candidates, key=lambda value: abs(value - predicted))
                if abs(nearest - predicted) <= 0.65:
                    pairs.append((gcode_time, nearest))
        if len(pairs) < 2:
            break
        residuals = [abs(y - (slope * x + offset)) for x, y in pairs]
        cutoff = max(0.12, percentile(residuals, 0.85))
        inliers = [(x, y) for (x, y), residual in zip(pairs, residuals) if residual <= cutoff]
        if len(inliers) >= 2:
            slope, offset = linear_fit(
                [pair[0] for pair in inliers], [pair[1] for pair in inliers]
            )
            matched_count = len(inliers)
    if not 0.95 <= slope <= 1.05:
        raise ValueError(f"implausible G-code/data clock scale: {slope:.6f}")
    return slope, offset, matched_count


def window_median(
    smoothed: Sequence[Sequence[float]],
    bin_seconds: float,
    start: float,
    end: float,
) -> list[float]:
    """计算指定相对时间窗口内 20 个通道各自的中位数。"""

    first = max(0, int(math.floor(start / bin_seconds)))
    last = min(len(smoothed), int(math.ceil(end / bin_seconds)))
    if last <= first:
        index = max(0, min(len(smoothed) - 1, first))
        return list(smoothed[index])
    return [median(smoothed[i][channel] for i in range(first, last)) for channel in range(20)]


def refine_event(
    predicted: float,
    smoothed: Sequence[Sequence[float]],
    channel_names: Sequence[str],
    bin_seconds: float,
    threshold: float,
    relative_floor: float,
) -> dict[str, object]:
    """在 G-code 预测时刻附近搜索传感器变化最明显的实际接触点。

    每个候选时刻分别计算接触前基线和接触后读数，并使用
    ``abs(changed-baseline) / max(abs(baseline), relative_floor)`` 评价变化。
    任一通道严格大于 threshold 时，该事件通过阈值判定。
    """

    candidates: list[dict[str, object]] = []
    steps = int(round(0.9 / bin_seconds)) + 1
    for step_index in range(steps):
        candidate_time = predicted - 0.40 + step_index * bin_seconds
        baseline = window_median(smoothed, bin_seconds, candidate_time - 0.50, candidate_time - 0.15)
        changed = window_median(smoothed, bin_seconds, candidate_time + 0.10, candidate_time + 0.35)
        changes = [
            abs(after - before) / max(abs(before), relative_floor)
            for before, after in zip(baseline, changed)
        ]
        passed = [index for index, change in enumerate(changes) if change > threshold]
        strongest = max(range(20), key=lambda index: changes[index])
        top = sorted(changes, reverse=True)[:5]
        # 独立变化通道数是更稳健的主评分；同时限制最大变化贡献，避免基线接近
        # 零的单个通道因相对变化率过大而主导接触时刻。
        score = (len(passed), sum(min(value, 5.0) for value in top))
        candidates.append(
            {
                "time": candidate_time,
                "score": score,
                "changes": changes,
                "strongest": strongest,
                "passed": passed,
            }
        )

    # 选择变化最强且涉及通道最多的候选点；评分相同时优先靠近 G-code 预测时刻，
    # 防止单通道噪声把接触点拉到搜索区间的左边缘。
    selected = max(
        candidates,
        key=lambda item: (
            item["score"],
            -abs(float(item["time"]) - predicted),
        ),
    )
    changes = selected["changes"]
    strongest = int(selected["strongest"])
    return {
        "event_time": float(selected["time"]),
        "max_relative_change": float(changes[strongest]),
        "trigger_channel": channel_names[strongest],
        "changed_channel_count": sum(value > threshold for value in changes),
        "threshold_passed": max(changes) > threshold,
    }


def phase(relative_time: float, contact_duration: float) -> str:
    """按照相对接触时刻将一行样本标记为 baseline/contact/separation。"""

    if relative_time < 0:
        return "baseline"
    if relative_time < contact_duration:
        return "contact"
    return "separation"


def clean_number(value: float) -> int | float:
    """将数值上等于整数的坐标写成整数，避免出现 2.000000 等标签。"""

    rounded = round(value)
    return int(rounded) if math.isclose(value, rounded, abs_tol=1e-9) else value


def main() -> None:
    """执行单批事件检测、切片、标注和结果文件写出。"""

    args = parse_args()

    # 第一步：读取两种时间序列，并从 G-code 得到理论事件时刻及坐标。
    header, times, raw_values = read_sensor_csv(args.csv_file)
    channel_names = header[1:]
    contacts, gcode_duration = parse_gcode(args.gcode_file)
    _, smoothed = resample_and_smooth(
        times, raw_values, args.bin_seconds, args.smooth_seconds
    )
    activity_onsets, activity_level = find_activity_onsets(
        smoothed, channel_names, args.bin_seconds
    )

    # 第二步：将 G-code 时钟映射到传感器时钟，消除启动延迟和轻微时钟漂移。
    clock_scale, clock_offset, clock_matches = align_gcode_clock(contacts, activity_onsets)

    batch_id = args.batch_id
    if batch_id is None:
        batch_match = re.search(r"_(\d+)$", args.csv_file.stem)
        batch_id = int(batch_match.group(1)) if batch_match else 0
    batch_name = f"batch_{batch_id:02d}"

    # 第三步：逐事件细化接触时刻，并记录原始 CSV 中对应的切片范围。
    manifests: list[dict[str, object]] = []
    for event_id, contact in enumerate(contacts):
        predicted = clock_scale * contact["gcode_time"] + clock_offset
        detection = refine_event(
            predicted,
            smoothed,
            channel_names,
            args.bin_seconds,
            args.threshold,
            args.relative_floor,
        )
        event_time_abs = times[0] + float(detection["event_time"])
        start_time = event_time_abs - args.before
        end_time = event_time_abs + args.after
        first_row = bisect.bisect_left(times, start_time)
        last_row = bisect.bisect_right(times, end_time)
        manifests.append(
            {
                "batch_id": batch_id,
                "sample_id": f"{batch_name}_event_{event_id:04d}",
                "event_id": event_id,
                "label_x_mm": clean_number(contact["label_x_mm"]),
                "label_y_mm": clean_number(contact["label_y_mm"]),
                "gcode_x_mm": clean_number(contact["gcode_x"]),
                "gcode_y_mm": clean_number(contact["gcode_y"]),
                "predicted_time": times[0] + predicted,
                "detected_time": event_time_abs,
                "window_start": start_time,
                "window_end": end_time,
                "first_source_row": first_row + 2,
                "last_source_row": last_row + 1,
                "sample_count": max(0, last_row - first_row),
                **detection,
                "first_index": first_row,
                "last_index": last_row,
            }
        )

    # 第四步：写出事件时序数据。events.csv 合并本批全部事件；individual/
    # 中的文件则便于人工审核单个事件。
    output_dir = args.output_dir or args.csv_file.with_name(args.csv_file.stem + "_events")
    output_dir.mkdir(parents=True, exist_ok=True)
    individual_dir = output_dir / "individual"
    if not args.no_individual_files:
        individual_dir.mkdir(parents=True, exist_ok=True)

    output_header = [
        "batch_id",
        "sample_id",
        "event_id",
        "label_x_mm",
        "label_y_mm",
        "relative_time",
        "phase",
        "threshold_passed",
        *header,
    ]
    combined_path = output_dir / "events.csv"
    with combined_path.open("w", encoding="utf-8", newline="") as combined_handle:
        combined_writer = csv.writer(combined_handle)
        combined_writer.writerow(output_header)
        for item in manifests:
            if args.only_confirmed and not item["threshold_passed"]:
                continue
            rows: list[list[object]] = []
            event_time = float(item["detected_time"])
            for index in range(int(item["first_index"]), int(item["last_index"])):
                relative_time = times[index] - event_time
                rows.append(
                    [
                        item["batch_id"],
                        item["sample_id"],
                        item["event_id"],
                        item["label_x_mm"],
                        item["label_y_mm"],
                        f"{relative_time:.6f}",
                        phase(relative_time, args.contact_duration),
                        int(bool(item["threshold_passed"])),
                        f"{times[index]:.6f}",
                        *[f"{value:.6f}" for value in raw_values[index]],
                    ]
                )
            combined_writer.writerows(rows)
            if not args.no_individual_files:
                name = (
                    f"{batch_name}_event_{int(item['event_id']):04d}_"
                    f"x{float(item['label_x_mm']):05.1f}_y{float(item['label_y_mm']):05.1f}.csv"
                )
                with (individual_dir / name).open("w", encoding="utf-8", newline="") as handle:
                    writer = csv.writer(handle)
                    writer.writerow(output_header)
                    writer.writerows(rows)

    # manifest.csv 每个事件仅占一行，适合作为索引和质量检查清单。
    manifest_fields = [
        "batch_id",
        "sample_id",
        "event_id",
        "label_x_mm",
        "label_y_mm",
        "gcode_x_mm",
        "gcode_y_mm",
        "predicted_time",
        "detected_time",
        "window_start",
        "window_end",
        "first_source_row",
        "last_source_row",
        "sample_count",
        "max_relative_change",
        "trigger_channel",
        "changed_channel_count",
        "threshold_passed",
    ]
    with (output_dir / "manifest.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=manifest_fields, extrasaction="ignore")
        writer.writeheader()
        for item in manifests:
            row = dict(item)
            row["threshold_passed"] = int(bool(row["threshold_passed"]))
            writer.writerow(row)

    confirmed = sum(bool(item["threshold_passed"]) for item in manifests)
    label_x_values = sorted({item["label_x_mm"] for item in contacts})
    label_y_values = sorted({item["label_y_mm"] for item in contacts})
    # metadata.json 保存输入哈希、参数和对齐质量，供批处理脚本判断能否复用。
    metadata = {
        "source_csv": str(args.csv_file.resolve()),
        "source_gcode": str(args.gcode_file.resolve()),
        "source_csv_sha256": sha256(args.csv_file),
        "source_gcode_sha256": sha256(args.gcode_file),
        "batch_id": batch_id,
        "batch_name": batch_name,
        "input_rows": len(times),
        "input_channels": len(channel_names),
        "input_duration_seconds": times[-1] - times[0],
        "gcode_duration_seconds": gcode_duration,
        "grid_x_count": len(label_x_values),
        "grid_y_count": len(label_y_values),
        "grid_x_min_mm": clean_number(min(label_x_values)),
        "grid_x_max_mm": clean_number(max(label_x_values)),
        "grid_y_min_mm": clean_number(min(label_y_values)),
        "grid_y_max_mm": clean_number(max(label_y_values)),
        "event_count": len(manifests),
        "confirmed_event_count": confirmed,
        "threshold_fraction": args.threshold,
        "threshold_percent": args.threshold * 100.0,
        "relative_change_formula": "abs(changed-baseline) / max(abs(baseline), relative_floor)",
        "relative_floor": args.relative_floor,
        "window_before_seconds": args.before,
        "window_after_seconds": args.after,
        "contact_duration_seconds": args.contact_duration,
        "detection_bin_seconds": args.bin_seconds,
        "detection_smooth_seconds": args.smooth_seconds,
        "coarse_activity_level": activity_level,
        "coarse_activity_onset_count": len(activity_onsets),
        "clock_scale": clock_scale,
        "clock_offset_seconds": clock_offset,
        "clock_fit_matches": clock_matches,
        "label_origin": "minimum contacted G-code X/Y mapped to (0, 0) mm",
        "only_confirmed_exported": args.only_confirmed,
        "individual_files_exported": not args.no_individual_files,
    }
    (output_dir / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        f"Done: {len(manifests)} G-code events, {confirmed} passed "
        f"the {args.threshold * 100:g}% threshold. Output: {output_dir}"
    )


if __name__ == "__main__":
    main()
