# 磁传感器接触定位数据处理

本项目根据磁传感器数据变化和 G-code 扫描顺序，从连续采集数据中提取单次接触事件，并添加二维位置标签。

当前实验使用：

- G-code：`point_grid_21x31_inclusive_step1_repeat1_movewait2_hold1_z8.gcode`
- 网格：包含边界的 `21 × 31` 个点
- 坐标范围：`x = 0–20 mm`、`y = 0–30 mm`
- 点间距：`1 mm`
- 每批事件数：`21 × 31 = 651`
- 移动后等待：`2 s`
- 接触保持：`1 s`
- Z 行程：`8 mm`

## 数据格式

原始数据放在 `data/` 中，文件名末尾必须带唯一批次号，例如：

```text
data/point_grid_21x31_inclusive_step1_repeat1_movewait2_hold1_z8_1.csv
data/point_grid_21x31_inclusive_step1_repeat1_movewait2_hold1_z8_3.csv
data/point_grid_21x31_inclusive_step1_repeat1_movewait2_hold1_z8_17.csv
```

批次编号可以不连续，但不能重复。

每个 CSV 包含时间戳和 20 个传感器通道：

```text
timestamp
TL_x, TL_y, TL_z, TL_mag
TR_x, TR_y, TR_z, TR_mag
BL_x, BL_y, BL_z, BL_mag
BR_x, BR_y, BR_z, BR_mag
C_x,  C_y,  C_z,  C_mag
```

`TL`、`TR`、`BL`、`BR` 和 `C` 表示 5 个传感器，每个传感器包含 `x/y/z` 分量和磁场强度 `mag`。

## 环境

- Python 3.10 或更高版本
- 不需要第三方 Python 包
- 以下命令以 PowerShell 为例

查看参数：

```powershell
python segment_contact_events.py --help
python batch_segment_contacts.py --help
python plot_random_events.py --help
```

## 脚本

### `segment_contact_events.py`

处理一个原始 CSV，是事件检测和标签生成的核心程序。它会：

1. 读取 `timestamp + 20` 个传感器通道；
2. 从 G-code 动态解析接触坐标和事件数量；
3. 对原始通道做短窗中位数去抖；
4. 将 G-code 节拍与传感器活动自动对齐；
5. 在预测时间附近根据通道相对变化确定接触时间；
6. 提取接触前 `2.0 s` 和接触后 `3.0 s`；
7. 标记 `baseline`、`contact` 和 `separation`；
8. 生成坐标、事件和批次标签。

相对变化公式：

```text
abs(changed - baseline) / max(abs(baseline), relative_floor)
```

默认阈值为 `1.0`，即 100%。

单独处理一批，例如第 17 批：

```powershell
python segment_contact_events.py `
  "data\point_grid_21x31_inclusive_step1_repeat1_movewait2_hold1_z8_17.csv" `
  "point_grid_21x31_inclusive_step1_repeat1_movewait2_hold1_z8.gcode" `
  --batch-id 17 `
  --threshold 1.0 `
  --output-dir "all_batches_events_21x31\batch_17"
```

### `batch_segment_contacts.py`

批量调用单批脚本，并生成跨批合并文件。默认配置已经指向当前的新 G-code 和 `data/`：

```powershell
python batch_segment_contacts.py
```

默认输出目录为 `all_batches_events_21x31/`。

后续新增批次后，建议运行：

```powershell
python batch_segment_contacts.py --reuse-existing
```

脚本允许批次号不连续，并会自动发现所有匹配的 CSV。`--reuse-existing` 只有在以下内容全部一致时才会复用某批结果：

- 原始文件名及 SHA-256；
- G-code SHA-256；
- G-code 解析出的事件数量；
- 检测阈值；
- 接触点前后的提取窗口；
- 是否只导出已确认事件；
- 是否生成单事件文件。

因此覆盖或修改某个原始 CSV 后，该批会自动重新处理。

可选地检查期望批次数量：

```powershell
python batch_segment_contacts.py --expected-batches 5 --reuse-existing
```

排除指定批次：

```powershell
python batch_segment_contacts.py --exclude-batches 3 11 --reuse-existing
```

临时修改检测阈值为 80%：

```powershell
python batch_segment_contacts.py --threshold 0.80
```

临时修改接触点前后的提取窗口：

```powershell
python batch_segment_contacts.py --before 2.0 --after 3.0
```

修改阈值或提取窗口后可以继续使用 `--reuse-existing`；脚本会识别参数变化并自动重新计算不匹配的旧结果。

不生成每个事件的独立 CSV：

```powershell
python batch_segment_contacts.py --no-individual-files
```

只导出超过阈值的事件：

```powershell
python batch_segment_contacts.py --only-confirmed
```

### `plot_random_events.py`

从合并数据中随机抽取事件，生成可直接用浏览器打开的交互式审核页面。默认读取新网格输出：

```powershell
python plot_random_events.py --count 8 --seed 20260819
```

默认生成 `random_event_review_21x31.html`，打开方式：

```powershell
Invoke-Item "random_event_review_21x31.html"
```

只审核指定批次：

```powershell
python plot_random_events.py `
  --count 10 `
  --batches 17 `
  --seed 42 `
  --output "batch_17_review.html"
```

审核指定坐标在不同批次中的数据：

```powershell
python plot_random_events.py `
  --count 20 `
  --x 17 `
  --y 14 `
  --output "coordinate_17_14_review.html"
```

审核指定事件：

```powershell
python plot_random_events.py `
  --sample-ids batch_03_event_0125 batch_17_event_0650 `
  --output "selected_event_review.html"
```

保持 `--seed` 不变会得到相同的随机样本。

## 输出结构

```text
all_batches_events_21x31/
├── batch_01/
│   ├── events.csv
│   ├── manifest.csv
│   ├── metadata.json
│   └── individual/
│       ├── batch_01_event_0000_x000.0_y000.0.csv
│       └── ...
├── batch_03/
│   └── ...
└── combined/
    ├── all_events.csv
    ├── all_manifest.csv
    ├── batch_summary.csv
    └── metadata.json
```

每个事件具有跨批唯一编号，例如 `sample_id = batch_17_event_0650`。

新 G-code 的坐标对应关系为：

```text
event_0000 -> (0, 0) mm
event_0020 -> (20, 0) mm
event_0021 -> (0, 1) mm
...
event_0650 -> (20, 30) mm
```

### `combined/all_events.csv`

完整时序数据，主要用于绘图、特征提取和训练。每行是一个采样时刻：

| 字段 | 含义 |
| --- | --- |
| `batch_id` | 采集批次号 |
| `sample_id` | 跨批唯一事件编号 |
| `event_id` | 当前批次内事件编号，范围 `0–650` |
| `label_x_mm`、`label_y_mm` | 接触坐标标签 |
| `relative_time` | 相对于检测接触点的时间 |
| `phase` | `baseline`、`contact` 或 `separation` |
| `threshold_passed` | 是否超过变化阈值 |
| `timestamp` | 原始时间戳 |
| 20 个传感器字段 | 原始传感器数据 |

### `combined/all_manifest.csv`

事件级索引，每个接触事件一行，包含批次、唯一事件编号、坐标、检测时间、提取窗口、原始文件行范围、采样点数、最大变化率和触发通道。

### `combined/batch_summary.csv`

每批一行，记录源文件哈希、持续时间、事件数和时钟对齐参数，可用于发现重复或异常批次。

### `combined/metadata.json`

记录本次运行使用的 G-code、网格尺寸、每批事件数、实际批次号、总事件数和阈值等信息。

## 训练建议

训练使用：

```text
all_batches_events_21x31/combined/all_events.csv
```

建议：

1. 以 20 个传感器通道作为输入；
2. 以 `label_x_mm`、`label_y_mm` 作为二维坐标标签；
3. 按 `sample_id` 组成完整事件，不要把 CSV 的单独一行作为一个训练样本；
4. 事件采样点数量可能略有不同，输入固定长度模型前应重采样到统一长度；
5. 按 `batch_id` 划分训练、验证和测试集，避免同一批数据泄漏到不同集合；
6. 使用 `all_manifest.csv` 做异常筛选和质量检查。

## 注意事项

- 旧的 `20 × 30` 数据和 `all_batches_events/` 不应与新 `21 × 31` 数据直接合并；
- 新旧 G-code 的事件数量和坐标定义不同；
- 不要手动拼接 `all_events.csv`，应使用批处理脚本重建；
- 修改 G-code、源 CSV 或检测参数后，对应结果必须重新生成；
- 每次添加数据后，建议按新增批次随机审核事件再用于训练。
