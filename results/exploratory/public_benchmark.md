# Detector sanity check on non-payment server metrics

NAB realAWSCloudwatch, thresholds set on tuning series to the same false-alert budget, scored on disjoint test series. Not payment data; never compare with the payment results.

| Detector | threshold | tune FA/day | test windows detected | test median delay (min) | test FA/day |
| --- | --- | --- | --- | --- | --- |
| CUSUM | 5.464 | 0.99 | 8/9 | 510.0 | 0.54 |
| IForest | 1.008 | 0.99 | 5/9 | 520.0 | 0.03 |
| RRCF | 0.9133 | 0.99 | 7/9 | 510.0 | 0.89 |
