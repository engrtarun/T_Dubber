import json
import logging
import os
from pathlib import Path
import tgup_bridge

CONFIG_FILE = Path(__file__).parent / "autotune_config.json"

def run_benchmark_and_tune(channel: str, api_id: int, api_hash: str) -> int:
    """Run the Go benchmarking tool to find the optimal concurrency level.
    Returns the optimal concurrency (default 3 if benchmark fails).
    """
    logging.info("Running network benchmark to determine optimal Go concurrency... (This may take a minute)")
    
    # We test concurrency levels 1, 2, 3, 4
    result = tgup_bridge.bench(
        channel=channel,
        api_id=api_id,
        api_hash=api_hash,
        size_mb=32, # 32MB test file
        levels="1,2,3,4"
    )
    
    if not result.get("ok"):
        logging.error(f"Benchmark failed, defaulting to concurrency=3. Error: {result.get('log')}")
        return 3
        
    rows = result.get("rows", {})
    if not rows:
        return 3
        
    # Analyze the benchmark results to find the sweet spot
    # We want the lowest concurrency that gives us ~max speed to avoid FLOOD_WAIT
    max_rate = 0.0
    optimal = 3
    
    for level, data in rows.items():
        rate = data.get("rate_mb_s", 0.0)
        # If this level adds at least 15% speed improvement, we take it
        if rate > max_rate * 1.15:
            max_rate = rate
            optimal = int(level)
            
    # Save to config so we don't have to run this every time
    config = {"optimal_concurrency": optimal, "max_rate_mb_s": max_rate}
    try:
        CONFIG_FILE.write_text(json.dumps(config, indent=2))
        logging.info(f"Auto-tuning complete! Optimal concurrency: {optimal} ({max_rate:.2f} MB/s)")
    except Exception as e:
        logging.error(f"Failed to save autotune config: {e}")
        
    return optimal

def get_optimal_concurrency(channel: str = "", api_id: int = 0, api_hash: str = "") -> int:
    """Get the cached optimal concurrency, or run the benchmark if missing."""
    if CONFIG_FILE.exists():
        try:
            config = json.loads(CONFIG_FILE.read_text())
            return config.get("optimal_concurrency", 3)
        except Exception:
            pass
            
    # If not cached and we have credentials, run it now
    if channel and api_id and api_hash:
        return run_benchmark_and_tune(channel, api_id, api_hash)
        
    return 3 # fallback
