
import csv
import os
import time
from datetime import datetime
from pytz import timezone as pytz_timezone
from typing import Optional, Any

class DecisionLogger:
    """Logs detailed strategy decisions to a dedicated CSV file."""
    
    def __init__(self, output_file: str = "bot_decisions.csv"):
        self.output_file = output_file
        self.headers = [
            "timestamp_est",
            "timestamp_ms",
            "event_slug",
            "formatted_slug",
            "trigger",
            "decision",
            "reason",
            "best_outcome",
            "best_price",
            "threshold",
            "limit_price",
            "order_id",
            "price_source",
            "raw_prices"
        ]
        self._setup_file()

    def _setup_file(self):
        file_exists = os.path.isfile(self.output_file)
        with open(self.output_file, "a", newline="") as f:
            writer = csv.writer(f)
            if not file_exists or os.path.getsize(self.output_file) == 0:
                writer.writerow(self.headers)

    def log_decision(
        self,
        event_slug: str,
        trigger: str,
        decision: str,
        reason: str,
        formatted_slug: str = "",
        best_outcome: str = "",
        best_price: Optional[float] = None,
        threshold: Optional[float] = None,
        limit_price: Optional[float] = None,
        order_id: str = "",
        price_source: str = "Gamma",
        raw_prices: str = ""
    ):
        """Logs a single strategy decision row."""
        ms = int(time.time() * 1000)
        est_tz = pytz_timezone("US/Eastern")
        est_str = datetime.fromtimestamp(ms / 1000.0, tz=est_tz).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        
        with open(self.output_file, "a", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                est_str,
                ms,
                event_slug,
                formatted_slug,
                trigger,
                decision,
                reason,
                best_outcome,
                f"{best_price:.4f}" if best_price is not None else "",
                f"{threshold:.3f}" if threshold is not None else "",
                f"{limit_price:.3f}" if limit_price is not None else "",
                order_id,
                price_source,
                raw_prices
            ])
