"""Incremental running mean/std/min/max - Welford's online algorithm for
mean and variance, so tracking a session's summary stats doesn't require
storing full history (matches the drop counters' style: update once per
pair, read the summary whenever needed)."""


class RunningStats:
    def __init__(self):
        self.count = 0
        self.mean = 0.0
        self.min = None
        self.max = None
        self._m2 = 0.0

    def update(self, value):
        self.count += 1
        delta = value - self.mean
        self.mean += delta / self.count
        delta2 = value - self.mean
        self._m2 += delta * delta2
        self.min = value if self.min is None else min(self.min, value)
        self.max = value if self.max is None else max(self.max, value)

    @property
    def std(self):
        if self.count < 2:
            return 0.0
        return (self._m2 / self.count) ** 0.5
