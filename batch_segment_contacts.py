#!/usr/bin/env python3
"""批量分割多批接触扫描，并生成跨批次汇总 CSV。

每个原始 CSV 仍由 segment_contact_events.py 独立处理。本脚本负责发现批次、
校验缓存、调用单批脚本，并将所有批次的事件数据与事件索引分别合并。
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path


def parse_args() -> argparse.Namespace:
    """解析批处理参数并检查阈值与事件窗口是否合法。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=Path("data"),
        help="source directory (default: data)",
    )
    parser.add_argument(
        "--pattern",
        default="point_grid_21x31_inclusive_step1_repeat1_movewait2_hold1_z8_*.csv",
        help="glob for raw batch CSV files",
    )
    parser.add_argument(
        "--gcode",
        type=Path,
        default=Path("point_grid_21x31_inclusive_step1_repeat1_movewait2_hold1_z8.gcode"),
    )
    parser.add_argument(
        "--output-dir", type=Path, default=Path("all_batches_events_21x31")
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=1.0,
        help="relative-change threshold as a fraction (default: 1.0 = 100%%)",
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
        "--expected-batches",
        type=int,
        default=None,
        help="optional check for the number of batches found after exclusions",
    )
    parser.add_argument(
        "--exclude-batches",
        type=int,
        nargs="*",
        default=[],
        help="batch numbers to ignore (default: none)",
    )
    parser.add_argument(
        "--reuse-existing",
        action="store_true",
        help="reuse complete batch directories instead of recomputing them",
    )
    parser.add_argument(
        "--no-individual-files", action="store_true", help="skip per-event CSV files"
    )
    parser.add_argument(
        "--only-confirmed",
        action="store_true",
        help="exclude below-threshold events from data CSVs",
    )
    args = parser.parse_args()
    if args.threshold < 0:
        parser.error("--threshold must be non-negative")
    if args.before <= 0 or args.after <= 0:
        parser.error("--before and --after must be positive")
    return args


def batch_number(path: Path) -> int:
    """从文件名末尾的 ``_<数字>.csv`` 提取批次编号。"""

    match = re.search(r"_(\d+)\.csv$", path.name, re.IGNORECASE)
    if not match:
        raise ValueError(f"cannot read batch number from {path.name}")
    return int(match.group(1))


def sha256(path: Path) -> str:
    """分块计算文件 SHA-256，用于识别被替换或修改过的原始数据。"""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def concatenate_csv(inputs: list[Path], output: Path) -> int:
    """按输入顺序合并表头一致的 CSV，并返回写出的数据行数。"""

    rows_written = 0
    expected_header: list[str] | None = None
    with output.open("w", encoding="utf-8", newline="") as output_handle:
        writer = csv.writer(output_handle)
        for path in inputs:
            with path.open("r", encoding="utf-8-sig", newline="") as input_handle:
                reader = csv.reader(input_handle)
                header = next(reader)
                if expected_header is None:
                    expected_header = header
                    writer.writerow(header)
                elif header != expected_header:
                    raise ValueError(f"CSV header mismatch: {path}")
                for row in reader:
                    writer.writerow(row)
                    rows_written += 1
    return rows_written


def main() -> None:
    """发现并处理所有批次，最后重建 combined/ 下的汇总文件。"""

    args = parse_args()
    script_dir = Path(__file__).resolve().parent
    segment_script = script_dir / "segment_contact_events.py"

    # G-code 可以相对当前目录、脚本目录或输入目录指定，按此顺序解析。
    input_dir = args.input_dir.resolve()
    if args.gcode.is_absolute():
        gcode = args.gcode
    elif args.gcode.exists():
        gcode = args.gcode.resolve()
    elif (script_dir / args.gcode).exists():
        gcode = (script_dir / args.gcode).resolve()
    else:
        gcode = (input_dir / args.gcode).resolve()
    output_dir = args.output_dir.resolve()
    if not input_dir.is_dir():
        raise SystemExit(f"Input directory not found: {input_dir}")
    if not gcode.is_file():
        raise SystemExit(f"G-code file not found: {gcode}")

    # 直接复用核心脚本的 G-code 解析函数，不在批处理脚本中写死网格或事件数。
    sys.path.insert(0, str(script_dir))
    from segment_contact_events import parse_gcode

    gcode_contacts, _ = parse_gcode(gcode)
    expected_events_per_batch = len(gcode_contacts)
    gcode_digest = sha256(gcode)

    # 只保留符合命名规则且未被明确排除的文件，并按批次编号排序。
    files = sorted(
        (
            path
            for path in input_dir.glob(args.pattern)
            if batch_number(path) not in set(args.exclude_batches)
        ),
        key=batch_number,
    )
    if not files:
        raise SystemExit(f"No input CSV files matched {args.pattern!r} in {input_dir}")
    if args.expected_batches is not None and len(files) != args.expected_batches:
        raise SystemExit(
            f"Expected {args.expected_batches} batches, found {len(files)}: "
            + ", ".join(path.name for path in files)
        )
    numbers = [batch_number(path) for path in files]
    if len(set(numbers)) != len(numbers):
        raise SystemExit(f"Duplicate batch numbers found: {numbers}")

    output_dir.mkdir(parents=True, exist_ok=True)
    summaries: list[dict[str, object]] = []
    hashes: dict[str, list[int]] = {}
    event_csvs: list[Path] = []
    manifest_csvs: list[Path] = []

    for position, source in enumerate(files, start=1):
        batch_id = batch_number(source)
        batch_dir = output_dir / f"batch_{batch_id:02d}"
        reusable = False
        metadata_path = batch_dir / "metadata.json"
        # 只有输入哈希、G-code、检测参数和导出选项全部一致时才复用旧结果。
        # 任一条件变化都会自动重新调用单批脚本。
        if args.reuse_existing and metadata_path.exists():
            old_metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            reusable = (
                old_metadata.get("batch_id") == batch_id
                and Path(old_metadata.get("source_csv", "")).name == source.name
                and old_metadata.get("source_csv_sha256") == sha256(source)
                and old_metadata.get("source_gcode_sha256") == gcode_digest
                and old_metadata.get("event_count") == expected_events_per_batch
                and old_metadata.get("threshold_fraction") == args.threshold
                and old_metadata.get("window_before_seconds") == args.before
                and old_metadata.get("window_after_seconds") == args.after
                and old_metadata.get("only_confirmed_exported") == args.only_confirmed
                and old_metadata.get("individual_files_exported")
                == (not args.no_individual_files)
                and (batch_dir / "events.csv").exists()
                and (batch_dir / "manifest.csv").exists()
            )
        action = "Reusing" if reusable else "Processing"
        print(f"[{position}/{len(files)}] {action} batch {batch_id}: {source.name}", flush=True)
        if not reusable:
            # 使用当前 Python 解释器启动核心脚本，保证虚拟环境保持一致。
            command = [
                sys.executable,
                str(segment_script),
                str(source),
                str(gcode),
                "--batch-id",
                str(batch_id),
                "--threshold",
                str(args.threshold),
                "--before",
                str(args.before),
                "--after",
                str(args.after),
                "--output-dir",
                str(batch_dir),
            ]
            if args.no_individual_files:
                command.append("--no-individual-files")
            if args.only_confirmed:
                command.append("--only-confirmed")
            subprocess.run(command, check=True)

        # 单批处理完成后再次核对事件数，防止网格不完整的数据进入汇总文件。
        metadata = json.loads((batch_dir / "metadata.json").read_text(encoding="utf-8"))
        if metadata["event_count"] != expected_events_per_batch:
            raise RuntimeError(
                f"Batch {batch_id} produced {metadata['event_count']} events; "
                f"G-code requires {expected_events_per_batch}"
            )
        digest = sha256(source)
        hashes.setdefault(digest, []).append(batch_id)
        summaries.append(
            {
                "batch_id": batch_id,
                "source_csv": source.name,
                "sha256": digest,
                "input_rows": metadata["input_rows"],
                "duration_seconds": metadata.get("input_duration_seconds"),
                "event_count": metadata["event_count"],
                "confirmed_event_count": metadata["confirmed_event_count"],
                "clock_scale": metadata["clock_scale"],
                "clock_offset_seconds": metadata["clock_offset_seconds"],
                "clock_fit_matches": metadata["clock_fit_matches"],
            }
        )
        event_csvs.append(batch_dir / "events.csv")
        manifest_csvs.append(batch_dir / "manifest.csv")

    # all_events.csv 保存所有采样行；all_manifest.csv 保存每个事件一行的索引。
    combined_dir = output_dir / "combined"
    combined_dir.mkdir(exist_ok=True)
    data_rows = concatenate_csv(event_csvs, combined_dir / "all_events.csv")
    manifest_rows = concatenate_csv(manifest_csvs, combined_dir / "all_manifest.csv")

    # 相同 SHA-256 表示不同批次文件内容完全相同；保留数据但在元数据中报告。
    duplicate_groups = [batch_ids for batch_ids in hashes.values() if len(batch_ids) > 1]
    with (combined_dir / "batch_summary.csv").open(
        "w", encoding="utf-8", newline=""
    ) as handle:
        fields = list(summaries[0])
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(summaries)

    # 汇总级元数据记录本次发现的批次、网格、参数和重复数据情况。
    run_metadata = {
        "batch_count": len(files),
        "batch_ids": numbers,
        "excluded_batch_ids": args.exclude_batches,
        "source_gcode": str(gcode),
        "source_gcode_sha256": gcode_digest,
        "events_per_batch": expected_events_per_batch,
        "grid_x_count": len({item["label_x_mm"] for item in gcode_contacts}),
        "grid_y_count": len({item["label_y_mm"] for item in gcode_contacts}),
        "event_count": manifest_rows,
        "data_row_count": data_rows,
        "threshold_fraction": args.threshold,
        "threshold_percent": args.threshold * 100.0,
        "window_before_seconds": args.before,
        "window_after_seconds": args.after,
        "duplicate_source_batch_groups": duplicate_groups,
        "note": "Excluded source batches are not processed; duplicates among included batches are reported above.",
    }
    (combined_dir / "metadata.json").write_text(
        json.dumps(run_metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        f"Done: {len(files)} batches, {manifest_rows} events, {data_rows} data rows. "
        f"Output: {output_dir}",
        flush=True,
    )
    if duplicate_groups:
        print(f"Identical source batch groups retained: {duplicate_groups}", flush=True)


if __name__ == "__main__":
    main()
