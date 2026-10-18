# Decision 0025: robust robot signatures stay diagnostic

Store per-phase, payload and firmware medians, MAD scales and empirical residual quantiles for force, tactile, temperature and joint-current signals. A detected exceedance becomes a bitemporal episode-linked anomaly and a conservative inspection recovery. Firmware drift is reported as an estimate. Optional time-series forecasting is adapter-owned and is not trusted as a safety verdict. Baseline representativeness and sensor calibration remain deployment assumptions.
