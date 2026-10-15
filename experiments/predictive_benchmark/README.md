# CourtVision：预测模型对照与时间序列实验

独立离线实验，不改变 CourtVision 正式模型、预测版本、Agent 或网站。不是新增 LLM 流程。

已执行结果见 [RESULTS.md](RESULTS.md)：真实LightGBM/XGBoost训练与ARIMA滚动回测已完成，不只是示例代码。

## 为什么做

用同一份结构化历史数据检验简单模型与梯度提升树的效果，并以独立的球队得分任务实践时间序列滚动预测。模型是否更复杂不等于效果更好；保留失败和不优于基线的结果。

## 两个不同任务

1. **表格预测**：胜负概率、主队净胜分、比赛总分。比较常量、逻辑/线性回归、HistGradientBoosting、LightGBM、XGBoost。复用85项冻结特征及现有指标。
2. **时间序列**：六支按数字ID固定选出的球队，预测下一场常规赛球队得分。比较上一场、近10场均值、EWMA(alpha=0.2)、固定ARIMA(1,0,0)。不是同一个预测任务，不能将两者MAE横向宣称优劣。

## 时间协议

- 2024为外层验证季，2025为已被查看过的历史测试季，不称作新盲测。
- 每个表格评估季用之前完整五季训练。两个固定树配置仅按2024验证结果选择。
- LightGBM/XGBoost早停只用训练窗口最后一季；先在更早四季拟合、训练内最后一季早停，再用选定树数重训完整五季。测试阶段冻结配置与树数，重训窗口前移一年，不用测试标签早停。
- 旧报告HGB可能使用默认早停，本实验明确关闭HGB内部随机早停。因此只在本实验的相同样本/设置下比较，不冒充旧产物逐字节重现。
- ARIMA以比赛序号为时间轴，最多使用此前60场、至少30场。每一场之前重新拟合；只将已结束的先前场次作为下一起点的历史。不能补无比赛日为0，不能凭空设7天季节性。
- ARIMA不收敛/异常保留失败，绝不偷偷换成均值；提供各自覆盖数和共同成功样本指标。
- 本实验是历史回测，不证明当年曾真实保存线上信息快照。休息等既有模型特征只用作内部输入，不升级为公开篮球事实。

## Windows运行

推荐独立 Python 3.13 环境，下面从仓库根目录执行：

```powershell
python -m venv experiments/predictive_benchmark/.venv
& experiments/predictive_benchmark/.venv/Scripts/python.exe -m pip install -r experiments/predictive_benchmark/requirements.txt
& experiments/predictive_benchmark/.venv/Scripts/python.exe -m pytest experiments/predictive_benchmark/tests -q
& experiments/predictive_benchmark/.venv/Scripts/python.exe experiments/predictive_benchmark/run.py --dataset data/processed/basketball_only_games.csv --output experiments/predictive_benchmark/runs/my-first-run
```

`--dataset`必须指向现有校验匹配的数据快照；本机数据位于相邻私有工作区时可显式传入那个路径，程序不静默寻找替代数据。`input_contract.json`只分发85项特征名、输入期望SHA256和原始元数据hash，不包含数据行；这些值来自原私有复现清单，公开运行不再要求缺失的私有元数据文件。公开仓库若没有原始数据，需按原数据文档取得合法数据并重建，不能保证无数据环境直接复现；不自动联网下载。

`--output`必须是不存在的新目录，避免覆盖旧实验。运行产物默认不进Git；输入原始快照也不上传。脚本只读CSV和冻结元数据，不访问生产数据库或外部API。依赖安装有网络，实验本身无网络。

## 产物

- `REPORT.md`：本次真实结果、分母和限制。
- `tabular_metrics.csv` / `tabular_predictions.csv`：所有配置验证结果、选中配置历史测试结果与逐场预测。
- `tabular_fits.csv` / `validation_selections.csv`：训练内早停、树数、外层选择记录。
- `timeseries_predictions.csv`：每队每场每方法的状态、预测、历史截止、参数、警告和耗时。
- `timeseries_metrics.csv` / `timeseries_common_predictions.csv`：失败数与相同成功样本对照。
- `manifest.json`：输入/代码/配置/结果hash，包版本及执行时间。
- `protection.json`：已有源代码、冻结模型与产物的运行前后hash保护。

完成后可以独立核对产物及时间协议：

```powershell
& experiments/predictive_benchmark/.venv/Scripts/python.exe experiments/predictive_benchmark/verify_run.py experiments/predictive_benchmark/runs/my-first-run
```

不会自动把新模型保存为生产权重、覆盖旧报告或部署。源码和研究结论可单独审查后公开；逐场派生数据默认留本地，不擅自判断第三方许可。

## 复现与方法检查

1. 亲自运行测试，解释“训练内早停”和“外层验证”为什么分开。
2. 查看验证选择文件，找到一个复杂模型没有超过线性模型的任务；若不存在，诚实说明当前结果，不制造例子。
3. 任取一条时间序列预测，核对 `history_end < game_date`，理解同一场结果不能用于预测自身。
4. 修改一个有依据的训练参数，使用新目录运行；不能依据测试结果反复挑选后称为盲测。
5. 能解释学习率/树数/深度、过拟合、Log Loss与MAE、ARIMA的p/d/q；本实验固定(1,0,0)，不等于已掌握所有时间序列方法。

这些检查用于理解实验的适用范围；一次历史实验不能保证未来指标提升。
